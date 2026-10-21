# Deploy to Azure App Service

Delivery Twin runs on **Azure App Service for Linux** with the built-in Python runtime. You deploy the code
as a zip, so you don't need a container registry. One script provisions everything and pushes the code.

## What gets created

| Resource | Notes |
|---|---|
| App Service plan `<app>-plan` | Linux, `B1` by default, **one instance** (DuckDB is a single-writer file). |
| Web app `<app>` | Python 3.13. The startup command is `python -m deliverytwin serve`, health check is `/healthz`, HTTPS only, TLS 1.2+, FTP off, Always On, and it has a system-assigned managed identity. |
| App Service Authentication | Microsoft Entra ID sign-in is required for every page except `/healthz`. |
| Entra app registration | Created by the script unless you pass an existing one. |

The template is [`infra/main.bicep`](../infra/main.bicep) and the script is [`infra/deploy.ps1`](../infra/deploy.ps1).

## Prerequisites

- The [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli). Run `az login` and select the
  subscription you want with `az account set`.
- Contributor rights on the target resource group. Creating the Entra app registration also needs
  permission to register applications in your tenant; otherwise pass an existing one (see below).
- Python 3.12 or later on your machine. The deploy script uses it to build the zip.

## Deploy

From the repository root:

```powershell
./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -Location eastus
```

The script:

1. creates the Entra app registration and its redirect URI;
2. deploys the Bicep template;
3. zips `src/` with [`scripts/package_app.py`](../scripts/package_app.py);
4. pushes the zip.

App Service then installs `requirements.txt`. The first build takes a few minutes, and the app is at
`https://<app>.azurewebsites.net`.

To redeploy only the code after a change:

```powershell
./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -SkipInfra
```

### Options

| Parameter | Default | Meaning |
|---|---|---|
| `-Sku` | `B1` | App Service plan size (`B1`–`B3`, `S1`–`S3`, `P0v3`–`P2v3`). |
| `-PythonVersion` | `3.13` | Built-in runtime (`3.12`, `3.13`, `3.14` where the region offers it). |
| `-AuthClientId`, `-AuthClientSecret` | — | Use an existing app registration. Its redirect URI must be `https://<app>.azurewebsites.net/.auth/login/aad/callback`, and its Application ID URI `api://<client-id>`. |
| `-EnableFoundry`, `-FoundryProjectEndpoint`, `-FoundryModel` | off | Foundry models for the AI features and chat (see below). |
| `-NoDemoMode` | off | Hides the simulation lab and the data reset and reload routes. |

## Microsoft Foundry and Azure Databricks

Both integrations are **off** by default (`DELIVERYTWIN_FOUNDRY_ENABLED=0`, `DELIVERYTWIN_DATABRICKS_ENABLED=0`).
Without them, the app runs on DuckDB, and every AI step shows its labeled rules-based fallback. In Azure the
only model source is **Microsoft Foundry**. On App Service the app ignores `OPENAI_API_KEY`, which is never
deployed, and Databricks Model Serving. Locally, `OPENAI_API_KEY` keeps working for testing. You can deploy
the settings now and switch them on later. To switch one on,
re-run the script with `-EnableFoundry` or `-EnableDatabricks`, or set the application setting to `1` in the
portal. Re-running the infrastructure step replaces all application settings, so pass the same values each time.

### Foundry

1. Deploy with the switch, the project endpoint and the model deployment. The AI features and the chat agent
   share them, and the model endpoint (`https://<resource>.openai.azure.com`) is derived from the project.
   Pass `-FoundryEndpoint` only to override it.

   ```powershell
   ./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -EnableFoundry `
       -FoundryProjectEndpoint https://<resource>.services.ai.azure.com/api/projects/<project> -FoundryModel <deployment>
   ```

2. Let the web app's managed identity call the model (*Cognitive Services OpenAI User* on the resource) and
   the chat agent (*Azure AI User* on the project). The template outputs `principalId`, or you can read it
   with `az webapp identity show`.

   ```powershell
   $principal = az webapp identity show -g rg-deliverytwin -n deliverytwin-demo --query principalId -o tsv
   $foundry = az cognitiveservices account show -g <foundry-rg> -n <foundry-resource> --query id -o tsv
   az role assignment create --assignee $principal --role "Cognitive Services OpenAI User" --scope $foundry
   ```

### Databricks

```powershell
./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -EnableDatabricks `
    -DatabricksHost https://adb-<id>.<n>.azuredatabricks.net -DatabricksHttpPath /sql/1.0/warehouses/<id> `
    -DatabricksToken <token>
```

- **What it does:** the four application tables move to `<catalog>.<schema>.dt_*` (default `dpm_dev.mlops`)
  once `-DatabricksHttpPath` is set. Models still come from Foundry: Databricks Model Serving is not used on
  App Service.
- **Libraries:** with `-EnableDatabricks` the package also installs the Databricks SQL connector and SDK.
- **Check first:** run `python -m deliverytwin databricks-smoke` against the workspace before switching on,
  because the adapter has not yet been run against a live workspace.
- **Token:** prefer replacing it with a Key Vault reference afterwards.

`/api/ai/status` reports the active AI backend and both switches, and the log stream's first lines show the
data engine. [`.env.example`](../.env.example) lists every setting with its meaning.

## Runtime settings

The template sets these application settings:

| Setting | Value | Why |
|---|---|---|
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` | App Service installs `requirements.txt` when you deploy. |
| `DELIVERYTWIN_HOST` / `DELIVERYTWIN_PORT` | `0.0.0.0` / `8000` | Where App Service routes traffic. |
| `DELIVERYTWIN_DB_PATH` | `/home/data/deliverytwin.duckdb` | `/home` is persistent storage that survives restarts and redeployments. |
| `DELIVERYTWIN_TRUSTED_AUTH` | `1` | The app has no sign-in of its own and refuses to listen beyond loopback without this flag. The template sets it **only together with** App Service Authentication. |
| `DELIVERYTWIN_DEMO_MODE` | `1` | Synthetic-data demo routes (reset, reload, simulation lab). |
| `DELIVERYTWIN_BLENDED_RATE` | `150` | Mock labor rate behind recovery cost impact. |
| `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` | the client secret | Read by App Service Authentication. |

## Operations

- **Logs:** run `az webapp log tail -g rg-deliverytwin -n deliverytwin-demo`, or open **Log stream** in the
  portal.
- **Scaling:** keep one instance. DuckDB is a single file with one writer, so scaling out would put several
  writers on the same file. Scale up (a larger SKU) instead. For shared, durable data across instances, use
  the Databricks data layer (`DELIVERYTWIN_DB_DIALECT=databricks`; see the README).
- **Data:** the database is synthetic. On first start the app creates the schema. Use the dashboard to load
  and simulate data. Deleting `/home/data/deliverytwin.duckdb` (through Kudu/SSH) starts fresh.
- **Secret rotation:** the app registration secret expires after one year. Rotate it with
  `az ad app credential reset` and update `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`.

## Container alternative

[`src/Dockerfile`](../src/Dockerfile) builds the same app as an image. To use it, push the image to Azure
Container Registry and create a **Web App for Containers**. Set `WEBSITES_PORT=8000`,
`DELIVERYTWIN_TRUSTED_AUTH=1` (with App Service Authentication on) and `DELIVERYTWIN_DB_PATH=/home/data/deliverytwin.duckdb`,
and enable `WEBSITES_ENABLE_APP_SERVICE_STORAGE` so `/home` persists.
