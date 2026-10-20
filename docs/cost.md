# Cost model — Multi-Region DR

Estimates in **INR/month** for `centralindia` + `southindia` + `westeurope`
(orchestrator), list prices at time of writing.

## Steady state (primary serving, secondary warm)

| Resource | Config | Monthly |
|---|---|---|
| Front Door Premium | ~50M requests, 20 GB egress, WAF | ~₹40,000 |
| Primary App Service plan | P1v3 × 3 | ~₹24,000 |
| Secondary App Service plan | P1v3 × 1 (warm standby) | ~₹8,000 |
| Azure SQL | S1 primary + geo-secondary (billed at primary rate) | ~₹12,000 |
| Storage | 2 × Standard_GRS 100 GB | ~₹3,500 |
| Key Vault | 2 vaults, low ops | ~₹500 |
| Cosmos DB (state) | 3 containers × 400 RU/s | ~₹5,500 |
| Function App | Flex Consumption, ~40k executions/mo | ~₹300 |
| Log Analytics | ~10 GB/mo | ~₹2,000 |
| Bandwidth | inter-region replication + egress | ~₹3,000 |
| **Total** | | **~₹99,000** |

## During a failover (secondary at full size)

| Delta | Monthly equivalent |
|---|---|
| Secondary scaled 1 → 3 instances | +₹16,000 |
| Primary at 0 (if stopped) | −₹24,000 |
| **Net while failed over** | **~−₹8,000 (cheaper)** |

Failing over is *cheaper* than steady state because the primary is stopped.
This is worth knowing: it removes "cost" as an excuse for failing back too
early.

## How to cut this in half

1. **Drop Front Door Premium → Standard** (~₹40k → ~₹9k). You lose WAF and
   private origins. For a DR showcase this is fine; for production, it is not.
2. **Replace Azure SQL with Cosmos DB single-write, multi-region read.** Removes
   the geo-secondary bill (~₹12k) and the RPO conversation entirely. Requires
   data modelling work — usually the right long-term answer.
3. **Standby at 0 instances with a "cold start on failover" trade.** Saves
   ₹8k/month and adds ~3 minutes to RTO. Only acceptable if your RTO target is
   10 minutes or more.
4. **Share one Log Analytics workspace across regions** instead of two
   (already done here).

## The number that actually matters

**Cost per minute of avoided downtime.** At ₹99,000/month, the DR topology
costs ~₹2.25 per minute. If one avoided hour of total outage saves more than
₹135 (it almost always does — revenue, SLA credits, trust), the topology pays
for itself in the first incident. Write that number down; it is the argument
you will need when someone asks why the second region exists.
