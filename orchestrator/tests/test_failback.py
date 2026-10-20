"""
Failback is the dangerous half of DR. These tests pin down its guard rails:
no self-approval, explicit data-loss acknowledgement, and a mandatory health
check on the original primary before anything is touched.

The heavy Azure imports live inside `failover.py`, so this module stubs the
Azure SDKs it touches at import time — the tests are about ordering and
authorisation logic, not SDK behaviour.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_failback_has_health_gate_first():
    from DrOrchestrator.failover import FAILBACK_STEPS
    names = [n for n, _ in FAILBACK_STEPS]
    assert names[0] == "verify_primary_healthy", (
        "failback must verify the primary is healthy BEFORE fencing the secondary"
    )
    assert names.index("fence_secondary") < names.index("reverse_replication"), (
        "must stop writes before reversing replication, or you get split-brain"
    )
    assert names.index("reverse_replication") < names.index("flip_traffic_back"), (
        "traffic must only move back after the original primary owns the data again"
    )
    assert names[-1] == "restore_standby", "standby must end up cheap again"


def test_failover_orders_fence_before_promote_before_traffic():
    from DrOrchestrator.timer import FAILOVER_STEPS
    names = [n for n, _ in FAILOVER_STEPS]
    assert names.index("fence_writes") < names.index("check_replication") < names.index("promote_sql")
    assert names.index("promote_sql") < names.index("flip_traffic"), (
        "traffic must never move to a database that is not yet primary"
    )
    assert names.index("verify_secondary") == len(names) - 1


def test_failback_requires_an_approver_and_acknowledgement():
    """The handler must reject failback without a second human and an explicit
    data-loss acknowledgement. Asserted on the source so the contract cannot be
    dropped without a test failing."""
    from DrOrchestrator import failover as mod
    src = inspect.getsource(mod.main)
    assert "approvedBy" in src
    assert "acknowledgeDataLoss" in src
    assert "approver must not be the requester" in src


def test_failover_refuses_when_primary_still_reachable():
    """Fencing a reachable primary means the outage was in DNS/AFD, not the app.
    Failover must stop and escalate instead of cutting over a healthy region."""
    from DrOrchestrator.timer import step_fence_writes

    with pytest.raises(RuntimeError, match="refusing to fence"):
        asyncio.run(step_fence_writes({"primary_probe": {"ok": True}}))

    msg = asyncio.run(step_fence_writes({"primary_probe": {"ok": False}}))
    assert "unreachable" in msg


def test_replication_lag_guard_blocks_failover():
    """A failover with unacceptable RPO must abort before promotion, not after."""
    from DrOrchestrator.timer import step_check_replication

    class SlowOps:
        async def replication_lag_seconds(self, *a):
            return 900.0

    with pytest.raises(RuntimeError, match="RPO would be unacceptable"):
        asyncio.run(step_check_replication({
            "ops": SlowOps(), "primary_rg": "rg", "sql_server": "s", "database": "db",
        }))


def test_replication_lag_within_budget_passes():
    from DrOrchestrator.timer import step_check_replication

    class FastOps:
        async def replication_lag_seconds(self, *a):
            return 4.2

    ctx = {"ops": FastOps(), "primary_rg": "rg", "sql_server": "s", "database": "db"}
    msg = asyncio.run(step_check_replication(ctx))
    assert "4.2s" in msg
    assert ctx["replication_lag_seconds"] == 4.2
