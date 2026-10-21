# Switch to Azure Databricks

Only the five application tables move to Unity Catalog / Delta: baselines, intake sessions, recovery scenarios
and their target updates, and TRACK snapshots. The simulated source records and the historical dataset stay in a
local DuckDB file, and models still come from Foundry. No code changes are needed.

## 1. Prepare Databricks

- **Workspace and warehouse:** a workspace with Unity Catalog, and a SQL warehouse. Note its HTTP path from
  **Connection details**, for example `/sql/1.0/warehouses/<id>`.
- **Schema:** a dedicated catalog and schema (default `dpm_dev.mlops`).
- **Identity:** a service principal, or a personal access token for a local test.

### Permissions

| Grant | On | Why |
|---|---|---|
| **CAN USE** | the SQL warehouse | Run queries. |
| **USE CATALOG** | the catalog | Reach the schema. |
| **USE SCHEMA**, **CREATE TABLE**, **SELECT**, **MODIFY** | the schema | Create the tables on first use, read them and upsert into them. |
| **CREATE SCHEMA** | the catalog | Only for the [smoke test](#3-smoke-test), which creates and drops a throwaway schema. |

## 2. Configure

Install the libraries with `python -m pip install -r src/requirements-databricks.txt`, then set:

```
DELIVERYTWIN_DATABRICKS_ENABLED=1
DATABRICKS_HOST=https://adb-<id>.<n>.azuredatabricks.net
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<id>
DATABRICKS_TOKEN=<token>          # or DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET
DELIVERYTWIN_CATALOG=dpm_dev
DELIVERYTWIN_SCHEMA=mlops
```

The data moves only when the switch is `1` and both the host and the HTTP path are set.

## 3. Smoke test

Run `python -m deliverytwin databricks-smoke` from `src/`.

- It creates a throwaway schema, creates the tables, loads the dataset, reads it back and writes a baseline.
- It prints PASS or FAIL for each check, then drops the schema.

The Databricks adapter has so far been tested only against a fake connection, so run the smoke test before
relying on it.

## 4. Switch

Restart the app. The startup line shows `Data: databricks`. Local baselines and scenarios don't migrate, so
approve a baseline again in COMMIT.

## 5. On Azure App Service

- **Scripted:**

  ```powershell
  ./infra/deploy.ps1 -ResourceGroup rg-deliverytwin -AppName deliverytwin-demo -EnableDatabricks `
      -DatabricksHost https://adb-<id>.<n>.azuredatabricks.net -DatabricksHttpPath /sql/1.0/warehouses/<id> `
      -DatabricksToken <token>
  ```

  This also installs the Databricks SQL connector and SDK. Re-running the infrastructure step replaces all
  application settings, so pass the same values each time.
- **Manual:** add the same app settings, package with `python scripts/package_app.py --with-databricks`, and
  deploy the zip (see [manual install](deploy-azure-app-service.md#manual-install-portal)).
- **Token:** store it as a Key Vault reference.
- **Models:** Databricks Model Serving is not used on App Service; models still come from Foundry.

## Table DDL

The app creates each table the first time it uses it, with
`CREATE TABLE IF NOT EXISTS <catalog>.<schema>.dt_<name> (…) USING DELTA`.

- **Columns and keys:** `STRING`, `BIGINT`, `DOUBLE`, `DATE` and `TIMESTAMP` columns, with `NOT NULL` keys and an
  informational `PRIMARY KEY`.
- **Writes:** `MERGE INTO`, so writes are upserts.
- **Creating them in advance:** run [`scripts/databricks_ddl.sql`](../scripts/databricks_ddl.sql) in the SQL editor.
  It has all 12 tables for `dpm_dev.mlops`; regenerate it for another schema with
  `python -m deliverytwin ddl --catalog <c> --schema <s>`.
- **Schema changes:** existing tables are never altered, so a later column change needs a manual `ALTER TABLE`.

## Switch back

Set `DELIVERYTWIN_DATABRICKS_ENABLED=0` and restart. Nothing in the warehouse is deleted, and nothing is copied
back.
