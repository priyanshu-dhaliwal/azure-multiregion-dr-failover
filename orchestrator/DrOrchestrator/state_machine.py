"""
The DR state machine.

Design constraints, in order of importance:

1.  **Exactly one failover at a time.** Enforced by Cosmos DB optimistic
    concurrency on the state document (`_etag`). A second orchestrator instance
    gets a 412 PreconditionFailed and backs off. No distributed locks, no
    Redis, no "probably fine".

2.  **Every transition is recorded before it is executed.** The audit document
    is written with `outcome=pending` first, so a crash mid-failover leaves
    evidence of *what was being attempted*.

3.  **Transitions are idempotent.** Each step checks the current world state
    before acting (is the SQL group already promoted? is the standby already
    scaled?). Re-running a partially completed failover must be safe, because
    that is exactly what happens when a function times out.

4.  **Failback is never automatic.** It requires an explicit approver identity
    and a data-loss acknowledgement, because re-syncing backwards is the only
    step in the whole flow that can destroy data.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from azure.core import MatchConditions
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosAccessConditionFailedError

log = logging.getLogger("dr.state_machine")


class DrState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING_OVER = "FAILING_OVER"
    FAILED_OVER = "FAILED_OVER"
    FAILING_BACK = "FAILING_BACK"
    # Terminal-ish: failover attempted and failed; requires a human.
    FAILOVER_FAILED = "FAILOVER_FAILED"


# Allowed transitions. Anything not listed here is rejected — this table is the
# contract, and it is the first thing to read when debugging a stuck DR system.
ALLOWED: dict[DrState, set[DrState]] = {
    DrState.HEALTHY: {DrState.DEGRADED, DrState.FAILING_OVER},
    DrState.DEGRADED: {DrState.HEALTHY, DrState.FAILING_OVER},
    DrState.FAILING_OVER: {DrState.FAILED_OVER, DrState.FAILOVER_FAILED},
    DrState.FAILED_OVER: {DrState.FAILING_BACK},
    DrState.FAILING_BACK: {DrState.HEALTHY, DrState.FAILED_OVER},
    DrState.FAILOVER_FAILED: {DrState.FAILING_OVER, DrState.DEGRADED},
}


@dataclass
class Transition:
    environment: str
    from_state: DrState
    to_state: DrState
    trigger: str                     # "timer" | "manual" | "sql-auto"
    actor: str                       # "system" | user object id / upn
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    steps: list[dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str = "", **extra: Any) -> None:
        self.steps.append({
            "name": name,
            "ok": ok,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
            **extra,
        })
        log.info("step=%s ok=%s %s", name, ok, detail)


class DrStateMachine:
    def __init__(
        self,
        cosmos: CosmosClient,
        database: str,
        container: str,
        audit_container: str = "transitions",
        dry_run: bool = False,
    ) -> None:
        self._cosmos = cosmos
        self._db = database
        self._state = container
        self._audit = audit_container
        self._dry_run = dry_run

    # ------------------------------------------------------------------ state

    async def get_state(self, environment: str) -> dict[str, Any]:
        c = self._cosmos.get_database_client(self._db).get_container_client(self._state)
        try:
            return await c.read_item(item=environment, partition_key=environment)
        except Exception:  # item missing -> bootstrap
            doc = {
                "id": environment,
                "environment": environment,
                "state": DrState.HEALTHY.value,
                "consecutiveFailures": 0,
                "lastTransition": None,
            }
            await c.upsert_item(doc)
            return doc

    async def transition(
        self,
        environment: str,
        to_state: DrState,
        trigger: str,
        actor: str,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> tuple[bool, Transition | None]:
        """
        Attempt a transition. Returns (applied, transition).

        `applied=False` means either the transition is not allowed from the
        current state, or another instance won the race (412). Both are
        non-exceptional: the caller just does nothing this tick.
        """
        container = self._cosmos.get_database_client(self._db).get_container_client(self._state)
        current = await self.get_state(environment)
        from_state = DrState(current["state"])

        if to_state not in ALLOWED.get(from_state, set()):
            log.warning("rejected transition %s -> %s (%s)", from_state, to_state, trigger)
            return False, None

        t = Transition(
            environment=environment,
            from_state=from_state,
            to_state=to_state,
            trigger=trigger,
            actor=actor,
            reason=reason,
            evidence=evidence or {},
        )

        # Write the audit row FIRST, as `pending`. A crash leaves a trace.
        audit = self._cosmos.get_database_client(self._db).get_container_client(self._audit)
        pending = {
            "id": f"{environment}-{t.started_at.isoformat()}-{to_state.value}",
            "environment": environment,
            "fromState": from_state.value,
            "toState": to_state.value,
            "trigger": trigger,
            "actor": actor,
            "reason": reason,
            "evidence": evidence or {},
            "startedAt": t.started_at.isoformat(),
            "outcome": "pending",
            "steps": [],
        }
        await audit.upsert_item(pending)

        if self._dry_run:
            log.info("DRY_RUN: would transition %s -> %s", from_state, to_state)
            return True, t

        # Conditional replace: fails with 412 if another instance changed state.
        current["state"] = to_state.value
        current["lastTransition"] = {
            "at": t.started_at.isoformat(),
            "from": from_state.value,
            "to": to_state.value,
            "trigger": trigger,
            "actor": actor,
        }
        if to_state == DrState.HEALTHY:
            current["consecutiveFailures"] = 0

        try:
            await container.replace_item(
                item=environment,
                body=current,
                etag=current.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError:
            log.info("lost the race for %s -> %s; another instance is working", from_state, to_state)
            await audit.upsert_item({**pending, "outcome": "superseded"})
            return False, None

        return True, t

    async def finish(self, t: Transition, ok: bool, detail: str = "") -> None:
        """Close out the audit document for a transition."""
        audit = self._cosmos.get_database_client(self._db).get_container_client(self._audit)
        doc_id = f"{t.environment}-{t.started_at.isoformat()}-{t.to_state.value}"
        doc = await audit.read_item(item=doc_id, partition_key=t.environment)
        finished = datetime.now(timezone.utc)
        doc.update({
            "outcome": "succeeded" if ok else "failed",
            "detail": detail,
            "finishedAt": finished.isoformat(),
            "durationSeconds": round((finished - t.started_at).total_seconds(), 1),
            "steps": t.steps,
        })
        await audit.replace_item(item=doc_id, body=doc)

    async def record_failure(self, environment: str, failures: int) -> DrState:
        """Bump the consecutive-failure counter and return the implied state."""
        container = self._cosmos.get_database_client(self._db).get_container_client(self._state)
        doc = await self.get_state(environment)
        doc["consecutiveFailures"] = failures
        await container.upsert_item(doc)
        return DrState.DEGRADED if failures > 0 else DrState.HEALTHY


# ---------------------------------------------------------------------------
# The failover procedure itself, expressed as an ordered list of idempotent
# steps. The orchestrator executes these; the tests below exercise the ordering
# and the short-circuit behaviour without touching Azure.
# ---------------------------------------------------------------------------

StepFn = Callable[[dict[str, Any]], Awaitable[str]]


async def run_steps(steps: list[tuple[str, StepFn]], ctx: dict[str, Any], t: Transition) -> bool:
    """
    Run failover steps in order. Stop at the first failure — a half-failed
    failover is safer than a failover that skips a step because an earlier one
    broke (e.g. flipping traffic before the database is promoted = data loss).
    """
    for name, fn in steps:
        try:
            detail = await fn(ctx)
            t.step(name, True, detail)
        except Exception as exc:  # noqa: BLE001 - we deliberately continue to record
            t.step(name, False, f"{type(exc).__name__}: {exc}")
            return False
    return True
