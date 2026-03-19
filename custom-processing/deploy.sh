#!/usr/bin/env bash
# =============================================================================
# AI Foundry Document Ingestion Pipeline — Custom Processing Deployment
# =============================================================================
# Deploys the Custom Processing Function App:
#   - Creates Function App + storage account
#   - Assigns Managed Identity + RBAC roles
#   - Configures app settings from .env (single source of truth)
#   - Publishes function code
#
# All values come from .env — no hardcoded defaults in this script.
# Parent orchestrator (../deploy.sh) can override via exported env vars.
#
# Prerequisites:
#   - .env file populated (copy .env.example if needed)
#   - Shared infrastructure must exist
#   - Azure CLI authenticated, Functions Core Tools installed
#
# Usage (standalone):
#   chmod +x deploy.sh
#   ./deploy.sh                                # deploy with .env values
#   TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh   # override trigger mode
#
# Usage (via orchestrator):
#   Called automatically by ../deploy.sh (which exports override values)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ---------------------------------------------------------------------------
# Load .env — single source of truth for all values
# ---------------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env file not found at $ENV_FILE"
  echo "  -> Copy .env.example to .env and fill in values"
  exit 1
fi

# Parse .env: set variables only if NOT already in environment.
while IFS= read -r line || [ -n "$line" ]; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  line="${line%%#*}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  key="${line%%=*}"
  val="${line#*=}"
  [[ "$key" == *.* ]] && continue
  if [ -z "${!key+x}" ]; then
    declare "$key=$val"
  fi
done < "$ENV_FILE"

# ---------------------------------------------------------------------------
# Map variables
# ---------------------------------------------------------------------------
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$DEPLOY_SUBSCRIPTION_ID}"
LOCATION="${LOCATION:-$DEPLOY_LOCATION}"
RG_NAME="${RG_NAME:-$DEPLOY_RG_NAME}"
FUNC_CUSTOM_APP="${FUNC_CUSTOM_APP:-$DEPLOY_FUNC_APP_NAME}"
FUNC_CUSTOM_STORAGE="${FUNC_CUSTOM_STORAGE:-$DEPLOY_FUNC_STORAGE_ACCOUNT}"
IS_ACTIVE="${IS_ACTIVE:-${DEPLOY_IS_ACTIVE:-true}}"
SEARCH_SERVICE="${SEARCH_SERVICE:-$DEPLOY_SEARCH_SERVICE}"
SEARCH_RG="${SEARCH_RG:-$DEPLOY_SEARCH_RG}"
FOUNDRY_ACCOUNT="${FOUNDRY_ACCOUNT:-$DEPLOY_FOUNDRY_ACCOUNT}"
FOUNDRY_RG="${FOUNDRY_RG:-$DEPLOY_FOUNDRY_RG}"

# App Insights — optional. If not set, Function App is created without it.
APP_INSIGHTS="${APP_INSIGHTS:-${DEPLOY_APP_INSIGHTS:-}}"

ADLS_ACCOUNT="${ADLS_ACCOUNT:-$ADLS_ACCOUNT_NAME}"
SEARCH_INDEX="${SEARCH_INDEX:-$SEARCH_INDEX_NAME}"

# Default to BLOB — simplest mode, no Event Grid or Queue infra needed
TRIGGER_MODE="${TRIGGER_MODE:-BLOB}"

echo "=============================================="
echo "Custom Processing — Deployment Starting"
echo "=============================================="
echo "Function App: $FUNC_CUSTOM_APP"
echo "Active:       $IS_ACTIVE"
echo "Trigger mode: $TRIGGER_MODE"
if [ -n "$APP_INSIGHTS" ]; then
  echo "App Insights: $APP_INSIGHTS"
else
  echo "App Insights: (none — monitoring disabled)"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 1: Create Function App + Storage
# ---------------------------------------------------------------------------
echo "[Step 1/5] Creating Function App: $FUNC_CUSTOM_APP"

echo "  -> Creating storage account: $FUNC_CUSTOM_STORAGE"
az storage account create \
  --name "$FUNC_CUSTOM_STORAGE" \
  --resource-group "$RG_NAME" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --tags Environment=dev Purpose="FuncCustomProcessingStorage" \
  -o none

# Build Function App create command — conditionally include --app-insights
FUNC_CREATE_ARGS=(
  --name "$FUNC_CUSTOM_APP"
  --resource-group "$RG_NAME"
  --storage-account "$FUNC_CUSTOM_STORAGE"
  --runtime python
  --runtime-version 3.11
  --functions-version 4
  --os-type Linux
  --consumption-plan-location "$LOCATION"
  --https-only true
  --tags Environment=dev Purpose="CustomLibrariesDocProcessing"
  -o none
)
if [ -n "$APP_INSIGHTS" ]; then
  FUNC_CREATE_ARGS+=(--app-insights "$APP_INSIGHTS")
fi

echo "  -> Creating Function App: $FUNC_CUSTOM_APP"
az functionapp create "${FUNC_CREATE_ARGS[@]}"
echo "  -> $FUNC_CUSTOM_APP created."

# ---------------------------------------------------------------------------
# Step 2: Enable Managed Identity + RBAC
# ---------------------------------------------------------------------------
echo "[Step 2/5] Enabling Managed Identity and assigning RBAC"

CUSTOM_PRINCIPAL_ID=$(az functionapp identity assign \
  --name "$FUNC_CUSTOM_APP" \
  --resource-group "$RG_NAME" \
  --query principalId -o tsv)
echo "  -> Principal ID: $CUSTOM_PRINCIPAL_ID"

echo "  -> Waiting 30s for AAD propagation..."
sleep 30

echo "  -> Assigning RBAC roles..."

# 1. Storage Blob Data Contributor on ADLS
az role assignment create \
  --assignee "$CUSTOM_PRINCIPAL_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG_NAME/providers/Microsoft.Storage/storageAccounts/$ADLS_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 2. Cognitive Services User on Foundry (embeddings only)
az role assignment create \
  --assignee "$CUSTOM_PRINCIPAL_ID" \
  --role "Cognitive Services User" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 3. Search Index Data Contributor on AI Search
az role assignment create \
  --assignee "$CUSTOM_PRINCIPAL_ID" \
  --role "Search Index Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$SEARCH_RG/providers/Microsoft.Search/searchServices/$SEARCH_SERVICE" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 4. Storage Queue Data Contributor on ADLS (for queue-based triggering)
az role assignment create \
  --assignee "$CUSTOM_PRINCIPAL_ID" \
  --role "Storage Queue Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG_NAME/providers/Microsoft.Storage/storageAccounts/$ADLS_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

echo "  -> RBAC assignments complete."

# ---------------------------------------------------------------------------
# Step 3: Configure App Settings (from .env, excluding DEPLOY_*)
# ---------------------------------------------------------------------------
echo "[Step 3/5] Configuring app settings from .env"

declare -a ENV_SETTINGS=()
while IFS= read -r line || [ -n "$line" ]; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  if [[ "$line" =~ ^([^#]*[^[:space:]])([[:space:]]+#.*)?$ ]]; then
    line="${BASH_REMATCH[1]}"
  fi
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  [[ "$line" =~ ^DEPLOY_ ]] && continue
  ENV_SETTINGS+=("$line")
done < "$ENV_FILE"

# Trigger enable/disable based on IS_ACTIVE and TRIGGER_MODE
if [ "$IS_ACTIVE" = "true" ]; then
  CUSTOM_EG_DISABLED="true"
  CUSTOM_QUEUE_DISABLED="true"
  CUSTOM_BLOB_DISABLED="true"

  if [ "$TRIGGER_MODE" = "EVENTGRID_QUEUE" ]; then
    CUSTOM_QUEUE_DISABLED="false"
  elif [ "$TRIGGER_MODE" = "EVENTGRID_DIRECT" ]; then
    CUSTOM_EG_DISABLED="false"
  elif [ "$TRIGGER_MODE" = "BLOB" ]; then
    CUSTOM_BLOB_DISABLED="false"
  fi
else
  CUSTOM_EG_DISABLED="true"
  CUSTOM_QUEUE_DISABLED="true"
  CUSTOM_BLOB_DISABLED="true"
fi

az functionapp config appsettings set \
  --name "$FUNC_CUSTOM_APP" \
  --resource-group "$RG_NAME" \
  --settings \
    "${ENV_SETTINGS[@]}" \
    "TRIGGER_MODE=$TRIGGER_MODE" \
    "AzureWebJobs.process_new_document.Disabled=$CUSTOM_EG_DISABLED" \
    "AzureWebJobs.process_queue_document.Disabled=$CUSTOM_QUEUE_DISABLED" \
    "AzureWebJobs.process_blob_document.Disabled=$CUSTOM_BLOB_DISABLED" \
  -o none

echo "  -> App settings configured from $ENV_FILE (DEPLOY_* excluded)"

# ---------------------------------------------------------------------------
# Step 4: Deploy Function App Code
# ---------------------------------------------------------------------------
echo "[Step 4/5] Deploying function code"

if command -v func &> /dev/null; then
  cd "$SCRIPT_DIR"
  echo "  -> Publishing $FUNC_CUSTOM_APP..."
  func azure functionapp publish "$FUNC_CUSTOM_APP" --python
  echo "  -> $FUNC_CUSTOM_APP deployed."
else
  echo "  WARNING: Azure Functions Core Tools (func) not installed."
  echo "  -> Skipping deployment. Run manually:"
  echo "     cd $SCRIPT_DIR && func azure functionapp publish $FUNC_CUSTOM_APP --python"
fi

# ---------------------------------------------------------------------------
# Step 5: Verify + Restart
# ---------------------------------------------------------------------------
echo "[Step 5/5] Verifying deployment"

FUNC_CUSTOM_STATE=$(az functionapp show --name "$FUNC_CUSTOM_APP" --resource-group "$RG_NAME" --query "state" -o tsv 2>/dev/null || echo "NOT FOUND")
echo "  -> Function App state: $FUNC_CUSTOM_STATE"

az functionapp restart --name "$FUNC_CUSTOM_APP" --resource-group "$RG_NAME" -o none 2>/dev/null || true
echo "  -> Function App restarted."

echo ""
echo "=============================================="
echo "Custom Processing — Deployment Complete"
echo "=============================================="
echo "  Function App: $FUNC_CUSTOM_APP"
echo "  Storage:      $FUNC_CUSTOM_STORAGE"
echo "  Health:       https://$FUNC_CUSTOM_APP.azurewebsites.net/api/health"
echo "  Logs:         func azure functionapp logstream $FUNC_CUSTOM_APP"
echo "  Active:       $IS_ACTIVE"
echo "  Trigger:      $TRIGGER_MODE"
echo ""
