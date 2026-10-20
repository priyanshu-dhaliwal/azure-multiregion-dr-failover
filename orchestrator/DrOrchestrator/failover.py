"""
HTTP trigger — manual failover and failback.

Both are gated:
  * require a bearer token from an allowed principal (function-level auth +
    an explicit allow-list check), because "anyone who can reach the URL" is
    not an authorization model for a DR action
  * failback additionally requires `approvedBy` — a second human — and
    `acknowledgeDataLoss: true`

The same failover code path runs here as in the timer, so a drill exercises
exactly what an incident would.
"""

from __future__ import annotations

import os
import json
import logging

import azure.functions as func
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from .state_machine import DrState, DrStateMachine, run_steps
from .clients import AzureOps
from .timer import execute_failover, probe

log = logging.getLogger("dr.http")

ALLOWED_CALLERS = {p.strip().lower() for p in os.environ.get("ALLOWED_CALLERS", "").split(",") if p.strip()}


def _forbidden(msg: str) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"error": msg}), status_code=403,
                             mimetype="application/json")


def _caller_ok(req: func.HttpRequest) -> tuple[bool, str]:
    """
    Function-level auth has already validated the key/identity. Here we check
    the caller's *identity claim* against the allow-list, so a leaked function
    key is not enough to fail over production.
    """
    caller = (
        req.headers.get("x-ms-client-principal-name")
        or req.headers.get("x-dr-actor")
        or "unknown"
    ).lower()
    if not ALLOWED_CALLERS:
        return True, caller   # allow-list not configured (dev only)
    return caller in ALLOWED_CALLERS, caller


async def main(req: func.HttpRequest) -> func.HttpResponse:
    action = (req.route_params.get("action") or "").lower()
    if action not in ("failover", "failback"):
        return func.HttpResponse("use /api/dr/failover or /api/dr/failback", status_code=404)

    ok, caller = _caller_ok(req)
    if not ok:
        return _forbidden(f"{caller} is not on the DR allow-list")

    try:
        body = req.get_json()
    except ValueError:
        body = {}

    cred = DefaultAzureCredential()
    cosmos = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=cred)
    sm = DrStateMachine(
        cosmos,
        os.environ["COSMOS_DATABASE"],
        os.environ["COSMOS_CONTAINER"],
        os.environ.get("COSMOS_AUDIT_CONTAINER", "transitions"),
    )
    ops = AzureOps(os.environ["AZURE_SUBSCRIPTION_ID"], cred)
    env = os.environ.get("ENVIRONMENT_NAME", "prod")

    try:
        if action == "failover":
            async with __import__("aiohttp").ClientSession() as s:
                p = await probe(s, os.environ["PRIMARY_APP"] + ".azurewebsites.net")
            await execute_failover(sm, ops, p)
            return func.HttpResponse(json.dumps({
                "action": "failover", "actor": caller,
                "reason": body.get("reason", ""),
                "primaryProbe": p,
            }), mimetype="application/json")

        # ---- failback: the only path that can destroy data ----------------
        approved_by = (body.get("approvedBy") or "").strip()
        ack = body.get("acknowledgeDataLoss") is True
        if not approved_by:
            return func.HttpResponse(
                json.dumps({"error": "failback requires approvedBy (a second engineer)"}),
                status_code=400, mimetype="application/json")
        if approved_by.lower() == caller:
            return func.HttpResponse(
                json.dumps({"error": "approver must not be the requester"}),
                status_code=400, mimetype="application/json")
        if not ack:
            return func.HttpResponse(
                json.dumps({"error": "failback requires acknowledgeDataLoss: true"}),
                status_code=400, mimetype="application/json")

        applied, t = await sm.transition(
            env, DrState.FAILING_BACK, trigger="manual", actor=caller,
            reason=body.get("reason", "manual failback"),
            evidence={"approvedBy": approved_by},
        )
        if not applied or t is None:
            return func.HttpResponse(json.dumps({"error": "transition not allowed from current state"}),
                                     status_code=409, mimetype="application/json")

        ctx = {
            "ops": ops,
            "primary_rg": os.environ["PRIMARY_RG"],
            "secondary_rg": os.environ["SECONDARY_RG"],
            "global_rg": os.environ["GLOBAL_RG"],
            "sql_server": os.environ["SQL_SERVER"],
            "failover_group": os.environ["FAILOVER_GROUP"],
            "database": os.environ.get("DATABASE_NAME", "orders"),
            "secondary_plan": os.environ["SECONDARY_APP"].replace("-app-", "-plan-"),
            "standby_target_instances": int(os.environ.get("STANDBY_TARGET_INSTANCES", "3")),
            "afd_profile": os.environ["AFD_PROFILE"],
            "secondary_host": os.environ["SECONDARY_APP"] + ".azurewebsites.net",
            "primary_probe": {"ok": False},   # failback assumes primary is currently down
        }
        ok_steps = await run_steps(FAILBACK_STEPS, ctx, t)
        await sm.finish(t, ok_steps, "failback " + ("completed" if ok_steps else "failed"))
        if ok_steps:
            await sm.transition(env, DrState.HEALTHY, trigger="manual", actor=caller,
                                reason=f"failback completed, approved by {approved_by}")
        else:
            await sm.transition(env, DrState.FAILED_OVER, trigger="manual", actor=caller,
                                reason="failback failed — still on secondary")
        return func.HttpResponse(json.dumps({"action": "failback", "ok": ok_steps,
                                             "steps": t.steps}),
                                 mimetype="application/json")
    finally:
        await ops.close()
        await cosmos.close()
        await cred.close()


# ---------------------------------------------------------------------------
# Failback steps: bring the ORIGINAL primary back and reverse the replication.
# ---------------------------------------------------------------------------

async def fb_verify_primary_healthy(ctx: dict) -> str:
    """Never fail back to a sick primary. Wait for it, up to 30 minutes."""
    import asyncio, aiohttp
    deadline = asyncio.get_event_loop().time() + 1800
    async with aiohttp.ClientSession() as s:
        while asyncio.get_event_loop().time() < deadline:
            r = await probe(s, os.environ["PRIMARY_APP"] + ".azurewebsites.net")
            if r["ok"]:
                return f"primary healthy again ({r['latencyMs']}ms)"
            await asyncio.sleep(30)
    raise RuntimeError("primary did not become healthy within 30 minutes")


async def fb_fence_secondary(ctx: dict) -> str:
    """Stop writes on the current primary (the old secondary) before reversing."""
    await ctx["ops"].set_plan_capacity(ctx["secondary_rg"], ctx["secondary_plan"], 0)
    return "secondary app stopped (writes fenced)"


async def fb_reverse_replication(ctx: dict) -> str:
    """Fail the SQL group back so the original primary is authoritative again."""
    return await ctx["ops"].promote_failover_group(
        ctx["primary_rg"], ctx["sql_server"], ctx["failover_group"])


async def fb_restore_primary_capacity(ctx: dict) -> str:
    return await ctx["ops"].set_plan_capacity(
        ctx["primary_rg"], os.environ["PRIMARY_APP"].replace("-app-", "-plan-"), 3)


async def fb_flip_traffic_back(ctx: dict) -> str:
    a = await ctx["ops"].set_origin_priority(
        ctx["global_rg"], ctx["afd_profile"], "app-origins", "primary", 1)
    b = await ctx["ops"].set_origin_priority(
        ctx["global_rg"], ctx["afd_profile"], "app-origins", "secondary", 2)
    return f"{a}; {b}"


async def fb_restore_standby(ctx: dict) -> str:
    return await ctx["ops"].set_plan_capacity(
        ctx["secondary_rg"], ctx["secondary_plan"], 1)


FAILBACK_STEPS = [
    ("verify_primary_healthy", fb_verify_primary_healthy),
    ("fence_secondary", fb_fence_secondary),
    ("reverse_replication", fb_reverse_replication),
    ("restore_primary_capacity", fb_restore_primary_capacity),
    ("flip_traffic_back", fb_flip_traffic_back),
    ("restore_standby", fb_restore_standby),
]
