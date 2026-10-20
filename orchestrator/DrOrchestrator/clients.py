"""
Thin, retrying wrappers around the Azure SDKs the orchestrator needs.

Everything here takes a credential (DefaultAzureCredential → managed identity)
and never a key or connection string. If a call needs a secret, that is a
design smell for this project.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.web.aio import WebSiteManagementClient
from azure.mgmt.sql.aio import SqlManagementClient
from azure.mgmt.cdn.aio import CdnManagementClient

log = logging.getLogger("dr.clients")

RETRYABLE = (asyncio.TimeoutError, ConnectionError)


async def with_retries(fn, *args, attempts: int = 4, base_delay: float = 1.0, **kwargs) -> Any:
    """Exponential backoff. Azure control plane is eventually consistent; a
    single 409 Conflict on a scale operation is normal, not a failure."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except RETRYABLE as exc:
            last = exc
            delay = base_delay * (2 ** i)
            log.warning("retry %d/%d after %.1fs: %s", i + 1, attempts, delay, exc)
            await asyncio.sleep(delay)
    assert last is not None
    raise last


class AzureOps:
    """All the Azure mutations the DR flow performs, in one place."""

    def __init__(self, subscription_id: str, credential: DefaultAzureCredential | None = None):
        self._sub = subscription_id
        self._cred = credential or DefaultAzureCredential()
        self._web = WebSiteManagementClient(self._cred, subscription_id)
        self._sql = SqlManagementClient(self._cred, subscription_id)
        self._cdn = CdnManagementClient(self._cred, subscription_id)

    async def close(self) -> None:
        for c in (self._web, self._sql, self._cdn):
            await c.close()
        await self._cred.close()

    # ---- App Service -----------------------------------------------------

    async def get_app_state(self, rg: str, name: str) -> str:
        app = await with_retries(self._web.web_apps.get, rg, name)
        return app.state  # "Running" | "Stopped" | ...

    async def set_plan_capacity(self, rg: str, plan: str, instances: int) -> str:
        """Scale an App Service plan. Idempotent: setting the same capacity is a no-op."""
        plan_obj = await with_retries(self._web.app_service_plans.get, rg, plan)
        current = plan_obj.sku.capacity if plan_obj.sku else 1
        if current == instances:
            return f"already at {instances} instances"
        plan_obj.sku.capacity = instances
        await with_retries(
            self._web.app_service_plans.begin_create_or_update, rg, plan, plan_obj
        )
        return f"scaled {current} -> {instances}"

    async def restart_app(self, rg: str, name: str) -> str:
        await with_retries(self._web.web_apps.restart, rg, name)
        return "restarted"

    # ---- SQL -------------------------------------------------------------

    async def failover_group_status(self, rg: str, server: str, group: str) -> dict[str, Any]:
        fg = await with_retries(self._sql.failover_groups.get, rg, server, group)
        return {
            "role": fg.replication_role,               # Primary | Secondary
            "partner": fg.partner_servers[0].id if fg.partner_servers else None,
            "readWriteEndpoint": fg.read_write_endpoint.failover_policy,
        }

    async def promote_failover_group(self, rg: str, server: str, group: str) -> str:
        """Force failover of the group. Idempotent-ish: if already Primary, skip."""
        status = await self.failover_group_status(rg, server, group)
        if status["role"] == "Primary":
            return "already primary — skipped"
        await with_retries(self._sql.failover_groups.begin_failover, rg, server, group)
        return "promoted to primary"

    async def replication_lag_seconds(self, rg: str, server: str, db: str) -> float:
        """Read replication lag so we can record the RPO gap at promotion time."""
        metrics = self._sql.databases.list_metrics(
            rg, server, db,
            filter="name/value eq 'replication_lag'",
        )
        async for m in metrics:
            for ts in m.timeseries:
                for point in ts.data:
                    if point.average is not None:
                        return float(point.average)
        return 0.0

    # ---- Front Door ------------------------------------------------------

    async def set_origin_priority(self, rg: str, profile: str, origin_group: str,
                                  origin: str, priority: int) -> str:
        origin_obj = await with_retries(
            self._cdn.origins.get, rg, profile, origin_group, origin
        )
        if origin_obj.priority == priority:
            return f"{origin} already priority {priority}"
        origin_obj.priority = priority
        await with_retries(
            self._cdn.origins.begin_update, rg, profile, origin_group, origin, origin_obj
        )
        return f"{origin} -> priority {priority}"
