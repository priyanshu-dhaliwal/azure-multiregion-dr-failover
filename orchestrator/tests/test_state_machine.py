"""
Tests for the DR state machine — the parts that decide whether you fail over,
and refuse the transitions that would cause a data-loss failback or a
double-failover.

These run without Azure: the Cosmos container is a fake that implements the
etag precondition, which is the behaviour the correctness argument rests on.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from DrOrchestrator.state_machine import DrState, DrStateMachine, ALLOWED, run_steps, Transition


# ---------------------------------------------------------------------------
# Fake Cosmos
# ---------------------------------------------------------------------------

class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._etag = 0

    async def read_item(self, item, partition_key):
        if item not in self.items:
            raise KeyError(item)
        return copy.deepcopy(self.items[item])

    async def upsert_item(self, body):
        self._etag += 1
        body = copy.deepcopy(body)
        body["_etag"] = str(self._etag)
        self.items[body["id"]] = body
        return body

    async def replace_item(self, item, body, etag=None, match_condition=None):
        current = self.items.get(item)
        if current is None:
            raise KeyError(item)
        if etag is not None and current.get("_etag") != etag:
            from azure.cosmos.exceptions import CosmosAccessConditionFailedError
            raise CosmosAccessConditionFailedError("etag mismatch")
        self._etag += 1
        body = copy.deepcopy(body)
        body["_etag"] = str(self._etag)
        self.items[item] = body
        return body


class FakeCosmos:
    def __init__(self) -> None:
        self.containers = {"state": FakeContainer(), "transitions": FakeContainer()}

    def get_database_client(self, _):
        return self

    def get_container_client(self, name):
        return self.containers[name]


@pytest.fixture
def sm():
    return DrStateMachine(FakeCosmos(), "dr", "state", "transitions")


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

def test_healthy_to_failing_over_allowed(sm):
    applied, t = asyncio.run(
        sm.transition("prod", DrState.FAILING_OVER, "timer", "system", "probe failed"))
    assert applied and t is not None
    assert asyncio.run(sm.get_state("prod"))["state"] == DrState.FAILING_OVER.value


def test_cannot_skip_straight_to_healthy_after_failover(sm):
    """FAILED_OVER -> HEALTHY must go through FAILING_BACK. This is the guard
    that stops a failback from happening implicitly."""
    asyncio.run(sm.transition("prod", DrState.FAILING_OVER, "timer", "system"))
    asyncio.run(sm.transition("prod", DrState.FAILED_OVER, "timer", "system"))
    applied, _ = asyncio.run(
        sm.transition("prod", DrState.HEALTHY, "manual", "someone", "just heal it"))
    assert applied is False


def test_cannot_failover_twice(sm):
    asyncio.run(sm.transition("prod", DrState.FAILING_OVER, "timer", "system"))
    asyncio.run(sm.transition("prod", DrState.FAILED_OVER, "timer", "system"))
    applied, _ = asyncio.run(
        sm.transition("prod", DrState.FAILING_OVER, "timer", "system", "again"))
    assert applied is False


def test_all_failover_paths_terminate_in_known_states():
    """Every state must have at least one outgoing edge, and FAILED_OVER must be
    reachable from HEALTHY. A state with no exit is a stuck DR system."""
    reachable = {DrState.HEALTHY}
    changed = True
    while changed:
        changed = False
        for src in list(reachable):
            for dst in ALLOWED.get(src, set()):
                if dst not in reachable:
                    reachable.add(dst)
                    changed = True
    assert DrState.FAILED_OVER in reachable
    assert DrState.FAILOVER_FAILED in reachable
    for state in DrState:
        assert state in reachable, f"{state} is unreachable — dead code in the table"
        assert ALLOWED.get(state), f"{state} has no outgoing transition — would stick"


# ---------------------------------------------------------------------------
# Concurrency: the etag guard
# ---------------------------------------------------------------------------

def test_two_orchestrators_cannot_both_fail_over(sm):
    """The core safety property: N instances, exactly one failover."""
    async def race():
        a, b = await asyncio.gather(
            sm.transition("prod", DrState.FAILING_OVER, "timer", "system-a"),
            sm.transition("prod", DrState.FAILING_OVER, "timer", "system-b"),
        )
        return a[0], b[0]

    results = asyncio.run(race())
    assert sum(results) == 1, f"expected exactly one winner, got {results}"


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------

def test_steps_stop_at_first_failure():
    """Flipping traffic (step 4) must NOT run if promotion (step 2) failed.
    This is the ordering guarantee that prevents data loss."""
    called: list[str] = []

    async def ok(ctx):
        called.append("ok")
        return "fine"

    async def boom(ctx):
        called.append("boom")
        raise RuntimeError("sql promote failed")

    async def never(ctx):
        called.append("never")
        return "should not run"

    t = Transition("prod", DrState.HEALTHY, DrState.FAILING_OVER, "timer", "system")
    ok_all = asyncio.run(run_steps([("a", ok), ("b", boom), ("c", never)], {}, t))
    assert ok_all is False
    assert called == ["ok", "boom"]
    assert t.steps[-1]["ok"] is False
    assert "sql promote failed" in t.steps[-1]["detail"]


def test_audit_written_before_transition_commits(sm):
    """A crash mid-failover must leave a `pending` audit row behind."""
    asyncio.run(sm.transition("prod", DrState.FAILING_OVER, "timer", "system"))
    rows = list(sm._cosmos.containers["transitions"].items.values())
    assert len(rows) == 1
    assert rows[0]["outcome"] == "pending"


def test_dry_run_does_not_move_state():
    cosm = FakeCosmos()
    sm = DrStateMachine(cosm, "dr", "state", "transitions", dry_run=True)
    applied, t = asyncio.run(
        sm.transition("prod", DrState.FAILING_OVER, "timer", "system"))
    assert applied and t is not None
    assert asyncio.run(sm.get_state("prod"))["state"] == DrState.HEALTHY.value
