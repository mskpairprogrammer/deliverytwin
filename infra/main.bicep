// Delivery Twin on Azure App Service (Linux, built-in Python runtime, code deployment).
//
// Creates a Linux App Service plan and a web app that runs `python -m deliverytwin serve`, behind
// App Service Authentication (Microsoft Entra ID). The app has no sign-in of its own and refuses to
// listen beyond loopback unless DELIVERYTWIN_TRUSTED_AUTH=1, so the template always turns on
// authentication and sets that flag together. Deploy it with infra/deploy.ps1, which also packages
// src/ and pushes the code; see docs/deploy-azure-app-service.md.

@description('Globally unique web app name; the site is https://<appName>.azurewebsites.net.')
@minLength(2)
@maxLength(60)
param appName string

@description('Azure region for the plan and the web app.')
param location string = resourceGroup().location

@description('App Service plan SKU. B1 or higher keeps the app always on; the app runs as a single instance.')
@allowed(['B1', 'B2', 'B3', 'S1', 'S2', 'S3', 'P0v3', 'P1v3', 'P2v3'])
param skuName string = 'B1'

@description('Python runtime version of the App Service built-in image.')
@allowed(['3.12', '3.13', '3.14'])
param pythonVersion string = '3.13'

@description('Application (client) ID of the Microsoft Entra app registration used for sign-in.')
param authClientId string

@description('Client secret of that app registration; stored as an app setting App Service Authentication reads.')
@secure()
param authClientSecret string

@description('Microsoft Entra tenant that users sign in with.')
param tenantId string = subscription().tenantId

@description('DuckDB file. /home is persistent storage that survives restarts and redeployments.')
param databasePath string = '/home/data/deliverytwin.duckdb'

@description('Demo mode enables the simulation lab, data reset and reload routes (synthetic data only).')
param demoMode bool = true

@description('Mock blended labor rate (USD/hour) behind the cost impact of recovery scenarios.')
param blendedRate string = '150'

// --- Microsoft Foundry: the only model source in Azure (off by default) -----------------------------------
// The app signs in with the web app's managed identity: grant it "Cognitive Services OpenAI User" on the
// Foundry resource and "Azure AI User" on the project. No API key is stored, and OPENAI_API_KEY is never set
// (the app also ignores it on App Service).
@description('Master switch for Microsoft Foundry. Off: the Foundry settings below are ignored even if filled in.')
param enableFoundry bool = false

@description('Foundry project endpoint (https://<resource>.services.ai.azure.com/api/projects/<project>).')
param foundryProjectEndpoint string = ''

@description('Model deployment name used by the AI features and the chat agent, e.g. gpt-4o-mini.')
param foundryModel string = ''

@description('Optional model endpoint; by default derived from the project (https://<resource>.openai.azure.com).')
param foundryEndpoint string = ''

// --- Azure Databricks (off by default) -------------------------------------------------------------------
@description('Master switch for Azure Databricks. Off: the Databricks settings below are ignored even if filled in.')
param enableDatabricks bool = false

@description('Workspace URL (https://adb-<id>.<n>.azuredatabricks.net).')
param databricksHost string = ''

@description('SQL warehouse HTTP path; when set, the four application tables live in Unity Catalog / Delta.')
param databricksHttpPath string = ''

@description('Unity Catalog catalog and schema for the application tables.')
param databricksCatalog string = 'dpm_dev'
param databricksSchema string = 'mlops'

@description('Personal access token or service-principal token for the workspace. Prefer a Key Vault reference.')
@secure()
param databricksToken string = ''

// --- Package index (optional) ----------------------------------------------------------------------------
@description('Private PyPI index for the deploy-time pip install when the app has no internet access, e.g. https://<user>:<token>@<artifactory>/artifactory/api/pypi/<repo>/simple. Stored as an app setting; prefer a Key Vault reference.')
@secure()
param pipIndexUrl string = ''

var authSecretSettingName = 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'

var baseSettings = [
  { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' } // Oryx installs requirements.txt on deploy
  { name: 'DELIVERYTWIN_HOST', value: '0.0.0.0' }
  { name: 'DELIVERYTWIN_PORT', value: '8000' }
  { name: 'DELIVERYTWIN_DB_PATH', value: databasePath }
  { name: 'DELIVERYTWIN_TRUSTED_AUTH', value: '1' } // paired with authsettingsV2 below
  { name: 'DELIVERYTWIN_DEMO_MODE', value: demoMode ? '1' : '0' }
  { name: 'DELIVERYTWIN_BLENDED_RATE', value: blendedRate }
  { name: authSecretSettingName, value: authClientSecret }
]
var integrationSettings = [
  { name: 'DELIVERYTWIN_FOUNDRY_ENABLED', value: enableFoundry ? '1' : '0' }
  { name: 'DELIVERYTWIN_DATABRICKS_ENABLED', value: enableDatabricks ? '1' : '0' }
  { name: 'DELIVERYTWIN_CATALOG', value: databricksCatalog }
  { name: 'DELIVERYTWIN_SCHEMA', value: databricksSchema }
]
// Only filled-in values become app settings, so a later switch-on needs just the missing values.
var optionalSettings = filter([
  { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
  { name: 'FOUNDRY_MODEL', value: foundryModel }
  { name: 'FOUNDRY_ENDPOINT', value: foundryEndpoint }
  { name: 'DATABRICKS_HOST', value: databricksHost }
  { name: 'DATABRICKS_HTTP_PATH', value: databricksHttpPath }
  { name: 'DATABRICKS_TOKEN', value: databricksToken }
  { name: 'PIP_INDEX_URL', value: pipIndexUrl }
], setting => !empty(setting.value))

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'linux'
  sku: {
    name: skuName
    capacity: 1 // DuckDB is a single-writer file: keep one instance
  }
  properties: {
    reserved: true // Linux
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      appCommandLine: 'python -m deliverytwin serve'
      alwaysOn: true
      healthCheckPath: '/healthz'
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat(baseSettings, integrationSettings, optionalSettings)
    }
  }
}

resource auth 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: ['/healthz']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: 'https://sts.windows.net/${tenantId}/v2.0'
          clientId: authClientId
          clientSecretSettingName: authSecretSettingName
        }
        validation: {
          allowedAudiences: ['api://${authClientId}']
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
  }
}

resource logs 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: site
  name: 'logs'
  properties: {
    applicationLogs: {
      fileSystem: {
        level: 'Information'
      }
    }
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 7
        retentionInMb: 35
      }
    }
  }
}

output url string = 'https://${site.properties.defaultHostName}'
output principalId string = site.identity.principalId
