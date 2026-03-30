#!/usr/bin/env bash
# =============================================================================
# AI Foundry Document Ingestion Pipeline — AI Foundry Processing Deployment
# =============================================================================
# Deploys the AI Foundry Processing Function App:
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
#   - Shared infrastructure must exist (run ../deploy.sh first)
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
# This allows parent orchestrator exports and CLI overrides to take precedence.
while IFS= read -r line || [ -n "$line" ]; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  line="${line%%#*}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  key="${line%%=*}"
  val="${line#*=}"
  # Skip keys with dots (e.g. AzureWebJobs.*.Disabled) — not valid bash identifiers.
  # These are app settings only, handled by Step 3's ENV_SETTINGS loop.
  [[ "$key" == *.* ]] && continue
  # Only set if not already exported by parent/CLI
  if [ -z "${!key+x}" ]; then
    declare "$key=$val"
  fi
done < "$ENV_FILE"

# ---------------------------------------------------------------------------
# Map variables — DEPLOY_* for infra, app settings for shared values
# ---------------------------------------------------------------------------
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$DEPLOY_SUBSCRIPTION_ID}"
LOCATION="${LOCATION:-$DEPLOY_LOCATION}"
RG_NAME="${RG_NAME:-$DEPLOY_RG_NAME}"
FUNC_FOUNDRY_APP="${FUNC_FOUNDRY_APP:-$DEPLOY_FUNC_APP_NAME}"
FUNC_FOUNDRY_STORAGE="${FUNC_FOUNDRY_STORAGE:-$DEPLOY_FUNC_STORAGE_ACCOUNT}"
IS_ACTIVE="${IS_ACTIVE:-${DEPLOY_IS_ACTIVE:-true}}"
SEARCH_SERVICE="${SEARCH_SERVICE:-$DEPLOY_SEARCH_SERVICE}"
SEARCH_RG="${SEARCH_RG:-$DEPLOY_SEARCH_RG}"
FOUNDRY_ACCOUNT="${FOUNDRY_ACCOUNT:-$DEPLOY_FOUNDRY_ACCOUNT}"
FOUNDRY_RG="${FOUNDRY_RG:-$DEPLOY_FOUNDRY_RG}"

# App Insights — optional. If not set, Function App is created without it.
APP_INSIGHTS="${APP_INSIGHTS:-${DEPLOY_APP_INSIGHTS:-}}"

# Shared with app settings (from .env, overridable by parent exports)
ADLS_ACCOUNT="${ADLS_ACCOUNT:-$ADLS_ACCOUNT_NAME}"
SEARCH_INDEX="${SEARCH_INDEX:-$SEARCH_INDEX_NAME}"

# Default to BLOB trigger — simplest mode, no Event Grid or Queue infra needed
TRIGGER_MODE="${TRIGGER_MODE:-BLOB}"

echo "=============================================="
echo "AI Foundry Processing — Deployment Starting"
echo "=============================================="
echo "Function App: $FUNC_FOUNDRY_APP"
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
echo "[Step 1/6] Creating Function App: $FUNC_FOUNDRY_APP"

echo "  -> Creating storage account: $FUNC_FOUNDRY_STORAGE"
az storage account create \
  --name "$FUNC_FOUNDRY_STORAGE" \
  --resource-group "$RG_NAME" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --tags Environment=dev Purpose="FuncFoundryProcessingStorage" \
  -o none

# Build Function App create command — conditionally include --app-insights
FUNC_CREATE_ARGS=(
  --name "$FUNC_FOUNDRY_APP"
  --resource-group "$RG_NAME"
  --storage-account "$FUNC_FOUNDRY_STORAGE"
  --runtime python
  --runtime-version 3.11
  --functions-version 4
  --os-type Linux
  --consumption-plan-location "$LOCATION"
  --https-only true
  --tags Environment=dev Purpose="AIFoundryDocProcessing"
  -o none
)
if [ -n "$APP_INSIGHTS" ]; then
  FUNC_CREATE_ARGS+=(--app-insights "$APP_INSIGHTS")
fi

echo "  -> Creating Function App: $FUNC_FOUNDRY_APP"
az functionapp create "${FUNC_CREATE_ARGS[@]}"
echo "  -> $FUNC_FOUNDRY_APP created."

# ---------------------------------------------------------------------------
# Step 2: Enable Managed Identity + RBAC
# ---------------------------------------------------------------------------
echo "[Step 2/6] Enabling Managed Identity and assigning RBAC"

FOUNDRY_PRINCIPAL_ID=$(az functionapp identity assign \
  --name "$FUNC_FOUNDRY_APP" \
  --resource-group "$RG_NAME" \
  --query principalId -o tsv)
echo "  -> Principal ID: $FOUNDRY_PRINCIPAL_ID"

echo "  -> Waiting 30s for AAD propagation..."
sleep 30

echo "  -> Assigning RBAC roles..."

# 1. Storage Blob Data Contributor on ADLS
az role assignment create \
  --assignee "$FOUNDRY_PRINCIPAL_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG_NAME/providers/Microsoft.Storage/storageAccounts/$ADLS_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 2. Cognitive Services User on Foundry
az role assignment create \
  --assignee "$FOUNDRY_PRINCIPAL_ID" \
  --role "Cognitive Services User" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 3. Search Index Data Contributor on AI Search
az role assignment create \
  --assignee "$FOUNDRY_PRINCIPAL_ID" \
  --role "Search Index Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$SEARCH_RG/providers/Microsoft.Search/searchServices/$SEARCH_SERVICE" \
  -o none 2>/dev/null || echo "    (role may already exist)"

# 4. Storage Queue Data Contributor on ADLS (for queue-based triggering)
az role assignment create \
  --assignee "$FOUNDRY_PRINCIPAL_ID" \
  --role "Storage Queue Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG_NAME/providers/Microsoft.Storage/storageAccounts/$ADLS_ACCOUNT" \
  -o none 2>/dev/null || echo "    (role may already exist)"

echo "  -> RBAC assignments complete."

# ---------------------------------------------------------------------------
# Step 3: Ensure Foundry Model Deployments Exist
# ---------------------------------------------------------------------------
echo "[Step 3/6] Ensuring required model deployments in Foundry"

EMBEDDING_DEPLOYMENT="${FOUNDRY_EMBEDDING_DEPLOYMENT:-text-embedding-3-large}"
EMBEDDING_MODEL="${FOUNDRY_EMBEDDING_MODEL:-text-embedding-3-large}"

# Check if embedding model deployment exists; create if not
EXISTING_DEPLOYMENT=$(az cognitiveservices account deployment list \
  --name "$FOUNDRY_ACCOUNT" \
  --resource-group "$FOUNDRY_RG" \
  --query "[?name=='$EMBEDDING_DEPLOYMENT'].name" -o tsv 2>/dev/null || echo "")

if [ -z "$EXISTING_DEPLOYMENT" ]; then
  echo "  -> Deploying model: $EMBEDDING_DEPLOYMENT ($EMBEDDING_MODEL)"
  az cognitiveservices account deployment create \
    --name "$FOUNDRY_ACCOUNT" \
    --resource-group "$FOUNDRY_RG" \
    --deployment-name "$EMBEDDING_DEPLOYMENT" \
    --model-name "$EMBEDDING_MODEL" \
    --model-version "1" \
    --model-format OpenAI \
    --sku-capacity 1 \
    --sku-name Standard \
    -o none
  echo "  -> $EMBEDDING_DEPLOYMENT deployed."
else
  echo "  -> $EMBEDDING_DEPLOYMENT already exists."
fi

# ---------------------------------------------------------------------------
# Step 4: Configure App Settings (from .env, excluding DEPLOY_* variables)
# ---------------------------------------------------------------------------
echo "[Step 4/6] Configuring app settings from .env"

# Read app settings from .env — filter out DEPLOY_* (deployment-only, not app settings)
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

# Override trigger enable/disable based on IS_ACTIVE and TRIGGER_MODE
if [ "$IS_ACTIVE" = "true" ]; then
  FOUNDRY_EG_DISABLED="true"
  FOUNDRY_QUEUE_DISABLED="true"
  FOUNDRY_BLOB_DISABLED="true"

  if [ "$TRIGGER_MODE" = "EVENTGRID_QUEUE" ]; then
    FOUNDRY_QUEUE_DISABLED="false"
  elif [ "$TRIGGER_MODE" = "EVENTGRID_DIRECT" ]; then
    FOUNDRY_EG_DISABLED="false"
  elif [ "$TRIGGER_MODE" = "BLOB" ]; then
    FOUNDRY_BLOB_DISABLED="false"
  fi
else
  FOUNDRY_EG_DISABLED="true"
  FOUNDRY_QUEUE_DISABLED="true"
  FOUNDRY_BLOB_DISABLED="true"
fi

az functionapp config appsettings set \
  --name "$FUNC_FOUNDRY_APP" \
  --resource-group "$RG_NAME" \
  --settings \
    "${ENV_SETTINGS[@]}" \
    "TRIGGER_MODE=$TRIGGER_MODE" \
    "AzureWebJobs.process_new_document.Disabled=$FOUNDRY_EG_DISABLED" \
    "AzureWebJobs.process_queue_document.Disabled=$FOUNDRY_QUEUE_DISABLED" \
    "AzureWebJobs.process_blob_document.Disabled=$FOUNDRY_BLOB_DISABLED" \
  -o none

echo "  -> App settings configured from $ENV_FILE (DEPLOY_* excluded)"

# ---------------------------------------------------------------------------
# Step 5: Deploy Function App Code
# ---------------------------------------------------------------------------
echo "[Step 5/6] Deploying function code"

if command -v func &> /dev/null; then
  cd "$SCRIPT_DIR"
  echo "  -> Publishing $FUNC_FOUNDRY_APP..."
  func azure functionapp publish "$FUNC_FOUNDRY_APP" --python
  echo "  -> $FUNC_FOUNDRY_APP deployed."
else
  echo "  WARNING: Azure Functions Core Tools (func) not installed."
  echo "  -> Skipping deployment. Run manually:"
  echo "     cd $SCRIPT_DIR && func azure functionapp publish $FUNC_FOUNDRY_APP --python"
fi

# ---------------------------------------------------------------------------
# Step 6: Verify + Restart
# ---------------------------------------------------------------------------
echo "[Step 6/6] Verifying deployment"

FUNC_FOUNDRY_STATE=$(az functionapp show --name "$FUNC_FOUNDRY_APP" --resource-group "$RG_NAME" --query "state" -o tsv 2>/dev/null || echo "NOT FOUND")
echo "  -> Function App state: $FUNC_FOUNDRY_STATE"

az functionapp restart --name "$FUNC_FOUNDRY_APP" --resource-group "$RG_NAME" -o none 2>/dev/null || true
echo "  -> Function App restarted."

echo ""
echo "=============================================="
echo "AI Foundry Processing — Deployment Complete"
echo "=============================================="
echo "  Function App: $FUNC_FOUNDRY_APP"
echo "  Storage:      $FUNC_FOUNDRY_STORAGE"
echo "  Health:       https://$FUNC_FOUNDRY_APP.azurewebsites.net/api/health"
echo "  Logs:         func azure functionapp logstream $FUNC_FOUNDRY_APP"
echo "  Active:       $IS_ACTIVE"
echo "  Trigger:      $TRIGGER_MODE"
echo ""
