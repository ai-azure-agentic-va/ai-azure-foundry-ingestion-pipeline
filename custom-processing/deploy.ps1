# =============================================================================
# AI Foundry Document Ingestion Pipeline — Custom Processing Deployment (Windows)
# =============================================================================
# Deploys the Custom Processing Function App:
#   - Creates Function App + storage account
#   - Assigns Managed Identity + RBAC roles
#   - Configures app settings from .env (single source of truth)
#   - Publishes function code
#
# All values come from .env — no hardcoded defaults in this script.
#
# Prerequisites:
#   - .env file populated (copy .env.example if needed)
#   - Shared infrastructure must exist
#   - Azure CLI authenticated (az login), Functions Core Tools installed
#
# Usage:
#   .\deploy.ps1                                   # deploy with .env values
#   $env:TRIGGER_MODE="EVENTGRID_QUEUE"; .\deploy.ps1   # override trigger mode
# =============================================================================

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$EnvFile = Join-Path $ScriptDir ".env"

# ---------------------------------------------------------------------------
# Load .env — single source of truth for all values
# ---------------------------------------------------------------------------
if (-not (Test-Path $EnvFile)) {
    Write-Error "ERROR: .env file not found at $EnvFile`n  -> Copy .env.example to .env and fill in values"
    exit 1
}

$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) { return }
    if ($line -match '^([^#]*\S)(\s+#.*)$') { $line = $Matches[1] }
    $line = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { return }
    $eqIndex = $line.IndexOf('=')
    if ($eqIndex -le 0) { return }
    $key = $line.Substring(0, $eqIndex)
    $val = $line.Substring($eqIndex + 1)
    if ($key.Contains('.')) { return }
    $envVars[$key] = $val
}

function Get-Val {
    param([string]$EnvKey, [string]$FileKey = "", [string]$Default = "")
    if ($FileKey -eq "") { $FileKey = $EnvKey }
    $envVal = [System.Environment]::GetEnvironmentVariable($EnvKey)
    if ($envVal) { return $envVal }
    if ($envVars.ContainsKey($FileKey) -and $envVars[$FileKey]) { return $envVars[$FileKey] }
    if ($envVars.ContainsKey($EnvKey) -and $envVars[$EnvKey]) { return $envVars[$EnvKey] }
    return $Default
}

# ---------------------------------------------------------------------------
# Map variables
# ---------------------------------------------------------------------------
$SubscriptionId   = Get-Val "SUBSCRIPTION_ID"      "DEPLOY_SUBSCRIPTION_ID"
$Location         = Get-Val "LOCATION"              "DEPLOY_LOCATION"
$RgName           = Get-Val "RG_NAME"               "DEPLOY_RG_NAME"
$FuncApp          = Get-Val "FUNC_CUSTOM_APP"       "DEPLOY_FUNC_APP_NAME"
$FuncStorage      = Get-Val "FUNC_CUSTOM_STORAGE"   "DEPLOY_FUNC_STORAGE_ACCOUNT"
$IsActive         = Get-Val "IS_ACTIVE"             "DEPLOY_IS_ACTIVE"         "true"
$SearchService    = Get-Val "SEARCH_SERVICE"        "DEPLOY_SEARCH_SERVICE"
$SearchRg         = Get-Val "SEARCH_RG"             "DEPLOY_SEARCH_RG"
$FoundryAccount   = Get-Val "FOUNDRY_ACCOUNT"       "DEPLOY_FOUNDRY_ACCOUNT"
$FoundryRg        = Get-Val "FOUNDRY_RG"            "DEPLOY_FOUNDRY_RG"
$AppInsights      = Get-Val "APP_INSIGHTS"          "DEPLOY_APP_INSIGHTS"      ""
$AdlsAccount      = Get-Val "ADLS_ACCOUNT"          "ADLS_ACCOUNT_NAME"
$SearchIndex      = Get-Val "SEARCH_INDEX"          "SEARCH_INDEX_NAME"

# Default to BLOB — simplest mode, no Event Grid or Queue infra needed
$TriggerMode      = Get-Val "TRIGGER_MODE"          "TRIGGER_MODE"             "BLOB"

Write-Host "=============================================="
Write-Host "Custom Processing — Deployment Starting"
Write-Host "=============================================="
Write-Host "Function App: $FuncApp"
Write-Host "Active:       $IsActive"
Write-Host "Trigger mode: $TriggerMode"
if ($AppInsights) {
    Write-Host "App Insights: $AppInsights"
} else {
    Write-Host "App Insights: (none — monitoring disabled)"
}
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Create Function App + Storage
# ---------------------------------------------------------------------------
Write-Host "[Step 1/5] Creating Function App: $FuncApp"

Write-Host "  -> Creating storage account: $FuncStorage"
az storage account create `
    --name $FuncStorage `
    --resource-group $RgName `
    --location $Location `
    --sku Standard_LRS `
    --kind StorageV2 `
    --tags Environment=dev Purpose="FuncCustomProcessingStorage" `
    -o none

Write-Host "  -> Creating Function App: $FuncApp"
$funcCreateArgs = @(
    "functionapp", "create",
    "--name", $FuncApp,
    "--resource-group", $RgName,
    "--storage-account", $FuncStorage,
    "--runtime", "python",
    "--runtime-version", "3.11",
    "--functions-version", "4",
    "--os-type", "Linux",
    "--consumption-plan-location", $Location,
    "--https-only", "true",
    "--tags", "Environment=dev", "Purpose=CustomLibrariesDocProcessing",
    "-o", "none"
)
if ($AppInsights) {
    $funcCreateArgs += @("--app-insights", $AppInsights)
}
& az @funcCreateArgs
Write-Host "  -> $FuncApp created."

# ---------------------------------------------------------------------------
# Step 2: Enable Managed Identity + RBAC
# ---------------------------------------------------------------------------
Write-Host "[Step 2/5] Enabling Managed Identity and assigning RBAC"

$PrincipalId = az functionapp identity assign `
    --name $FuncApp `
    --resource-group $RgName `
    --query principalId -o tsv
Write-Host "  -> Principal ID: $PrincipalId"

Write-Host "  -> Waiting 30s for AAD propagation..."
Start-Sleep -Seconds 30

Write-Host "  -> Assigning RBAC roles..."

$roles = @(
    @{ Role = "Storage Blob Data Contributor";     Scope = "/subscriptions/$SubscriptionId/resourceGroups/$RgName/providers/Microsoft.Storage/storageAccounts/$AdlsAccount" },
    @{ Role = "Cognitive Services User";           Scope = "/subscriptions/$SubscriptionId/resourceGroups/$FoundryRg/providers/Microsoft.CognitiveServices/accounts/$FoundryAccount" },
    @{ Role = "Search Index Data Contributor";     Scope = "/subscriptions/$SubscriptionId/resourceGroups/$SearchRg/providers/Microsoft.Search/searchServices/$SearchService" },
    @{ Role = "Storage Queue Data Contributor";    Scope = "/subscriptions/$SubscriptionId/resourceGroups/$RgName/providers/Microsoft.Storage/storageAccounts/$AdlsAccount" }
)

foreach ($r in $roles) {
    try {
        az role assignment create --assignee $PrincipalId --role $r.Role --scope $r.Scope -o none 2>$null
    } catch {
        Write-Host "    ($($r.Role) may already exist)"
    }
}
Write-Host "  -> RBAC assignments complete."

# ---------------------------------------------------------------------------
# Step 3: Configure App Settings (from .env, excluding DEPLOY_*)
# ---------------------------------------------------------------------------
Write-Host "[Step 3/5] Configuring app settings from .env"

$appSettings = @()
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) { return }
    if ($line -match '^([^#]*\S)(\s+#.*)$') { $line = $Matches[1] }
    $line = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { return }
    if ($line.StartsWith("DEPLOY_")) { return }
    $appSettings += $line
}

# Trigger enable/disable
$egDisabled    = "true"
$queueDisabled = "true"
$blobDisabled  = "true"

if ($IsActive -eq "true") {
    switch ($TriggerMode) {
        "EVENTGRID_QUEUE"  { $queueDisabled = "false" }
        "EVENTGRID_DIRECT" { $egDisabled    = "false" }
        "BLOB"             { $blobDisabled  = "false" }
    }
}

$appSettings += "TRIGGER_MODE=$TriggerMode"
$appSettings += "AzureWebJobs.process_new_document.Disabled=$egDisabled"
$appSettings += "AzureWebJobs.process_queue_document.Disabled=$queueDisabled"
$appSettings += "AzureWebJobs.process_blob_document.Disabled=$blobDisabled"

az functionapp config appsettings set `
    --name $FuncApp `
    --resource-group $RgName `
    --settings @appSettings `
    -o none

Write-Host "  -> App settings configured from $EnvFile (DEPLOY_* excluded)"

# ---------------------------------------------------------------------------
# Step 4: Deploy Function App Code
# ---------------------------------------------------------------------------
Write-Host "[Step 4/5] Deploying function code"

$funcCmd = Get-Command func -ErrorAction SilentlyContinue
if ($funcCmd) {
    Push-Location $ScriptDir
    Write-Host "  -> Publishing $FuncApp..."
    func azure functionapp publish $FuncApp --python
    Pop-Location
    Write-Host "  -> $FuncApp deployed."
} else {
    Write-Host "  WARNING: Azure Functions Core Tools (func) not installed."
    Write-Host "  -> Skipping deployment. Run manually:"
    Write-Host "     cd $ScriptDir; func azure functionapp publish $FuncApp --python"
}

# ---------------------------------------------------------------------------
# Step 5: Verify + Restart
# ---------------------------------------------------------------------------
Write-Host "[Step 5/5] Verifying deployment"

$state = az functionapp show --name $FuncApp --resource-group $RgName --query "state" -o tsv 2>$null
if (-not $state) { $state = "NOT FOUND" }
Write-Host "  -> Function App state: $state"

az functionapp restart --name $FuncApp --resource-group $RgName -o none 2>$null
Write-Host "  -> Function App restarted."

Write-Host ""
Write-Host "=============================================="
Write-Host "Custom Processing — Deployment Complete"
Write-Host "=============================================="
Write-Host "  Function App: $FuncApp"
Write-Host "  Storage:      $FuncStorage"
Write-Host "  Health:       https://$FuncApp.azurewebsites.net/api/health"
Write-Host "  Logs:         func azure functionapp logstream $FuncApp"
Write-Host "  Active:       $IsActive"
Write-Host "  Trigger:      $TriggerMode"
Write-Host ""
