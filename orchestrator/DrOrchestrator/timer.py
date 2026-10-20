"""
Timer trigger — the automatic half of DR.

Every 20 seconds: probe the primary, update the consecutive-failure counter,
and start a failover when the threshold is crossed.

Deliberately NOT here:
  * latency-based failover (see README: "slow is not down")
  * automatic failback (see state_machine.py docstring)
  * any remediation of the *primary* — healing the primary is the
    self-healing project's job, not DR's. Mixing them means a flapping primary
    causes a failover/failback loop.
"""

from __future__ import annotations

import os
import logging
import asyncio

import aiohttp
import azure.functions as func
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from .state_machine import DrState, DrStateMachine, run_steps
from .clients import AzureOps

log = logging.getLogger("dr.timer")
logging.getLogger("azure").setLevel(logging.WARNING)

FAILURES_TO_TRIGGER = int(os.environ.get("PROBE_FAILURES_TO_TRIGGER", "3"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "5"))
GRACE_SECONDS = int(os.environ.get("FAILOVER_GRACE_SECONDS", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
ENV = os.environ.get("ENVIRONMENT_NAME", "prod")

PRIMARY_HOST = os.environ.get("PRIMARY_APP", "primary-app") + ".azurewebsites.net"
SECONDARY_HOST = os.environ.get("SECONDARY_APP", "secondary-app") + ".azurewebsites.net"


async def probe(session: aiohttp.ClientSession, host: str) -> dict:
    """One availability probe. Returns evidence, not just a boolean."""
    url = f"https://{host}/health"
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as r:
            body = await r.text()
            return {
                "host": host,
                "ok": r.status == 200,
                "status": r.status,
                "latencyMs": round((loop.time() - t0) * 1000),
                "body": body[:200],
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "host": host,
            "ok": False,
            "status": None,
            "latencyMs": round((loop.time() - t0) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Failover steps. Order matters more than anything else in this file.
# ---------------------------------------------------------------------------

async def step_fence_writes(ctx: dict) -> str:
    """Stop the primary app so it cannot accept writes during promotion.

    Without fencing, a partitioned-but-alive primary keeps writing and you get
    split-brain: two databases both believing they are authoritative. Fencing
    is the single most important step and the one most often omitted.
    """
    if ctx["primary_probe"]["ok"]:
        raise RuntimeError("primary still reachable — refusing to fence (likely AFD/DNS fault)")
    return "primary unreachable; no fencing needed"


async def step_check_replication(ctx: dict) -> str:
    lag = await ctx["ops"].replication_lag_seconds(
        ctx["primary_rg"], ctx["sql_server"], ctx["database"])
    ctx["replication_lag_seconds"] = lag
    if lag > 300:
        raise RuntimeError(f"replication lag {lag}s exceeds 300s — RPO would be unacceptable")
    return f"replication lag {lag:.1f}s (within RPO)"


async def step_promote_sql(ctx: dict) -> str:
    return await ctx["ops"].promote_failover_group(
        ctx["primary_rg"], ctx["sql_server"], ctx["failover_group"])


async def step_scale_standby(ctx: dict) -> str:
    return await ctx["ops"].set_plan_capacity(
        ctx["secondary_rg"], ctx["secondary_plan"], ctx["standby_target_instances"])


async def step_flip_traffic(ctx: dict) -> str:
    a = await ctx["ops"].set_origin_priority(
        ctx["global_rg"], ctx["afd_profile"], "app-origins", "secondary", 1)
    b = await ctx["ops"].set_origin_priority(
        ctx["global_rg"], ctx["afd_profile"], "app-origins", "primary", 2)
    return f"{a}; {b}"


async def step_verify_secondary(ctx: dict) -> str:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 180
    async with aiohttp.ClientSession() as session:
        while loop.time() < deadline:
            r = await probe(session, ctx["secondary_host"])
            if r["ok"] and "secondary" in r.get("body", "").lower():
                return f"secondary serving traffic ({r['latencyMs']}ms)"
            await asyncio.sleep(5)
    raise RuntimeError("secondary did not serve traffic within 180s")


FAILOVER_STEPS = [
    ("fence_writes", step_fence_writes),
    ("check_replication", step_check_replication),
    ("promote_sql", step_promote_sql),
    ("scale_standby", step_scale_standby),
    ("flip_traffic", step_flip_traffic),
    ("verify_secondary", step_verify_secondary),
]


def _ctx(ops: AzureOps, primary_probe: dict) -> dict:
    return {
        "ops": ops,
        "primary_probe": primary_probe,
        "primary_rg": os.environ["PRIMARY_RG"],
        "secondary_rg": os.environ["SECONDARY_RG"],
        "global_rg": os.environ.get("GLOBAL_RG", os.environ["PRIMARY_RG"]),
        "sql_server": os.environ["SQL_SERVER"],
        "failover_group": os.environ["FAILOVER_GROUP"],
        "database": os.environ.get("DATABASE_NAME", "orders"),
        "secondary_plan": os.environ["SECONDARY_APP"].replace("-app-", "-plan-"),
        "standby_target_instances": int(os.environ.get("STANDBY_TARGET_INSTANCES", "3")),
        "afd_profile": os.environ["AFD_PROFILE"],
        "secondary_host": SECONDARY_HOST,
    }


async def execute_failover(sm: DrStateMachine, ops: AzureOps, primary_probe: dict,
                           trigger: str = "timer", actor: str = "system") -> dict:
    applied, t = await sm.transition(
        ENV, DrState.FAILING_OVER, trigger=trigger, actor=actor,
        reason=f"{FAILURES_TO_TRIGGER} consecutive probe failures",
        evidence={"primary": primary_probe},
    )
    if not applied or t is None:
        return {"applied": False}

    ctx = _ctx(ops, primary_probe)
    ok = await run_steps(FAILOVER_STEPS, ctx, t)
    if ok:
        await sm.finish(t, True, f"RPO gap {ctx.get('replication_lag_seconds', 0):.1f}s")
        await sm.transition(ENV, DrState.FAILED_OVER, trigger=trigger, actor=actor,
                            reason="failover completed")
    else:
        await sm.finish(t, False, "see steps")
        await sm.transition(ENV, DrState.FAILOVER_FAILED, trigger=trigger, actor=actor,
                            reason="failover steps failed — human required")
    return {"applied": True, "ok": ok, "steps": t.steps}


async def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        log.warning("timer is past due — possible cold start or scale issue")

    cred = DefaultAzureCredential()
    cosmos = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=cred)
    sm = DrStateMachine(
        cosmos,
        os.environ["COSMOS_DATABASE"],
        os.environ["COSMOS_CONTAINER"],
        os.environ.get("COSMOS_AUDIT_CONTAINER", "transitions"),
        dry_run=DRY_RUN,
    )
    ops = AzureOps(os.environ["AZURE_SUBSCRIPTION_ID"], cred)

    try:
        state = await sm.get_state(ENV)
        if state["state"] in (DrState.FAILED_OVER.value, DrState.FAILING_BACK.value):
            log.info("state=%s — timer stands down (manual failback territory)", state["state"])
            return

        async with aiohttp.ClientSession() as session:
            primary = await probe(session, PRIMARY_HOST)

        if primary["ok"]:
            if state.get("consecutiveFailures", 0) > 0:
                log.info("primary recovered after %d failures", state["consecutiveFailures"])
                await sm.transition(ENV, DrState.HEALTHY, trigger="timer", actor="system",
                                    reason="primary probe recovered")
            return

        failures = state.get("consecutiveFailures", 0) + 1
        await sm.record_failure(ENV, failures)
        log.warning("primary probe failed (%d/%d): %s", failures, FAILURES_TO_TRIGGER,
                    primary.get("error") or primary.get("status"))

        if failures < FAILURES_TO_TRIGGER:
            await sm.transition(ENV, DrState.DEGRADED, trigger="timer", actor="system",
                                reason=f"probe failure {failures}/{FAILURES_TO_TRIGGER}")
            return

        log.error("threshold reached — waiting %ds grace before failover", GRACE_SECONDS)
        await asyncio.sleep(GRACE_SECONDS)

        # Re-probe after the grace period: transient blips must not fail over.
        async with aiohttp.ClientSession() as session:
            primary = await probe(session, PRIMARY_HOST)
        if primary["ok"]:
            await sm.transition(ENV, DrState.HEALTHY, trigger="timer", actor="system",
                                reason="primary recovered during grace period")
            return

        await execute_failover(sm, ops, primary)
    finally:
        await ops.close()
        await cosmos.close()
        await cred.close()
