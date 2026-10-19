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
