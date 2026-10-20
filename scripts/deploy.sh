#!/usr/bin/env bash
# Deploy both regions, Front Door, state store and orchestrator.
set -euo pipefail

ENV="${1:-prod}"
MODE="${2:-deploy}"
LOCATION="${LOCATION:-centralindia}"
PARAMS="infra/params/${ENV}.bicepparam"

[ -f "$PARAMS" ] || { echo "missing $PARAMS" >&2; exit 1; }

echo "==> subscription: $(az account show --query name -o tsv)"
az bicep build --file infra/main.bicep --stdout > /dev/null
echo "==> bicep ok"

if [ "$MODE" = "what-if" ]; then
  az deployment sub what-if --name "dr-${ENV}-$(date +%s)" -l "$LOCATION" \
    -f infra/main.bicep -p "$PARAMS" --result-format FullResourcePayloads
else
  az deployment sub create --name "dr-${ENV}-$(date +%s)" -l "$LOCATION" \
    -f infra/main.bicep -p "$PARAMS" -o table

  echo "==> publishing orchestrator function code"
  FUNC_APP=$(az deployment sub show -n "$(az deployment sub list --query '[0].name' -o tsv)" \
    --query properties.outputs.orchestratorName.value -o tsv)
  (cd orchestrator && func azure functionapp publish "$FUNC_APP" --python)
fi
