# Delivery Twin prototype

Delivery Twin is a Python prototype for shaping and monitoring a delivery commitment across a simulated portfolio. It combines synthetic records from five enterprise systems into a four-stage dashboard:

| Stage | Purpose |
|---|---|
| **SHAPE** | Clarify a project request, compare it with historical examples, estimate effort, and check a proposed commitment against capacity. |
| **SENSE** | Surface delivery signals and conflicts across the simulated source systems. |
| **FORECAST** | Show a rules-based on-time estimate and projected slip from the current sprint signals. |
| **ACT** | Compare advisory recovery options and show what could be synchronized if a human approved one. |

The workflow orchestration uses Microsoft Agent Framework. The SHAPE calculations and delivery forecast are deterministic; an LLM is optional for intake suggestions and report-grounded chat. All source records are synthetic. The prototype has no live source connectors and does not write to source systems.

## SHAPE: the first dashboard tab

SHAPE turns an informal request into a visible, reviewable baseline through five steps. The dashboard starts with the **SHAPE** tab selected. You can work through intake without configuring an LLM.

1. **Enter the request.** Paste a business request of 10–2,000 characters, or choose **Use sample request**. **Analyze request** checks which of four areas the text appears to specify.
2. **Review clarification questions.** The questions cover **Architecture**, **Data**, **Security**, and **Reuse**. Each has selectable answers, a suggested answer, and evidence from synthetic historical lessons. Review and change every suggestion as needed; the user’s selected answers drive the estimate.
3. **Confirm the 6D delivery fingerprint.** The answers are translated into six dimensions: **structure**, **complexity**, **systems**, **roles**, **dependencies**, and **delivery pattern**. The dashboard ranks the three closest historical initiatives and shows similarity by dimension.
4. **Inspect the explainable ROM.** The rough-order-of-magnitude estimate uses the median P50 and P80 effort from those three analogs, adds their median integration buffer, and rounds the results to the nearest 50 hours. The page shows the base effort, buffer, adjusted estimates, and analog names so the calculation can be reviewed.
5. **Check and approve a commitment.** The estimate is compared with the time remaining to the target date and the available team allocation. Required FTE is calculated from P80 effort at 40 hours per FTE per week. The decision gate only enables **Approve baseline** when the commitment is feasible. Approval is recalculated on the server and saved as a local `baseline_approvals` record; it does not approve or update anything in a source system. After approval, **Continue to SENSE** opens the next stage.

The accompanying SHAPE summary shows P50/P80 effort, integration buffer, target date, initial confidence, allocated capacity, headroom, fingerprint status, and baseline status after the intake has been evaluated. Its displayed assumptions describe the Customer 360 sample scenario (customer profiles across CRM, commerce, and service; identity resolution and privacy controls).

### How SHAPE uses an LLM

The historical lesson retrieval is always keyword overlap against seeded synthetic lessons; it does not use embeddings. If `OPENAI_API_KEY` is configured, SHAPE also asks the OpenAI Chat Completions API to choose a valid suggested answer for each clarification question using the request and the retrieved lesson evidence. The questions, evidence, fingerprint, analog ranking, ROM, and feasibility calculation remain application logic—not LLM-generated estimates. Suggestions are shown for review and can be changed. Without a key, or if the suggestion response cannot be used, SHAPE uses its deterministic keyword-based suggestions.

The SHAPE suggestion feature currently uses OpenAI only. Configuring Microsoft Foundry for dashboard chat does not switch SHAPE suggestions to Foundry.

## Dashboard stages

The remaining tabs use synthetic records from the five simulated sources:

| Source | Example records represented |
|---|---|
| Planview | Customer 360 project, milestones, baseline, and resource allocation |
| Azure DevOps | Projects, sprints, and work items, including unplanned scope |
| ServiceNow | Incidents and change requests, including a simulated CAB blocker |
| Oracle HR / HCM | Workers and skills |
| UKG Pro WFM | Shifts, time entries, and approved time off |

- **SENSE** displays the cross-system finding and the underlying capacity, support, scope, change, and time-off signals. Choose **Simulate next sprint** to load or advance the sample records by one 14-day sprint. Each advance refreshes the inputs used by the later stages.
- **FORECAST** shows the current sprint’s on-time percentage, projected slip in days, baseline P50 effort, unplanned scope, and allocated/available FTE. Its explainable drivers call out workforce drain, scope growth, and change approval delay.
- **ACT** compares three recovery scenarios: contractors, squad reallocation, and phasing/unblocking. It shows illustrative costs, confidence, and trade-offs, highlights a rules-selected recommendation, and lists proposed updates for the milestone, Azure DevOps, ServiceNow, and workforce. These are recommendations and proposed synchronization text only; no option is approved or written back.

The dashboard is keyboard accessible: use the left/right arrow keys to move between tabs, or Home/End to select the first/last tab. The selected stage is reflected in the URL fragment.

## Dashboard screenshots

These screenshots show the four dashboard stages using synthetic Customer 360 data. SHAPE shows the sample request and its suggested clarification answers with historical evidence; the other screenshots show a simulated five-sprint run. Values are illustrative and change as the simulation advances.

### SHAPE · Intake and clarification

![SHAPE intake with the sample request, four clarification areas, suggested answers, and historical evidence](docs/screenshots/shape.png)

### SENSE · Cross-system delivery signals

![SENSE showing a cross-system delivery collision and its workforce, scope, blocker, and source-system signals](docs/screenshots/sense.png)

### FORECAST · Rules-based delivery outlook

![FORECAST showing the on-time estimate, projected slip, explainable drivers, and forecast method](docs/screenshots/forecast.png)

### ACT · Recovery options

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

Pass `--db PATH` before the subcommand to select a different SQLite file. `simulate --reset` clears the evolving simulation state so SENSE, FORECAST, and ACT return to their waiting state. **Clear all demo data** in SHAPE is broader: after browser confirmation, it deletes all rows in the selected local database (including source registrations, history, simulation state, and intake approvals) while preserving the schema. Demo dates are generated relative to the current date.

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
        UI[Four-stage dashboard]
        DB[(SQLite demo database)]
        WF[Agent Framework SHAPE → SENSE → FORECAST → ACT]
        RULES[Deterministic intake, signal, and forecast logic]
        CHAT[Read-only report chat]
        UI --> DB
        DB --> RULES --> WF
        DB --> CHAT
    end
    Sources -. synthetic records only .-> DB
    CHAT --> MODEL[Microsoft Foundry or OpenAI]
    WF --> HUMAN[Human decision required]
    HUMAN -. no source write-back .-> Sources
```

The shared delivery model and simulated source tables are defined in [`deliverytwin/schema.sql`](deliverytwin/schema.sql). The schema also includes `source_systems` and `ingestion_runs` for source mode and load history, `source_entity_map` for mapping source identifiers to shared entities, historical lessons and initiatives for SHAPE, and `baseline_approvals` for locally approved intake baselines.

The lifecycle workflow uses deterministic executors; the LLM is not required to run SHAPE calculations, SENSE, FORECAST, or ACT. The forecast is a transparent heuristic over the current sample signals, not a Monte Carlo forecast or production integration. The collision finding is also rules-based. An approval in SHAPE is recorded only in the local SQLite database; ACT does not approve a recommendation or write to a source.

The local workflow uses in-memory checkpoints. To use the Agent Framework Cosmos checkpoint provider in a deployed instance, set `AZURE_COSMOS_ENDPOINT`; optionally set `AZURE_COSMOS_DATABASE_NAME` and `AZURE_COSMOS_CONTAINER_NAME`. Authentication uses `DefaultAzureCredential`; configure a managed identity with the required Cosmos DB data-plane permissions. The Cosmos provider dependency is prerelease, so pin and validate it for the target environment. SQLite and the synthetic adapters are demo components, not production data infrastructure.

## Container prototype

The included `Dockerfile` serves the dashboard on port 8000. Build and run locally:

```powershell
docker build -t deliverytwin .
docker run --rm -p 8000:8000 deliverytwin
```

For Azure Container Apps, configure ingress on port 8000 and the HTTP health probe at `/healthz`. Configure Foundry chat and Cosmos checkpoints with the environment variables and identity described above as needed. The container stores its demo SQLite database in ephemeral local storage.

## Scope and safety

Every registered source is marked simulated, with `writeback_enabled=0`. There are no live enterprise connectors, production credentials, Monte Carlo forecasts, or source-system write operations. Historical examples and reported values are synthetic and illustrative, not commitments or guarantees. Recovery recommendations require human review.

The sample HTTP server does not implement application authentication or rate limiting. Do not expose it as a production service without adding appropriate identity, authorization, and abuse protections.
