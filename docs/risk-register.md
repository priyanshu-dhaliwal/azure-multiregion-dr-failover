# Risk register

Every architecture has risks. This one lists them, with an owner and a
mitigation — including the risks this design *introduces*.

| # | Risk | Likelihood | Impact | Mitigation | Accepted? |
|---|---|---|---|---|---|
| R1 | **Front Door is a global single point of failure.** Both regions sit behind one AFD profile. | Low | Critical | None technical. AFD has a strong SLA but it is Microsoft's control plane. Documented, monitored, and the status page is checked first in any incident. | Yes — accepted |
| R2 | **Split-brain during failover.** Primary is partitioned from the orchestrator but still serving writes. | Medium | Critical | `step_fence_writes` refuses to proceed if the primary responds; promotion only after fencing; SQL failover group has a single writable endpoint by construction. | Mitigated |
| R3 | **Data loss on failback.** Reverse replication drops writes made on the secondary. | Medium | Critical | Manual, two-person approval, explicit `acknowledgeDataLoss`, verified backup, maintenance window. | Mitigated, not eliminated |
| R4 | **Orchestrator fails during a failover.** Function times out mid-sequence. | Medium | High | Every transition is audited as `pending` before it runs; steps are idempotent; state lands in `FAILOVER_FAILED` which pages a human. | Mitigated |
| R5 | **Two orchestrators both fail over.** | Low | Critical | Cosmos DB optimistic concurrency on the state document (`_etag`); second writer gets 412 and exits. Unit-tested. | Mitigated |
| R6 | **Replication lag exceeds RPO.** | Medium | High | `step_check_replication` aborts the failover if lag > 300s and pages a human instead of silently losing data. | Mitigated |
| R7 | **Failover triggers on a transient blip.** | Medium | Medium | 3 consecutive probe failures + 60s grace + re-probe before acting. | Mitigated |
| R8 | **Failover triggers on latency, not availability.** | Low | High | Deliberately not implemented; availability only. Latency raises an alert. | Accepted by design |
| R9 | **Cost surprise from a forgotten failover.** Running on the secondary indefinitely at full size. | Medium | Medium | Budget alert on the secondary resource group; weekly state check (`failover.sh status`). | Mitigated |
| R10 | **The drill harness itself causes an outage.** A drill run against prod by mistake. | Medium | High | Drills require `FRONTDOOR_HOST` and named RG env vars; run against the `dev` environment by default; `--no-restore` is opt-in. | Partially mitigated |
| R11 | **Orchestrator RBAC too broad.** A DR tool that can do anything can cause the outage. | Low | High | Website Contributor + SQL DB Contributor + Monitoring Reader, scoped to the two app RGs. No subscription-level Contributor. | Mitigated |
| R12 | **Nobody runs the drill.** DR paths rot silently. | High | High | Monthly scheduled drill (see `.github/workflows/dr-drill.yml`); the drill writes to `docs/drills.md`, so the absence of recent rows is itself visible. | Mitigated |

## Risks consciously not mitigated

**R1 (AFD single point of failure).** The alternative is multi-CDN or
DNS-level failover, both of which add operational complexity that outweighs the
risk for this scale. Revisit if the SLA requirement goes above 99.99%.

**R8 (no latency failover).** Latency-based failover causes flapping and
cascading failures during regional congestion. Human decision is better here.
