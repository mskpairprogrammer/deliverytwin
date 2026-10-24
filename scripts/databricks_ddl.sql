CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_intakes (
  project_id STRING NOT NULL,
  project_name STRING,
  problem_statement_and_desired_outcome STRING,
  overall_scope STRING,
  affected_systems STRING,
  business_strategy STRING,
  estimated_complexity STRING,
  request_category STRING,
  line_of_business STRING,
  known_business_risks STRING,
  known_business_dependencies STRING,
  delivery_pattern STRING,
  submitted_date DATE,
  PRIMARY KEY (project_id)
)
USING DELTA
COMMENT 'Intake form fields; delivery_pattern is an extension used for 6D matching.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_estimate_summary (
  project_id STRING NOT NULL,
  project_name STRING,
  project_size STRING,
  p50_hours BIGINT,
  p80_hours BIGINT,
  actual_hours BIGINT,
  duration_weeks BIGINT,
  actual_duration_weeks BIGINT,
  confidence_score DOUBLE,
  contingency_percent BIGINT,
  estimate_basis STRING,
  assumptions STRING,
  primary_risks STRING,
  outcome STRING,
  PRIMARY KEY (project_id)
)
USING DELTA
COMMENT 'P50/P80 estimate (including contingency) versus actuals; actual_duration_weeks and outcome are extensions.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_role_estimates (
  project_id STRING NOT NULL,
  role STRING NOT NULL,
  p50_hours BIGINT,
  p80_hours BIGINT,
  actual_hours BIGINT,
  average_fte DOUBLE,
  skill_level STRING,
  allocation_note STRING,
  PRIMARY KEY (project_id, role)
)
USING DELTA
COMMENT 'Per-role effort breakdown.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_resource_assignments (
  project_id STRING NOT NULL,
  resource_name STRING NOT NULL,
  role STRING NOT NULL,
  planned_hours BIGINT,
  allocation_percent BIGINT,
  availability_at_start STRING,
  capacity_conflict STRING,
  constraint_note STRING,
  PRIMARY KEY (project_id, resource_name, role)
)
USING DELTA
COMMENT 'Named resource allocations.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_approval_dependencies (
  project_id STRING NOT NULL,
  gate STRING NOT NULL,
  dependency STRING,
  owner STRING,
  status STRING,
  blocking STRING,
  approved_date DATE,
  PRIMARY KEY (project_id, gate)
)
USING DELTA
COMMENT 'Approval gates; dependency is an extension that carries the 6D vocabulary term.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_devops_workitems_details (
  workitem_id STRING NOT NULL,
  prjnumber STRING,
  title STRING,
  workitemtype STRING,
  state STRING,
  scheduling_effort BIGINT,
  tags STRING,
  PRIMARY KEY (workitem_id)
)
USING DELTA
COMMENT 'Epics, features, and stories joined on prjnumber.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_project_lessons (
  project_id STRING NOT NULL,
  category STRING NOT NULL,
  lesson STRING,
  suggested_option STRING,
  PRIMARY KEY (project_id, category)
)
USING DELTA
COMMENT 'Extension: lesson per clarification category and the option it supports.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_commitment_baselines (
  baseline_id STRING NOT NULL,
  request_text STRING,
  answers_json STRING,
  fingerprint_json STRING,
  p50_hours DOUBLE,
  p80_hours DOUBLE,
  integration_buffer_pct DOUBLE,
  allocated_fte DOUBLE,
  capacity_fte DOUBLE,
  target_date DATE,
  confidence_pct DOUBLE,
  approved_at TIMESTAMP,
  PRIMARY KEY (baseline_id)
)
USING DELTA
COMMENT 'Human-approved COMMIT baselines.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_intake_sessions (
  session_id STRING NOT NULL,
  started_at TIMESTAMP,
  first_rom_at TIMESTAMP,
  approved_at TIMESTAMP,
  PRIMARY KEY (session_id)
)
USING DELTA
COMMENT 'Anonymous intake timing; no request text or document content.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_recovery_scenarios (
  scenario_id BIGINT NOT NULL,
  initiative_id STRING,
  recommended_option STRING,
  allocated_fte DOUBLE,
  scope_defer_pct BIGINT,
  target_date DATE,
  incremental_cost_k DOUBLE,
  confidence_pct BIGINT,
  saved_at TIMESTAMP,
  PRIMARY KEY (scenario_id)
)
USING DELTA
COMMENT 'INTERVENE scenarios saved after a what-if preview.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_recovery_target_updates (
  scenario_id BIGINT NOT NULL,
  source_key STRING NOT NULL,
  payload_json STRING,
  PRIMARY KEY (scenario_id, source_key)
)
USING DELTA
COMMENT 'Simulated per-system update records of a saved recovery scenario.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_track_snapshots (
  snapshot_id STRING NOT NULL,
  initiative_id STRING,
  sprint_number BIGINT,
  as_of DATE,
  on_time_pct DOUBLE,
  projected_slip_days BIGINT,
  unplanned_story_points BIGINT,
  support_hours_per_week DOUBLE,
  captured_at TIMESTAMP,
  PRIMARY KEY (snapshot_id)
)
USING DELTA
COMMENT 'One TRACK outlook per sprint, kept for trend history.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_approvals (
  approval_id STRING NOT NULL,
  stage STRING,
  record_id STRING,
  approver STRING,
  approver_source STRING,
  summary_json STRING,
  approved_at TIMESTAMP,
  PRIMARY KEY (approval_id)
)
USING DELTA
COMMENT 'Who approved each COMMIT baseline and applied each INTERVENE plan, and what they approved.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_source_systems (
    source_key STRING PRIMARY KEY,
    display_name STRING NOT NULL,
    integration_mode STRING NOT NULL DEFAULT 'simulated'
,
    writeback_enabled BIGINT NOT NULL DEFAULT 0
,
    created_at STRING NOT NULL DEFAULT CURRENT_TIMESTAMP
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_simulation_state (
    id BIGINT PRIMARY KEY,
    started_on STRING NOT NULL,
    as_of STRING NOT NULL,
    sprints_advanced BIGINT NOT NULL,
    sprint_days BIGINT NOT NULL,
    created_at STRING NOT NULL DEFAULT CURRENT_TIMESTAMP
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_simulation_settings (
    key STRING PRIMARY KEY,
    value STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_simulation_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number BIGINT NOT NULL,
    kind STRING NOT NULL,
    injected_at STRING NOT NULL DEFAULT CURRENT_TIMESTAMP
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ingestion_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key STRING NOT NULL,
    started_at STRING NOT NULL,
    completed_at STRING,
    status STRING NOT NULL,
    records_loaded BIGINT NOT NULL DEFAULT 0,
    error_message STRING
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_delivery_initiatives (
    id STRING PRIMARY KEY,
    name STRING NOT NULL,
    portfolio STRING NOT NULL,
    planned_start STRING NOT NULL,
    target_end STRING NOT NULL,
    baseline_p50_hours DOUBLE NOT NULL,
    baseline_p80_hours DOUBLE NOT NULL,
    integration_buffer_pct DOUBLE NOT NULL,
    initial_confidence_pct DOUBLE NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_delivery_milestones (
    id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    name STRING NOT NULL,
    target_date STRING NOT NULL,
    status STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_people (
    id STRING PRIMARY KEY,
    display_name STRING NOT NULL,
    active BIGINT NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_skills (
    id STRING PRIMARY KEY,
    name STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_person_skills (
    person_id STRING NOT NULL,
    skill_id STRING NOT NULL,
    proficiency STRING NOT NULL,
    PRIMARY KEY (person_id, skill_id)
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_source_entity_map (
    source_key STRING NOT NULL,
    entity_type STRING NOT NULL,
    external_id STRING NOT NULL,
    twin_entity_id STRING NOT NULL,
    PRIMARY KEY (source_key, entity_type, external_id)
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_planview_projects (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    project_status STRING NOT NULL,
    capacity_fte DOUBLE NOT NULL,
    allocated_fte DOUBLE NOT NULL,
    planned_hours DOUBLE NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_planview_milestones (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    name STRING NOT NULL,
    baseline_date STRING NOT NULL,
    status STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_planview_resource_assignments (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    person_id STRING NOT NULL,
    role_name STRING NOT NULL,
    allocated_fte DOUBLE NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ado_projects (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    name STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ado_sprints (
    external_id STRING PRIMARY KEY,
    project_external_id STRING NOT NULL,
    name STRING NOT NULL,
    start_date STRING NOT NULL,
    end_date STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ado_work_items (
    external_id STRING PRIMARY KEY,
    project_external_id STRING NOT NULL,
    sprint_external_id STRING,
    title STRING NOT NULL,
    work_item_type STRING NOT NULL,
    state STRING NOT NULL,
    story_points DOUBLE NOT NULL DEFAULT 0,
    is_unplanned BIGINT NOT NULL DEFAULT 0
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_servicenow_incidents (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    assignee_person_id STRING,
    priority BIGINT NOT NULL,
    state STRING NOT NULL,
    short_description STRING NOT NULL,
    opened_at STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_servicenow_change_requests (
    external_id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    short_description STRING NOT NULL,
    state STRING NOT NULL,
    opened_at STRING NOT NULL,
    planned_start STRING,
    is_blocking BIGINT NOT NULL DEFAULT 0
)
USING DELTA
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_oracle_workers (
    external_id STRING PRIMARY KEY,
    person_id STRING NOT NULL,
    department STRING NOT NULL,
    job_title STRING NOT NULL,
    worker_status STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_oracle_worker_skills (
    worker_external_id STRING NOT NULL,
    skill_id STRING NOT NULL,
    proficiency STRING NOT NULL,
    PRIMARY KEY (worker_external_id, skill_id)
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ukg_shifts (
    external_id STRING PRIMARY KEY,
    person_id STRING NOT NULL,
    shift_date STRING NOT NULL,
    start_time STRING NOT NULL,
    end_time STRING NOT NULL,
    shift_type STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ukg_time_entries (
    external_id STRING PRIMARY KEY,
    person_id STRING NOT NULL,
    initiative_id STRING,
    work_date STRING NOT NULL,
    work_type STRING NOT NULL,
    hours DOUBLE NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_ukg_pto_requests (
    external_id STRING PRIMARY KEY,
    person_id STRING NOT NULL,
    start_date STRING NOT NULL,
    end_date STRING NOT NULL,
    hours DOUBLE NOT NULL,
    status STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_historical_initiatives (
    id STRING PRIMARY KEY,
    name STRING NOT NULL,
    summary STRING NOT NULL,
    structure STRING NOT NULL,
    complexity STRING NOT NULL,
    systems STRING NOT NULL,
    roles STRING NOT NULL,
    dependencies STRING NOT NULL,
    delivery_pattern STRING NOT NULL,
    base_p50_hours DOUBLE NOT NULL,
    base_p80_hours DOUBLE NOT NULL,
    integration_buffer_pct DOUBLE NOT NULL,
    on_time_confidence_pct DOUBLE NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_historical_lessons (
    id STRING PRIMARY KEY,
    initiative_id STRING NOT NULL,
    category STRING NOT NULL,
    lesson STRING NOT NULL,
    suggested_option STRING NOT NULL
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_historical_projects (
    project_id STRING PRIMARY KEY,
    project_name STRING NOT NULL,
    description STRING NOT NULL,
    technologies STRING NOT NULL,
    integrations BIGINT NOT NULL,
    team_size BIGINT NOT NULL,
    planned_duration_weeks BIGINT NOT NULL,
    actual_duration_weeks BIGINT NOT NULL,
    planned_effort_hours BIGINT NOT NULL,
    actual_effort_hours BIGINT NOT NULL,
    outcome STRING NOT NULL,
    lessons_learned STRING NOT NULL,
    structure STRING,
    complexity STRING,
    systems STRING,
    roles STRING,
    dependencies STRING,
    delivery_pattern STRING,
    base_p50_hours DOUBLE,
    base_p80_hours DOUBLE,
    integration_buffer_pct DOUBLE,
    on_time_confidence_pct DOUBLE
)
USING DELTA;

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.dt_historical_project_lessons (
    id STRING PRIMARY KEY,
    project_id STRING NOT NULL,
    category STRING NOT NULL,
    lesson STRING NOT NULL,
    suggested_option STRING NOT NULL
)
USING DELTA;
