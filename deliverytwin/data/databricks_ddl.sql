CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_intakes (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_estimate_summary (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_role_estimates (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_resource_assignments (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_approval_dependencies (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.devops_workitems_details (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.project_lessons (
  project_id STRING NOT NULL,
  category STRING NOT NULL,
  lesson STRING,
  suggested_option STRING,
  PRIMARY KEY (project_id, category)
)
USING DELTA
COMMENT 'Extension: lesson per clarification category and the option it supports.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.commitment_baselines (
  baseline_id BIGINT NOT NULL,
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.intake_sessions (
  session_id STRING NOT NULL,
  started_at TIMESTAMP,
  first_rom_at TIMESTAMP,
  approved_at TIMESTAMP,
  PRIMARY KEY (session_id)
)
USING DELTA
COMMENT 'Anonymous intake timing; no request text or document content.';

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.recovery_scenarios (
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

CREATE TABLE IF NOT EXISTS dpm_dev.mlops.track_snapshots (
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
COMMENT 'New: one TRACK outlook per sprint for trend history.';
