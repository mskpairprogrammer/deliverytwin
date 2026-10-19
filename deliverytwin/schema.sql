PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_systems (
    source_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    integration_mode TEXT NOT NULL DEFAULT 'simulated'
        CHECK (integration_mode IN ('simulated', 'connected')),
    writeback_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (writeback_enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS simulation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_on TEXT NOT NULL,
    as_of TEXT NOT NULL,
    sprints_advanced INTEGER NOT NULL CHECK (sprints_advanced >= 0),
    sprint_days INTEGER NOT NULL CHECK (sprint_days > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL REFERENCES source_systems(source_key),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    records_loaded INTEGER NOT NULL DEFAULT 0 CHECK (records_loaded >= 0),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS delivery_initiatives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    target_end TEXT NOT NULL,
    baseline_p50_hours REAL NOT NULL CHECK (baseline_p50_hours >= 0),
    baseline_p80_hours REAL NOT NULL CHECK (baseline_p80_hours >= baseline_p50_hours),
    integration_buffer_pct REAL NOT NULL CHECK (integration_buffer_pct >= 0),
    initial_confidence_pct REAL NOT NULL CHECK (initial_confidence_pct BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS delivery_milestones (
    id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    name TEXT NOT NULL,
    target_date TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS person_skills (
    person_id TEXT NOT NULL REFERENCES people(id),
    skill_id TEXT NOT NULL REFERENCES skills(id),
    proficiency TEXT NOT NULL,
    PRIMARY KEY (person_id, skill_id)
);

CREATE TABLE IF NOT EXISTS source_entity_map (
    source_key TEXT NOT NULL REFERENCES source_systems(source_key),
    entity_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    twin_entity_id TEXT NOT NULL,
    PRIMARY KEY (source_key, entity_type, external_id)
);

CREATE TABLE IF NOT EXISTS planview_projects (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    project_status TEXT NOT NULL,
    capacity_fte REAL NOT NULL CHECK (capacity_fte >= 0),
    allocated_fte REAL NOT NULL CHECK (allocated_fte >= 0),
    planned_hours REAL NOT NULL CHECK (planned_hours >= 0)
);

CREATE TABLE IF NOT EXISTS planview_milestones (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    name TEXT NOT NULL,
    baseline_date TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planview_resource_assignments (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    person_id TEXT NOT NULL REFERENCES people(id),
    role_name TEXT NOT NULL,
    allocated_fte REAL NOT NULL CHECK (allocated_fte >= 0)
);

CREATE TABLE IF NOT EXISTS ado_projects (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ado_sprints (
    external_id TEXT PRIMARY KEY,
    project_external_id TEXT NOT NULL REFERENCES ado_projects(external_id),
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ado_work_items (
    external_id TEXT PRIMARY KEY,
    project_external_id TEXT NOT NULL REFERENCES ado_projects(external_id),
    sprint_external_id TEXT REFERENCES ado_sprints(external_id),
    title TEXT NOT NULL,
    work_item_type TEXT NOT NULL,
    state TEXT NOT NULL,
    story_points REAL NOT NULL DEFAULT 0 CHECK (story_points >= 0),
    is_unplanned INTEGER NOT NULL DEFAULT 0 CHECK (is_unplanned IN (0, 1))
);

CREATE TABLE IF NOT EXISTS servicenow_incidents (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    assignee_person_id TEXT REFERENCES people(id),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
    state TEXT NOT NULL,
    short_description TEXT NOT NULL,
    opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servicenow_change_requests (
    external_id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    short_description TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    planned_start TEXT,
    is_blocking INTEGER NOT NULL DEFAULT 0 CHECK (is_blocking IN (0, 1))
);

CREATE TABLE IF NOT EXISTS oracle_workers (
    external_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL UNIQUE REFERENCES people(id),
    department TEXT NOT NULL,
    job_title TEXT NOT NULL,
    worker_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oracle_worker_skills (
    worker_external_id TEXT NOT NULL REFERENCES oracle_workers(external_id),
    skill_id TEXT NOT NULL REFERENCES skills(id),
    proficiency TEXT NOT NULL,
    PRIMARY KEY (worker_external_id, skill_id)
);

CREATE TABLE IF NOT EXISTS ukg_shifts (
    external_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id),
    shift_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    shift_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ukg_time_entries (
    external_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id),
    initiative_id TEXT REFERENCES delivery_initiatives(id),
    work_date TEXT NOT NULL,
    work_type TEXT NOT NULL,
    hours REAL NOT NULL CHECK (hours >= 0)
);

CREATE TABLE IF NOT EXISTS ukg_pto_requests (
    external_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    hours REAL NOT NULL CHECK (hours >= 0),
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_initiatives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    structure TEXT NOT NULL,
    complexity TEXT NOT NULL CHECK (complexity IN ('low', 'medium', 'high')),
    systems TEXT NOT NULL,
    roles TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    delivery_pattern TEXT NOT NULL,
    base_p50_hours REAL NOT NULL CHECK (base_p50_hours >= 0),
    base_p80_hours REAL NOT NULL CHECK (base_p80_hours >= base_p50_hours),
    integration_buffer_pct REAL NOT NULL CHECK (integration_buffer_pct >= 0),
    on_time_confidence_pct REAL NOT NULL CHECK (on_time_confidence_pct BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS historical_lessons (
    id TEXT PRIMARY KEY,
    initiative_id TEXT NOT NULL REFERENCES historical_initiatives(id),
    category TEXT NOT NULL CHECK (category IN ('architecture', 'data', 'security', 'reuse')),
    lesson TEXT NOT NULL,
    suggested_option TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_projects (
    project_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    description TEXT NOT NULL,
    technologies TEXT NOT NULL,
    integrations INTEGER NOT NULL CHECK (integrations >= 0),
    team_size INTEGER NOT NULL CHECK (team_size > 0),
    planned_duration_weeks INTEGER NOT NULL CHECK (planned_duration_weeks > 0),
    actual_duration_weeks INTEGER NOT NULL CHECK (actual_duration_weeks > 0),
    planned_effort_hours INTEGER NOT NULL CHECK (planned_effort_hours > 0),
    actual_effort_hours INTEGER NOT NULL CHECK (actual_effort_hours > 0),
    outcome TEXT NOT NULL,
    lessons_learned TEXT NOT NULL,
    structure TEXT,
    complexity TEXT CHECK (complexity IS NULL OR complexity IN ('low', 'medium', 'high')),
    systems TEXT,
    roles TEXT,
    dependencies TEXT,
    delivery_pattern TEXT,
    base_p50_hours REAL CHECK (base_p50_hours IS NULL OR base_p50_hours >= 0),
    base_p80_hours REAL CHECK (base_p80_hours IS NULL OR base_p80_hours >= base_p50_hours),
    integration_buffer_pct REAL CHECK (integration_buffer_pct IS NULL OR integration_buffer_pct >= 0),
    on_time_confidence_pct REAL CHECK (
        on_time_confidence_pct IS NULL OR on_time_confidence_pct BETWEEN 0 AND 100
    )
);

CREATE TABLE IF NOT EXISTS historical_project_lessons (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES historical_projects(project_id),
    category TEXT NOT NULL CHECK (category IN ('architecture', 'data', 'security', 'reuse')),
    lesson TEXT NOT NULL,
    suggested_option TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS baseline_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_text TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,
    p50_hours REAL NOT NULL,
    p80_hours REAL NOT NULL,
    integration_buffer_pct REAL NOT NULL,
    allocated_fte REAL NOT NULL,
    capacity_fte REAL NOT NULL,
    target_date TEXT NOT NULL,
    confidence_pct REAL NOT NULL,
    approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    first_rom_at TEXT,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS recovery_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initiative_id TEXT NOT NULL REFERENCES delivery_initiatives(id),
    recommended_option TEXT NOT NULL,
    allocated_fte REAL NOT NULL CHECK (allocated_fte >= 0),
    scope_defer_pct INTEGER NOT NULL CHECK (scope_defer_pct BETWEEN 0 AND 100),
    target_date TEXT NOT NULL,
    incremental_cost_k REAL NOT NULL CHECK (incremental_cost_k >= 0),
    confidence_pct INTEGER NOT NULL CHECK (confidence_pct BETWEEN 0 AND 100),
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_target_updates (
    scenario_id INTEGER NOT NULL REFERENCES recovery_scenarios(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL REFERENCES source_systems(source_key),
    payload_json TEXT NOT NULL,
    PRIMARY KEY (scenario_id, source_key)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_time
    ON ingestion_runs(source_key, started_at);
CREATE INDEX IF NOT EXISTS idx_ado_items_project_unplanned
    ON ado_work_items(project_external_id, is_unplanned);
CREATE INDEX IF NOT EXISTS idx_snow_changes_initiative_state
    ON servicenow_change_requests(initiative_id, state);
CREATE INDEX IF NOT EXISTS idx_ukg_entries_initiative_type_date
    ON ukg_time_entries(initiative_id, work_type, work_date);
CREATE INDEX IF NOT EXISTS idx_intake_sessions_started_at
    ON intake_sessions(started_at);
