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
| `-PipIndexUrl` | — | Private PyPI index, such as Artifactory, for the deploy-time `pip install` when App Service has no internet access. It is set as `PIP_INDEX_URL`; prefer a Key Vault reference (see [Artifactory](#packages-from-artifactory-no-internet-access)). |

## Manual install (portal)

Turn on sign-in **before** the app goes live. The app has no login of its own.

1. **Create the web app.** Go to **Create a resource → Web App**.
   - **Resource group:** new, for example `rg-deliverytwin`.
   - **Name:** for example `deliverytwin-demo`.
   - **Publish:** Code. **Runtime:** Python 3.13. **OS:** Linux.
   - **Plan:** Basic B1 or higher.
2. **Turn on sign-in.** Go to **Settings → Authentication → Add identity provider**.
   - **Provider:** Microsoft, and create a new app registration.
   - **Account types:** current tenant only.
   - **Access:** require authentication, and redirect unauthenticated requests (HTTP 302).
3. **Add the app settings.** Go to **Settings → Environment variables**:

   | Name | Value |
   |---|---|
   | `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |
   | `DELIVERYTWIN_HOST` / `DELIVERYTWIN_PORT` | `0.0.0.0` / `8000` |
   | `DELIVERYTWIN_DB_PATH` | `/home/data/deliverytwin.duckdb` |
   | `DELIVERYTWIN_TRUSTED_AUTH` | `1` (only with step 2 done) |
   | `DELIVERYTWIN_DEMO_MODE` | `1` |
   | `DELIVERYTWIN_FOUNDRY_ENABLED` / `DELIVERYTWIN_DATABRICKS_ENABLED` | `0` / `0` |

   Don't add `OPENAI_API_KEY`; the app ignores it on App Service.
4. **Set startup, health check and identity.**
   - **Configuration → General settings:**
     - **Startup Command:** `python -m deliverytwin serve`
     - **Always On:** On. **HTTPS Only:** On. **FTP:** Disabled.
   - **Monitoring → Health check:** path `/healthz`.
   - **Identity → System assigned:** On.
5. **Build and push the code.** From the repository root:

   ```powershell
   python scripts/package_app.py
   az webapp deploy --resource-group rg-deliverytwin --name deliverytwin-demo `
       --src-path dist/deliverytwin-app.zip --type zip
   ```

   App Service installs `requirements.txt` during the deploy, which takes a few minutes the first time.
   Without the CLI, use **Advanced Tools (Kudu) → Tools → Zip Push Deploy** and drag the zip in.

## Packages from Artifactory (no internet access)

When App Service can't reach the internet, point the deploy-time `pip install` at your Artifactory PyPI
repository. pip reads its index from the `PIP_INDEX_URL` app setting.

1. **Find the index URL.** Use a PyPI *virtual* repository in Artifactory that proxies PyPI:
   `https://<artifactory-host>/artifactory/api/pypi/<pypi-virtual-repo>/simple`.
   If it needs credentials, include them in the URL:
   `https://<user>:<token>@<artifactory-host>/artifactory/api/pypi/<pypi-virtual-repo>/simple`.
2. **Set it before the first code push.**
   - **Scripted:** `./infra/deploy.ps1 … -PipIndexUrl "https://<user>:<token>@<artifactory-host>/artifactory/api/pypi/<repo>/simple"`.
   - **Manual:** add `PIP_INDEX_URL` under **Settings → Environment variables**.
   - **Private certificate authority:** if Artifactory's certificate comes from a private CA that App Service doesn't
     trust, also add `PIP_TRUSTED_HOST=<artifactory-host>`. Trusting your CA is the better fix if your platform team can do it.
3. **Keep the token out of plain settings.** Store the full URL as a Key Vault secret and set the app setting to
   `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>/)`. Grant the web app's managed
   identity **Key Vault Secrets User** on the vault.
4. **Make sure the network allows it.** App Service needs a route to Artifactory, usually VNet integration plus
   DNS for a private host. The install runs in App Service's deployment container, so confirm with your network
   team that deployment traffic also follows that route.
5. **Push the code** (step 5 of the [manual install](#manual-install-portal), or re-run the script). The log stream
   should show pip downloading from your Artifactory host. Check that Artifactory has cached every package in
   `src/requirements.txt` (and `src/requirements-databricks.txt` if you use Databricks).

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

3. Restart the app. The log stream then shows `AI: foundry`.

**Manual equivalent:** set `FOUNDRY_PROJECT_ENDPOINT`
(`https://<resource>.services.ai.azure.com/api/projects/<project>`) and `FOUNDRY_MODEL` (your deployment name),
change `DELIVERYTWIN_FOUNDRY_ENABLED` to `1`, grant the roles above and restart.

### Databricks

Permissions, configuration, the smoke test, the table DDL and the App Service switches are in
[databricks.md](databricks.md).

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

## Check and operate

- **Startup line:** the log stream shows `Data: duckdb · AI: … · Databricks: off · Foundry: …` at startup.
- **Sign-in:** `https://<app>.azurewebsites.net` asks you to sign in, then opens the dashboard.
- **Health check:** if it reports unhealthy because sign-in blocks it, run
  `az webapp auth update -g <rg> -n <app> --excluded-paths "[\"/healthz\"]"`.
- **Code updates:** repeat the code push (manual step 5, or `deploy.ps1 -SkipInfra`).
- **Logs:** run `az webapp log tail -g rg-deliverytwin -n deliverytwin-demo`, or open **Log stream** in the
  portal.
- **Scaling:** keep one instance. DuckDB is a single file with one writer, so scaling out would put several
  writers on the same file. Scale up (a larger SKU) instead. For shared, durable data across instances, use
  the [Databricks data layer](databricks.md).
- **Data:** the database is synthetic. On first start the app creates the schema. Use the dashboard to load
  and simulate data. Deleting `/home/data/deliverytwin.duckdb` (through Kudu/SSH) starts fresh.
- **Secret rotation:** the app registration secret expires after one year. Rotate it with
  `az ad app credential reset` and update `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`.
- **Untested:** none of this has been run against a live subscription yet.

## Container alternative

[`src/Dockerfile`](../src/Dockerfile) builds the same app as an image. To use it, push the image to Azure
Container Registry and create a **Web App for Containers**. Set `WEBSITES_PORT=8000`,
`DELIVERYTWIN_TRUSTED_AUTH=1` (with App Service Authentication on) and `DELIVERYTWIN_DB_PATH=/home/data/deliverytwin.duckdb`,
and enable `WEBSITES_ENABLE_APP_SERVICE_STORAGE` so `/home` persists.
