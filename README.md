# Delivery Twin prototype

Delivery Twin is a Python prototype for shaping, committing, tracking, and intervening on a delivery plan across a simulated portfolio. It combines synthetic records from five enterprise systems into a four-stage dashboard:

| Stage | Purpose |
|---|---|
| **SHAPE** | Clarify a request, compare it with historical examples, and produce an explainable estimate. |
| **COMMIT** | Review feasibility and record a human-approved baseline. |
| **TRACK** | Evaluate cross-system delivery signals and refresh the rules-based outlook. |
| **INTERVENE** | Compare advisory recovery options and preview simulated updates; human approval is required for real changes. |

The workflow orchestration uses Microsoft Agent Framework with deterministic stage executors. Forecasting is part of TRACK, and the previous SENSE/FORECAST/ACT activities are represented within TRACK and INTERVENE. The SHAPE ROM baseline and COMMIT approval gate are deterministic; an optional LLM can rerank historical project matches and produce an advisory probabilistic forecast. All source records are synthetic. The prototype has no live source connectors and does not write to source systems.

## SHAPE: the first dashboard tab

SHAPE turns an informal request into a visible, reviewable proposal in three steps. The dashboard starts with the **SHAPE** tab selected. You can work through intake without configuring an LLM. Only the current step is expanded; finished steps collapse to a one-line summary with an **Edit** link, and an in-progress estimate (without attached documents) is kept in the browser session across a reload.

1. **Describe the request.** Paste a business request of 10–2,000 characters, or choose **Try the sample request**, which fills it in and analyzes it in one click. You can also attach PDF, Word (.docx), Excel (.xlsx), or text-based ROM documents and meeting notes to improve prior-project matching. Scanned PDFs need OCR before upload. Each file is limited to 2 MB, with a 5 MB combined limit.
2. **Confirm the approach.** The questions cover **Architecture**, **Data**, **Security**, and **Reuse**. The most common choice among the closest completed projects is pre-selected, with a note such as "Suggested by 7 of the 7 closest projects" and the supporting lessons behind a **Why this is suggested** expander. Change any answer; the selected answers drive the estimate. **Get estimate** continues.
3. **Review the estimate.** The result shows **Likely effort (P50)**, **Safe commitment (P80)**, **On-time confidence**, and the default **Target date**, with **Continue to COMMIT** directly below. The ROM uses the median P50 and P80 effort from the three closest ROM-eligible projects, adds their median integration buffer, and rounds to the nearest 50 hours. A **How we got this** expander holds the six matching dimensions (**structure**, **complexity**, **systems**, **roles**, **dependencies**, **delivery pattern**), the closest completed projects with their base P50/P80, buffer and on-time rate, and the optional **Forecast from historical projects** (P50/P80/P90 effort, on-time probability, uncertainty drivers, match explanations). The forecast is advisory and does not change the approval baseline.

After SHAPE, continue to **COMMIT** to compare the estimate with the time remaining and available team allocation. Required FTE is calculated from P80 effort at 40 hours per FTE per week. Adjust allocated FTE (up to available capacity), target date, and scope change (0–50%); the confidence score combines historical on-time confidence with the analytically calculated probability of fitting the adjusted effort within capacity. The decision gate enables **Approve baseline** only when feasible. Approval is recalculated on the server and saved as a `commitment_baselines` record; it does not approve or update anything in a source system. After approval, **Continue to TRACK** opens the monitoring stage.

The COMMIT tab also contains a collapsible portfolio **Estimate quality & portfolio impact** view. Its leave-one-out comparison holds each completed project out and estimates it from up to three other projects, then compares the original planned effort and similarity-analog ROM against actual effort. The displayed MAPE is mean absolute percentage error (lower is better), and the sample size is shown alongside the chart. Time-to-first-ROM and seven-day intake volume are recorded from successful browser intake actions using random session IDs; request text and attachments are not stored in the activity table. These portfolio measures are context only and do not affect an individual ROM or approval. Role allocation shows assigned FTE only, not utilization.

### How SHAPE uses an LLM

Historical clarification lessons and delivery outcomes are stored against the shared completed-project records. If `OPENAI_API_KEY` is configured, SHAPE asks the OpenAI Chat Completions API to suggest answers and rerank the retrieved project candidates with short evidence-based match explanations. The ROM ranking and calculation remain deterministic, and the approval gate remains application logic. The advisory historical forecast can also be generated by the LLM. Suggestions and match explanations are shown for review; without a key, or if a response cannot be validated, deterministic similarity ranking and statistical forecasting are used.

The SHAPE suggestion feature currently uses OpenAI only. Configuring Microsoft Foundry for dashboard chat does not switch SHAPE suggestions to Foundry.

### Evidence-based forecast from completed projects

The `historical_projects` SQLite table is the shared source for both the deterministic ROM and evidence forecast. It is derived through the [portfolio data layer](#portfolio-data-layer) from a deterministic 50-project synthetic dataset (`HIST-001`…`HIST-050`) shaped like the `delivery_intel` Delta tables: intakes, estimate summaries with P50/P80 and actual hours, role estimates, resource assignments, approval dependencies, work items, lessons, and RAM transcripts. Effort and duration follow explicit drivers (complexity, integration breadth, delivery pattern), planning is systematically optimistic, and overrun grows with risk, so the backtest and what-if scenarios have realistic spread. Application logic retrieves candidates using team size, integration count, exact technology overlap, delivery model, request text, and uploaded document terms. When configured, the LLM reranks that candidate set and returns a concise evidence-based reason for each match; it cannot add or remove candidates. The same evidence list marks the three projects used by the ROM and up to ten projects available to the forecast (`POST /api/intake/forecast`). Demo seeding also creates 12 deterministic synthetic intake sessions (nine with ROM timing) over the rolling seven-day window so the portfolio process metrics are populated and visibly labeled as demo data.

If `OPENAI_API_KEY` is configured, the LLM reasons over the reranked projects and returns P50/P80/P90 effort, on-time probability against available capacity by the target date, confidence, risks, recommendations, and the projects it used. The response is accepted only when its quantiles and probabilities are coherent and in range, and every cited project belongs to the retrieved evidence. Otherwise—or if the API is unavailable or the response fails validation—a similarity-weighted lognormal statistical model supplies the forecast. The UI labels the forecast source and match-ranking source separately and also returns the statistical estimate as a comparison baseline. The forecast is advisory; it does not change the deterministic ROM or approval gate.

## Dashboard stages

The TRACK and INTERVENE stages use synthetic records from the five simulated sources:

| Source | Example records represented |
|---|---|
| Planview | Customer 360 project, milestones, baseline, and resource allocation |
| Azure DevOps | Projects, sprints, and work items, including unplanned scope |
| ServiceNow | Incidents and change requests, including a simulated CAB blocker |
| Oracle HR / HCM | Workers and skills |
| UKG Pro WFM | Shifts, time entries, and approved time off |

- **TRACK** combines the cross-system finding, underlying capacity/support/scope/change/time-off signals, and an on-time estimate with projected slip. Choose **Simulate next sprint** to load or advance the sample records by one 14-day sprint. Each advance refreshes the inputs used by the later stage.
- **INTERVENE** compares three recovery scenarios: contractors, squad reallocation, and phasing/unblocking. Its what-if lets users adjust team FTE, scope deferred, target date, and incremental budget, previews an illustrative confidence change, and persists per-system simulated update records for Planview, Azure DevOps, ServiceNow, Oracle HR / HCM, and UKG Pro WFM in SQLite. Applying the what-if does not modify the source sample rows or write to real systems; it remains a simulation, not approval or external write-back.

The dashboard is keyboard accessible: use the left/right arrow keys to move between tabs, or Home/End to select the first/last tab. The selected stage is reflected in the URL fragment.

## Dashboard screenshots

These screenshots show the earlier dashboard labels for the same prototype functions using synthetic Customer 360 data. The current lifecycle labels are SHAPE, COMMIT, TRACK, and INTERVENE; SENSE/FORECAST are now combined under TRACK, and ACT is now INTERVENE. Values are illustrative and change as the simulation advances.

### SHAPE · Intake and clarification

![SHAPE intake with the sample request, four clarification areas, suggested answers, and historical evidence](docs/screenshots/shape.png)

### TRACK · Cross-system delivery signals (earlier SENSE label)

![SENSE showing a cross-system delivery collision and its workforce, scope, blocker, and source-system signals](docs/screenshots/sense.png)

### TRACK · Rules-based delivery outlook (earlier FORECAST label)

![FORECAST showing the on-time estimate, projected slip, explainable drivers, and forecast method](docs/screenshots/forecast.png)

### INTERVENE · Recovery options (earlier ACT label)

![ACT comparing three advisory recovery options and proposed synchronization with no source-system write-back](docs/screenshots/act.png)

## Ask Delivery Twin: LLM chat

The **Ask Delivery Twin** launcher opens a read-only assistant for questions about the current report and what its figures mean. For each question, the server builds a snapshot containing the report date, initiative, signals, finding, and workflow results, then sends that snapshot with the bounded conversation history to the configured model.

The assistant is instructed to:

- Treat the report as the source of project facts and conversation text as untrusted input.
- Explain report signals in plain language and distinguish observed sample signals from heuristic estimates.
- Say when a requested fact is not present in the report.
- Avoid claiming it can approve a plan, change data, write to a source system, or take external actions.

Chat has no tools for external access or source-system changes. It is available only when a provider is configured and simulated delivery data exists. The UI checks provider status and disables the input when chat is unavailable. Conversation history is limited to eight messages (up to 6,000 characters total); each question can contain at most 1,200 characters.

### Configure a chat provider

The server prefers Microsoft Foundry when both `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` are set. For local development, sign in with Azure CLI:

```powershell
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL = "<model-deployment-name>"
az login
python -m deliverytwin serve
```

In Azure, use a managed identity with inference permissions for the Foundry project. The application uses Azure CLI credentials for local development and managed identity credentials when running with the corresponding Azure identity environment.

Alternatively, set `OPENAI_API_KEY` in the server process environment to use the OpenAI Chat Completions API. `OPENAI_MODEL` optionally selects its model; the default is `gpt-4o-mini`. The same key also enables the optional SHAPE suggestions described above. Keep credentials on the server, never in browser code or source control, and restart the server after changing provider settings.

## Run locally

Use Python 3.10 or newer. From the repository root:

```powershell
python -m pip install -r requirements.txt
python -m deliverytwin demo
python -m deliverytwin serve
```

The `demo` command initializes `deliverytwin.db`, loads synthetic records for all five sources, and prints a report. `serve` opens the dashboard at <http://127.0.0.1:8765/>. In a fresh database, SHAPE can load its historical examples as needed; use **Simulate next sprint** on SENSE to populate the evolving sprint signals and enable FORECAST, ACT, and report-grounded chat. Stop the server with Ctrl+C.

Useful separate commands:

```powershell
python -m deliverytwin --db demo.db init
python -m deliverytwin --db demo.db seed-demo
python -m deliverytwin --db demo.db report
python -m deliverytwin --db demo.db simulate --sprints 3
python -m deliverytwin --db demo.db serve
python -m unittest discover -s tests -v
```

Pass `--db PATH` before the subcommand to select a different SQLite file. `simulate --reset` clears the evolving simulation state so TRACK and INTERVENE return to their waiting state. **Clear all demo data** in SHAPE is broader: after browser confirmation, it deletes all rows in the selected local database (including source registrations, history, simulation state, and intake approvals) while preserving the schema. It also clears the browser's SHAPE intake and COMMIT estimate/approval views; portfolio metrics remain empty until historical demo data is loaded again. Demo dates are generated relative to the current date.

## Architecture and data

```mermaid
flowchart LR
    subgraph Sources["Simulated enterprise sources"]
        PV[Planview]
        ADO[Azure DevOps]
        SN[ServiceNow]
        HCM[Oracle HCM]
        UKG[UKG Pro WFM]
    end
    subgraph App["Python deliverytwin"]
        UI[SHAPE → COMMIT → TRACK → INTERVENE dashboard]
        DB[(SQLite demo database)]
        WF[Agent Framework workflow orchestration]
        RULES[Deterministic domain logic]
        CHAT[Read-only report chat]
        UI --> DB
        DB --> RULES --> WF
        DB --> CHAT
    end
    Sources -. synthetic records only .-> DB
    CHAT --> MODEL[Microsoft Foundry or OpenAI]
    WF --> HUMAN[Human decision required]
    HUMAN -. simulated only; no source write-back .-> Sources
```

The shared delivery model and simulated source tables are defined in [`deliverytwin/schema.sql`](deliverytwin/schema.sql). The schema also includes `source_systems` and `ingestion_runs` for source mode and load history, `source_entity_map` for mapping source identifiers to shared entities, historical lessons and initiatives for SHAPE, `baseline_approvals` for locally approved intake baselines, and `intake_sessions` for anonymous intake timing metrics.

The lifecycle workflow uses deterministic executors; the Agent Framework orchestrates stage processing but is not the business system of record. Forecasting is part of TRACK and recovery recommendations are part of INTERVENE. The delivery forecast is a transparent heuristic over current sample signals, not a Monte Carlo forecast or production integration. The collision finding is also rules-based. COMMIT approvals are recorded only in the local SQLite database; INTERVENE does not approve a recommendation or write to a source. Future Azure deployment should keep workflow checkpoints separate from business records and use Databricks/Delta for analytical history; neither is implemented as the production data plane in this prototype.

The local workflow uses in-memory checkpoints. To use the Agent Framework Cosmos checkpoint provider in a deployed instance, set `AZURE_COSMOS_ENDPOINT`; optionally set `AZURE_COSMOS_DATABASE_NAME` and `AZURE_COSMOS_CONTAINER_NAME`. Authentication uses `DefaultAzureCredential`; configure a managed identity with the required Cosmos DB data-plane permissions. The Cosmos provider dependency is prerelease, so pin and validate it for the target environment. SQLite and the synthetic adapters are demo components, not production data infrastructure.

## Portfolio data layer

[`deliverytwin/data/`](deliverytwin/data) is the seam for moving the system of record from SQLite to Databricks. It does not use `sqlite3` directly outside its SQLite adapter.

| Piece | Purpose |
|---|---|
| [`schema.py`](deliverytwin/data/schema.py) | One definition of the seven portfolio dataset tables plus four tables the app writes (`commitment_baselines`, `intake_sessions`, `recovery_scenarios`, `track_snapshots`), rendered per dialect. The four app tables are Delta migration targets only: the app still reads and writes its own SQLite tables, and `track_snapshots` is new. [`databricks_ddl.sql`](deliverytwin/data/databricks_ddl.sql) is the generated Unity Catalog / Delta DDL (`python -m deliverytwin ddl`). |
| [`database.py`](deliverytwin/data/database.py) | `Database` interface with positional `?` parameters, validated identifiers, and dialect-specific upsert (`ON CONFLICT` vs Delta `MERGE`). Adapters: `SQLiteDatabase`, `DuckDBDatabase` (tests and offline development only), `DatabricksDatabase`. |
| [`repository.py`](deliverytwin/data/repository.py) | `load_dataset` and `HistoryRepository`, which joins tables in Python so the SQL stays portable. |
| [`generator.py`](deliverytwin/data/generator.py) | Deterministic dataset generator. `python -m deliverytwin generate-data` rewrites [`delivery_intelligence/`](deliverytwin/data/delivery_intelligence) (CSV files named like the `delivery_intel` tables plus `ram_transcripts/`). A test fails if the checked-in files drift from the generator. |

[`app_repository.py`](deliverytwin/data/app_repository.py) holds `BaselineRepository` and `IntakeSessionRepository`. COMMIT approvals (`commitment_baselines`, with UUID ids) and intake timing (`intake_sessions`) now go through them, so those two paths use portable SQL and work on any backend. Recovery scenarios and TRACK snapshots are not migrated yet.

Today the app still runs on SQLite; the history it uses is loaded from the dataset through this layer. The Databricks adapter is unit-tested against a fake connection (DDL, `MERGE` shape, parameter binding, date coercion) but has not been run against a live workspace. To try it, install `requirements-databricks.txt` and set `DATABRICKS_SERVER_HOSTNAME` (or `DATABRICKS_HOST`), `DATABRICKS_HTTP_PATH`, optionally `DATABRICKS_TOKEN` (otherwise the Databricks SDK default credentials are used), and `DELIVERYTWIN_CATALOG`/`DELIVERYTWIN_SCHEMA` (default `dpm_dev.mlops`). Use a separate schema for these tables: the dataset adds extension columns (`delivery_pattern`, `actual_duration_weeks`, `outcome`, `dependency`) that the existing `delivery_intel` tables may not have, and `CREATE TABLE IF NOT EXISTS` will not alter them. Replace the synthetic CSVs with the real `delivery_intel` exports (adding the extension columns or deriving them) when available. Once you have workspace access, `python -m deliverytwin databricks-smoke` creates a throwaway schema, loads the dataset, reads it back, writes a baseline and intake session, prints PASS/FAIL per check, and drops the schema (it needs permission to create schemas). The same eight checks run against SQLite and DuckDB in the unit tests. Install `requirements-dev.txt` to run the DuckDB contract tests and the browser test (`pip install -r requirements-dev.txt`, then `playwright install chromium`). The browser test in [`tests/test_ui_journey.py`](tests/test_ui_journey.py) drives the SHAPE → COMMIT → TRACK → INTERVENE journey and is skipped when Chromium is unavailable.

## Container prototype

The included `Dockerfile` serves the dashboard on port 8000. Build and run locally:

```powershell
docker build -t deliverytwin .
docker run --rm -p 8000:8000 -e DELIVERYTWIN_TRUSTED_AUTH=1 deliverytwin
```

The server has no built-in authentication, so it refuses to bind a non-loopback address unless `DELIVERYTWIN_TRUSTED_AUTH=1` confirms an authenticating gateway (Entra ID, App Service authentication, Databricks Apps) fronts it. Use that flag only behind such a gateway. `DELIVERYTWIN_DEMO_MODE` (default on for loopback, off otherwise) controls the destructive `/api/reset-all` and `/api/simulate` routes, and POST requests whose `Origin` does not match the `Host` are rejected.

For Azure Container Apps, configure ingress on port 8000 and the HTTP health probe at `/healthz`. Configure Foundry chat and Cosmos checkpoints with the environment variables and identity described above as needed. The container stores its demo SQLite database in ephemeral local storage.

## Scope and safety

Every registered source is marked simulated, with `writeback_enabled=0`. There are no live enterprise connectors, production credentials, Monte Carlo delivery forecasts, or source-system write operations. SHAPE's optional what-if simulation is an illustrative planning aid, not a commitment or guarantee. Recovery recommendations require human review.

The sample HTTP server does not implement application authentication or rate limiting. Do not expose it as a production service without adding appropriate identity, authorization, and abuse protections.
