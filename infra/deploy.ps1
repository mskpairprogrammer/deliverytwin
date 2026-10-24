<#
.SYNOPSIS
    Provision Delivery Twin on Azure App Service and deploy the code.

.DESCRIPTION
    1. Creates (or reuses) a Microsoft Entra app registration for App Service Authentication.
    2. Deploys infra/main.bicep: a Linux App Service plan and web app with sign-in required.
    3. Zips src/ (scripts/package_app.py) and pushes it; App Service installs requirements.txt.
    Needs the Azure CLI (az login first) and Python. Re-run with -SkipInfra to redeploy code only.

    In Azure the app uses Microsoft Foundry models only; OPENAI_API_KEY is never deployed (and is ignored on
    App Service). Foundry and Azure Databricks are off unless -EnableFoundry / -EnableDatabricks is passed. Their
    settings can be supplied now and switched on later (re-run with the switch, or set the
    DELIVERYTWIN_*_ENABLED application setting to 1). Re-running the infrastructure step replaces every
    application setting, so pass the same values each time.

.EXAMPLE
    ./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -Location eastus

.EXAMPLE
    ./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -EnableFoundry `
        -FoundryProjectEndpoint https://my-foundry.services.ai.azure.com/api/projects/delivery-twin -FoundryModel gpt-4o-mini
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $AppName,
    [string] $Location = "eastus",
    [string] $Sku = "B1",
    [string] $PythonVersion = "3.13",
    # Reuse an existing app registration instead of creating one.
    [string] $AuthClientId = "",
    [string] $AuthClientSecret = "",
    [switch] $NoDemoMode,
    [switch] $SkipInfra,

    # Microsoft Foundry: the project and the model deployment used by the AI features and the chat agent.
    [switch] $EnableFoundry,
    [string] $FoundryProjectEndpoint = "",
    [string] $FoundryModel = "",
    [string] $FoundryEndpoint = "",   # optional; derived from the project endpoint when blank

    # Azure Databricks: SQL warehouse for all application data.
    [switch] $EnableDatabricks,
    [string] $DatabricksHost = "",
    [string] $DatabricksHttpPath = "",
    [string] $DatabricksCatalog = "dpm_dev",
    [string] $DatabricksSchema = "mlops",
    [string] $DatabricksToken = "",

    # Private PyPI index (e.g. Artifactory) for the deploy-time pip install when the app has no internet access.
    [string] $PipIndexUrl = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Az {
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed." }
    return $output
}

if (-not $SkipInfra) {
    if (-not $AuthClientId) {
        Write-Host "Creating the Entra app registration '$AppName' for sign-in..."
        $callback = "https://$AppName.azurewebsites.net/.auth/login/aad/callback"
        $AuthClientId = Invoke-Az ad app create --display-name $AppName --sign-in-audience AzureADMyOrg `
            --web-redirect-uris $callback --enable-id-token-issuance true --query appId -o tsv
        Invoke-Az ad app update --id $AuthClientId --identifier-uris "api://$AuthClientId" | Out-Null
        $AuthClientSecret = Invoke-Az ad app credential reset --id $AuthClientId --display-name "app-service-auth" `
            --years 1 --query password -o tsv
    } elseif (-not $AuthClientSecret) {
        throw "Pass -AuthClientSecret together with -AuthClientId."
    }

    # Parameters go through a temporary file so secrets stay off the command line and empty values stay empty.
    $values = [ordered]@{
        appName = $AppName; location = $Location; skuName = $Sku; pythonVersion = $PythonVersion
        authClientId = $AuthClientId; authClientSecret = $AuthClientSecret; demoMode = (-not $NoDemoMode)
        enableFoundry = [bool]$EnableFoundry; foundryProjectEndpoint = $FoundryProjectEndpoint
        foundryModel = $FoundryModel; foundryEndpoint = $FoundryEndpoint
        enableDatabricks = [bool]$EnableDatabricks; databricksHost = $DatabricksHost
        databricksHttpPath = $DatabricksHttpPath; databricksCatalog = $DatabricksCatalog
        databricksSchema = $DatabricksSchema; databricksToken = $DatabricksToken
        pipIndexUrl = $PipIndexUrl
    }
    $parameters = [ordered]@{
        '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters = [ordered]@{}
    }
    foreach ($name in $values.Keys) { $parameters.parameters[$name] = @{ value = $values[$name] } }
    $parameterFile = Join-Path ([System.IO.Path]::GetTempPath()) "deliverytwin-$([guid]::NewGuid()).parameters.json"

    Write-Host "Provisioning resource group $ResourceGroup and the web app..."
    Invoke-Az group create --name $ResourceGroup --location $Location --output none | Out-Null
    try {
        $parameters | ConvertTo-Json -Depth 5 | Set-Content -Path $parameterFile -Encoding utf8
        $url = Invoke-Az deployment group create --resource-group $ResourceGroup `
            --template-file (Join-Path $PSScriptRoot "main.bicep") --parameters "@$parameterFile" `
            --query properties.outputs.url.value -o tsv
    } finally {
        Remove-Item $parameterFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Packaging src/..."
$zip = Join-Path $root "dist/deliverytwin-app.zip"
$packageArgs = @((Join-Path $root "scripts/package_app.py"), "--out", $zip)
if ($EnableDatabricks) { $packageArgs += "--with-databricks" }  # App Service installs only requirements.txt
& python @packageArgs
if ($LASTEXITCODE -ne 0) { throw "Packaging failed." }

Write-Host "Deploying the code (App Service installs requirements.txt; the first build takes a few minutes)..."
Invoke-Az webapp deploy --resource-group $ResourceGroup --name $AppName --src-path $zip --type zip --output none | Out-Null

if (-not $url) { $url = "https://" + (Invoke-Az webapp show --resource-group $ResourceGroup --name $AppName --query defaultHostName -o tsv) }
Write-Host "Done: $url (sign in with your Microsoft Entra account)."
