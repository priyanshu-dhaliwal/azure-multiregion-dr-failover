# Runbook: failback

Failback is the only DR operation that can **destroy data**. Everything here
assumes that and is written to be slow on purpose.

## Preconditions — all of them, no exceptions

- [ ] Primary region has been healthy for **≥ 30 minutes** (the orchestrator
      checks this; do not skip it manually)
- [ ] An incident record exists with a named incident commander
- [ ] A second engineer is available to approve (`approvedBy`)
- [ ] You can state, out loud, the RPO window you are accepting:
      *"we accept losing up to N minutes of writes that happened on the
      secondary after this point"*
- [ ] A backup/export of the secondary database exists and its restore has been
      verified (not just taken)
- [ ] Traffic volume is currently low, or you have scheduled a maintenance
      window

If any box is unchecked, **do not fail back today.** Running on the secondary
for another day costs money; a botched failback costs the company.

## Procedure

### 1. Freeze writes on the secondary (fence first, always)

```bash
az webapp stop -g <secondary-rg> -n <secondary-app>
az sql failover-group show -g <primary-rg> --server <sql> -n <fog> \
  --query "{role:replicationRole, partner:partnerServers[0].id}"
```

Confirm the group reports `role: Secondary` (the secondary is currently the
primary). Users will see errors from this moment — that is expected and is why
this is done inside a window.

### 2. Verify the primary is actually healthy

```bash
curl -s https://<primary-app>.azurewebsites.net/health | jq .
# must be {"status":"ok","role":"primary",...}
```

Do not proceed on a 200 alone — check the instance count and that the app can
reach the database:

```bash
az webapp show -g <primary-rg> -n <primary-app> --query state
az webapp log tail -g <primary-rg> -n <primary-app>
```

### 3. Reverse replication

```bash
az sql failover-group set-primary -g <primary-rg> --server <sql> -n <fog>
```

Watch the operation:

```bash
az sql failover-group show -g <primary-rg> --server <sql> -n <fog> \
  --query replicationRole   # expect "Primary"
```

### 4. Restore the primary app tier

```bash
az appservice plan update -g <primary-rg> -n <primary-plan> --number-of-workers 3
az webapp start -g <primary-rg> -n <primary-app>
```

### 5. Flip traffic back at the edge

```bash
az afd origin update -g <global-rg> --profile-name <afd> --origin-group app-origins \
  --origin primary --priority 1
az afd origin update -g <global-rg> --profile-name <afd> --origin-group app-origins \
  --origin secondary --priority 2
```

### 6. Verify, then restore the standby

```bash
for i in 1 2 3 4 5; do
  curl -s https://<afd-endpoint>/health | jq -r .role   # expect "primary"
  sleep 5
done
az appservice plan update -g <secondary-rg> -n <secondary-plan> --number-of-workers 1
```

### 7. Write the post-incident review

Answer, in writing:

1. What was the actual RPO? (rows lost, if any — count them, do not estimate)
2. What was the actual RTO? (from `docs/drills.md` and the orchestrator's
   `transitions` container)
3. Did the automatic failover trigger, or did a human? Why?
4. Which step was slowest, and is that step necessary?
5. What would have made this failback unnecessary?

## Anti-patterns

**"Fail back as soon as the primary is up."** No. The primary being *up* is not
the same as the primary being *correct*. Give it 30 minutes of clean operation.

**"Fail back during peak because it is faster."** It is not faster at peak;
scale operations and replication catch-up are slower under load, and the blast
radius of a mistake is larger.

**"Let the orchestrator fail back automatically."** It deliberately cannot. The
absence of that feature is a design decision, documented in the README.
