#!/usr/bin/env bash
# Manual DR operations. Same code path as the automatic orchestrator, so a
# drill proves the incident path.
#
#   ./scripts/failover.sh status
#   ./scripts/failover.sh failover --reason "planned drill"
#   ./scripts/failover.sh failback --approver second@example.com --ack
set -euo pipefail

FUNC_APP="${FUNC_APP:?set FUNC_APP to the orchestrator function app name}"
FUNC_KEY="${FUNC_KEY:?set FUNC_KEY to the function key (az functionapp keys list)}"
ACTOR="${ACTOR:-$(az ad signed-in-user show --query userPrincipalName -o tsv 2>/dev/null || echo unknown)}"

ACTION="${1:?usage: failover.sh <status|failover|failback> [options]}"
shift || true

REASON="manual"
APPROVER=""
ACK="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --reason)   REASON="$2"; shift 2 ;;
    --approver) APPROVER="$2"; shift 2 ;;
    --ack)      ACK="true"; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

BASE="https://${FUNC_APP}.azurewebsites.net/api/dr"

case "$ACTION" in
  status)
    az cosmosdb sql query \
      --account-name "$(echo "$FUNC_APP" | sed 's/-orchestrator$/-drstate/; s/-//g')" \
      --database-name dr --container-name state \
      --query-text "SELECT * FROM c" -o table 2>/dev/null \
      || echo "state store not reachable via CLI; check the Cosmos account name"
    ;;

  failover)
    echo "==> manual failover as $ACTOR — $REASON"
    curl -sS -X POST "${BASE}/failover" \
      -H "x-functions-key: ${FUNC_KEY}" \
      -H "x-dr-actor: ${ACTOR}" \
      -H "Content-Type: application/json" \
      -d "{\"reason\": \"${REASON}\"}" | python -m json.tool
    ;;

  failback)
    if [ -z "$APPROVER" ]; then
      echo "failback requires --approver <second-engineer>" >&2
      exit 1
    fi
    if [ "$ACK" != "true" ]; then
      echo "failback requires --ack (acknowledges possible data loss)" >&2
      exit 1
    fi
    echo "==> failback as $ACTOR, approved by $APPROVER"
    curl -sS -X POST "${BASE}/failback" \
      -H "x-functions-key: ${FUNC_KEY}" \
      -H "x-dr-actor: ${ACTOR}" \
      -H "Content-Type: application/json" \
      -d "{\"reason\": \"${REASON}\", \"approvedBy\": \"${APPROVER}\", \"acknowledgeDataLoss\": true}" \
      | python -m json.tool
    ;;

  *)
    echo "unknown action: $ACTION" >&2
    exit 1
    ;;
esac
