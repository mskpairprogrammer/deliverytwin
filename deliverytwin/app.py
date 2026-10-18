from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
INITIATIVE_ID = "autopay-modernization"
SOURCE_SYSTEMS = {
    "planview": ("Planview", ("planview_projects", "planview_milestones", "planview_resource_assignments")),
    "azure_devops": ("Azure DevOps", ("ado_projects", "ado_sprints", "ado_work_items")),
    "servicenow": ("ServiceNow", ("servicenow_incidents", "servicenow_change_requests")),
    "oracle_hcm": ("Oracle HR / HCM", ("oracle_workers", "oracle_worker_skills")),
    "ukg_wfm": ("UKG Pro WFM", ("ukg_shifts", "ukg_time_entries", "ukg_pto_requests")),
}
MAX_CHAT_REQUEST_BYTES = 16_384
MAX_INTAKE_REQUEST_BYTES = 7_500_000
INTAKE_POST_ROUTES = (
    "/api/intake/analyze", "/api/intake/evaluate", "/api/intake/approve", "/api/intake/what-if",
    "/api/intake/forecast",
)
ACT_POST_ROUTES = ("/api/act/what-if", "/api/act/apply")
MAX_CHAT_QUESTION_LENGTH = 1_200
logger = logging.getLogger(__name__)


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign-key enforcement enabled."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection, register_sources: bool = True) -> None:
    """Create the source-system and deliverytwin schema."""
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if register_sources:
        with connection:
            connection.executemany(
                """
                INSERT INTO source_systems (source_key, display_name, integration_mode, writeback_enabled)
                VALUES (?, ?, 'simulated', 0)
                ON CONFLICT(source_key) DO NOTHING
                """,
                [(key, name) for key, (name, _) in SOURCE_SYSTEMS.items()],
            )


def _upsert_many(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple[Any, ...]],
    update_columns: tuple[str, ...],
) -> None:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
    connection.executemany(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders}) "
        f"ON CONFLICT({columns[0]}) DO UPDATE SET {updates}",
        rows,
    )


def seed_demo(connection: sqlite3.Connection, as_of: date | None = None) -> dict[str, Any]:
    """Load the synthetic Customer 360 source-system scenario."""
    initialize_database(connection)
    demo_date = as_of or date.today()
    opened_nine_days_ago = (demo_date - timedelta(days=9)).isoformat()
    target_date = (demo_date + timedelta(days=39)).isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connection:
        _upsert_many(
            connection,
            "delivery_initiatives",
            ("id", "name", "portfolio", "planned_start", "target_end", "baseline_p50_hours",
             "baseline_p80_hours", "integration_buffer_pct", "initial_confidence_pct"),
            [(INITIATIVE_ID, "Customer 360", "Q4 value stream",
              demo_date.isoformat(), target_date, 4450, 4950, 14, 84)],
            ("name", "portfolio", "planned_start", "target_end", "baseline_p50_hours",
             "baseline_p80_hours", "integration_buffer_pct", "initial_confidence_pct"),
        )
        _upsert_many(
            connection,
            "people",
            ("id", "display_name", "active"),
            [("lead-engineer", "Customer 360 Lead", 1),
             ("data-engineer", "Customer Data Engineer", 1),
             ("delivery-engineer", "CRM Integration Engineer", 1),
             ("privacy-engineer", "Customer Privacy Engineer", 1)],
            ("display_name", "active"),
        )
        connection.execute("DELETE FROM person_skills WHERE skill_id = 'kafka'")
        connection.execute("DELETE FROM oracle_worker_skills WHERE skill_id = 'kafka'")
        connection.execute("DELETE FROM skills WHERE id = 'kafka'")
        _upsert_many(
            connection,
            "skills",
            ("id", "name"),
            [("data-engineering", "Data Engineering"),
             ("customer-data-management", "Customer Data Management"),
             ("identity-resolution", "Identity Resolution"),
             ("crm-integration", "CRM Integration"),
             ("privacy-review", "Customer Privacy Review"),
             ("azure", "Azure")],
            ("name",),
        )
        connection.executemany(
            """
            INSERT INTO person_skills (person_id, skill_id, proficiency) VALUES (?, ?, ?)
            ON CONFLICT(person_id, skill_id) DO UPDATE SET proficiency=excluded.proficiency
            """,
            [("data-engineer", "data-engineering", "senior"),
             ("data-engineer", "customer-data-management", "senior"),
             ("data-engineer", "identity-resolution", "senior"),
             ("delivery-engineer", "crm-integration", "senior"),
             ("privacy-engineer", "privacy-review", "senior"),
             ("privacy-engineer", "customer-data-management", "intermediate"),
             ("lead-engineer", "azure", "senior")],
        )
        _upsert_many(
            connection,
            "delivery_milestones",
            ("id", "initiative_id", "name", "target_date", "status"),
            [("shared-api-gateway", INITIATIVE_ID, "", target_date, "at_risk")],
            ("name", "target_date", "status"),
        )

        _upsert_many(
            connection,
            "planview_projects",
            ("external_id", "initiative_id", "project_status", "capacity_fte", "allocated_fte", "planned_hours"),
            [("PV-AUTO-001", INITIATIVE_ID, "approved_baseline", 8.0, 7.5, 4450)],
            ("project_status", "capacity_fte", "allocated_fte", "planned_hours"),
        )
        _upsert_many(
            connection,
            "planview_milestones",
            ("external_id", "initiative_id", "name", "baseline_date", "status"),
            [("PV-MS-API", INITIATIVE_ID, "", target_date, "at_risk")],
            ("name", "baseline_date", "status"),
        )
        _upsert_many(
            connection,
            "planview_resource_assignments",
            ("external_id", "initiative_id", "person_id", "role_name", "allocated_fte"),
            [("PV-RES-LEAD", INITIATIVE_ID, "lead-engineer", "Customer 360 Lead", 2.5),
             ("PV-RES-DATA", INITIATIVE_ID, "data-engineer", "Customer Data Engineer", 2.5),
             ("PV-RES-DELIVERY", INITIATIVE_ID, "delivery-engineer", "CRM Integration Engineer", 2.5)],
            ("person_id", "role_name", "allocated_fte"),
        )

        _upsert_many(
            connection,
            "ado_projects",
            ("external_id", "initiative_id", "name"),
            [("ADO-AUTO", INITIATIVE_ID, "Customer 360")],
            ("initiative_id", "name"),
        )
        sprint_start = (demo_date - timedelta(days=7)).isoformat()
        sprint_end = (demo_date + timedelta(days=7)).isoformat()
        _upsert_many(
            connection,
            "ado_sprints",
            ("external_id", "project_external_id", "name", "start_date", "end_date"),
            [("ADO-SPRINT-DEMO", "ADO-AUTO", "Demo sprint", sprint_start, sprint_end)],
            ("project_external_id", "name", "start_date", "end_date"),
        )
        unplanned_items = [
            (f"ADO-WI-{index:03d}", "ADO-AUTO", "ADO-SPRINT-DEMO",
             f"Customer profile source integration work item {index}", "User Story", "New",
             5 if index <= 8 else 4, 1)
            for index in range(1, 15)
        ]
        _upsert_many(
            connection,
            "ado_work_items",
            ("external_id", "project_external_id", "sprint_external_id", "title",
             "work_item_type", "state", "story_points", "is_unplanned"),
            unplanned_items,
            ("project_external_id", "sprint_external_id", "title", "work_item_type",
             "state", "story_points", "is_unplanned"),
        )

        _upsert_many(
            connection,
            "servicenow_incidents",
            ("external_id", "initiative_id", "assignee_person_id", "priority", "state",
             "short_description", "opened_at"),
            [("INC-P1-001", INITIATIVE_ID, "lead-engineer", 1, "in_progress",
              "Customer profile reconciliation P1 incident", opened_nine_days_ago)],
            ("assignee_person_id", "priority", "state", "short_description", "opened_at"),
        )
        _upsert_many(
            connection,
            "servicenow_change_requests",
            ("external_id", "initiative_id", "short_description", "state", "opened_at",
             "planned_start", "is_blocking"),
            [("CHG-C360-001", INITIATIVE_ID, "Customer identity matching rules deployment", "cab_review",
              opened_nine_days_ago, None, 1)],
            ("short_description", "state", "opened_at", "planned_start", "is_blocking"),
        )

        _upsert_many(
            connection,
            "oracle_workers",
            ("external_id", "person_id", "department", "job_title", "worker_status"),
            [("ORCL-EMP-1001", "lead-engineer", "Customer 360", "Customer 360 Lead", "active"),
             ("ORCL-EMP-1002", "data-engineer", "Customer Data", "Customer Data Engineer", "active"),
             ("ORCL-EMP-1003", "delivery-engineer", "Customer Platforms", "CRM Integration Engineer", "active"),
             ("ORCL-EMP-1004", "privacy-engineer", "Data Governance", "Customer Privacy Engineer", "active")],
            ("person_id", "department", "job_title", "worker_status"),
        )
        connection.executemany(
            """
            INSERT INTO oracle_worker_skills (worker_external_id, skill_id, proficiency)
            VALUES (?, ?, ?)
            ON CONFLICT(worker_external_id, skill_id) DO UPDATE SET proficiency=excluded.proficiency
            """,
            [("ORCL-EMP-1002", "data-engineering", "senior"),
             ("ORCL-EMP-1002", "customer-data-management", "senior"),
             ("ORCL-EMP-1002", "identity-resolution", "senior"),
             ("ORCL-EMP-1003", "crm-integration", "senior"),
             ("ORCL-EMP-1004", "privacy-review", "senior"),
             ("ORCL-EMP-1004", "customer-data-management", "intermediate"),
             ("ORCL-EMP-1001", "azure", "senior")],
        )
        connection.executemany(
            """
            INSERT INTO source_entity_map (source_key, entity_type, external_id, twin_entity_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_key, entity_type, external_id)
            DO UPDATE SET twin_entity_id=excluded.twin_entity_id
            """,
            [("planview", "initiative", "PV-AUTO-001", INITIATIVE_ID),
             ("azure_devops", "initiative", "ADO-AUTO", INITIATIVE_ID),
             ("oracle_hcm", "person", "ORCL-EMP-1001", "lead-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1002", "data-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1003", "delivery-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1004", "privacy-engineer")],
        )

        _upsert_many(
            connection,
            "ukg_shifts",
            ("external_id", "person_id", "shift_date", "start_time", "end_time", "shift_type"),
            [("UKG-SHIFT-001", "lead-engineer", demo_date.isoformat(), "09:00", "17:00", "regular")],
            ("person_id", "shift_date", "start_time", "end_time", "shift_type"),
        )
        _upsert_many(
            connection,
            "ukg_time_entries",
            ("external_id", "person_id", "initiative_id", "work_date", "work_type", "hours"),
            [("UKG-TIME-SUPPORT-001", "lead-engineer", INITIATIVE_ID,
              (demo_date - timedelta(days=4)).isoformat(), "production_support", 8),
             ("UKG-TIME-SUPPORT-002", "lead-engineer", INITIATIVE_ID,
              demo_date.isoformat(), "production_support", 8)],
            ("person_id", "initiative_id", "work_date", "work_type", "hours"),
        )
        _upsert_many(
            connection,
            "ukg_pto_requests",
            ("external_id", "person_id", "start_date", "end_date", "hours", "status"),
            [("UKG-PTO-001", "lead-engineer", target_date, target_date, 40, "approved")],
            ("person_id", "start_date", "end_date", "hours", "status"),
        )

        for source_key, (_, tables) in SOURCE_SYSTEMS.items():
            records_loaded = sum(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            )
            connection.execute(
                """
                INSERT INTO ingestion_runs
                    (source_key, started_at, completed_at, status, records_loaded)
                VALUES (?, ?, ?, 'succeeded', ?)
                """,
                (source_key, stamp, stamp, records_loaded),
            )

    from .intake import seed_historical_data

    seed_historical_data(connection)
    return {"initiative_id": INITIATIVE_ID, "demo_date": demo_date.isoformat()}


SPRINT_DAYS = 14
MAX_SIMULATED_SPRINTS = 20


def _seed_simulated_sprints(
    connection: sqlite3.Connection, start: date, sprints: int
) -> None:
    """Load stable, deterministic source records for every completed and current sprint."""
    with connection:
        connection.execute(
            """
            UPDATE ukg_time_entries SET hours = 0
            WHERE external_id IN ('UKG-TIME-SUPPORT-001', 'UKG-TIME-SUPPORT-002')
            """
        )
        connection.execute(
            "UPDATE servicenow_incidents SET state = 'resolved' WHERE external_id = 'INC-P1-001'"
        )
        connection.execute(
            """
            UPDATE servicenow_change_requests SET state = 'closed', is_blocking = 0
            WHERE external_id = 'CHG-C360-001'
            """
        )

    for sprint_number in range(1, sprints + 1):
        sprint_start = start + timedelta(days=(sprint_number - 1) * SPRINT_DAYS)
        sprint_end = start + timedelta(days=sprint_number * SPRINT_DAYS)
        sprint_id = f"ADO-SIM-SPRINT-{sprint_number:03d}"
        with connection:
            _upsert_many(
                connection,
                "ado_sprints",
                ("external_id", "project_external_id", "name", "start_date", "end_date"),
                [(sprint_id, "ADO-AUTO", f"Sprint {sprint_number}",
                  sprint_start.isoformat(), sprint_end.isoformat())],
                ("project_external_id", "name", "start_date", "end_date"),
            )
            _upsert_many(
                connection,
                "ado_work_items",
                ("external_id", "project_external_id", "sprint_external_id", "title",
                 "work_item_type", "state", "story_points", "is_unplanned"),
                [
                    (f"ADO-SIM-WI-{sprint_number:03d}-{index:03d}", "ADO-AUTO", sprint_id,
                     f"Sprint {sprint_number} customer profile work item {index}",
                     "User Story", "Done" if sprint_number < sprints else "New", 4, 1)
                    for index in range(1, sprint_number * 4 + 1)
                ],
                ("project_external_id", "sprint_external_id", "title", "work_item_type",
                 "state", "story_points", "is_unplanned"),
            )
            _upsert_many(
                connection,
                "ukg_time_entries",
                ("external_id", "person_id", "initiative_id", "work_date", "work_type", "hours"),
                [(f"UKG-SIM-SUPPORT-{sprint_number:03d}", "lead-engineer", INITIATIVE_ID,
                  sprint_end.isoformat(), "production_support", sprint_number * 4)],
                ("person_id", "initiative_id", "work_date", "work_type", "hours"),
            )
            incident_count = min((sprint_number + 1) // 2, 3)
            _upsert_many(
                connection,
                "servicenow_incidents",
                ("external_id", "initiative_id", "assignee_person_id", "priority", "state",
                 "short_description", "opened_at"),
                [
                    (f"INC-SIM-SPRINT-{sprint_number:03d}-{index:02d}", INITIATIVE_ID,
                     "lead-engineer", 1, "in_progress",
                     f"Sprint {sprint_number} customer profile sync incident {index}",
                     (sprint_start + timedelta(days=2)).isoformat())
                    for index in range(1, incident_count + 1)
                ],
                ("initiative_id", "assignee_person_id", "priority", "state",
                 "short_description", "opened_at"),
            )
            change_age_days = 2 + 6 * (sprint_number - 1)
            _upsert_many(
                connection,
                "servicenow_change_requests",
                ("external_id", "initiative_id", "short_description", "state", "opened_at",
                 "planned_start", "is_blocking"),
                [(f"CHG-SIM-SPRINT-{sprint_number:03d}", INITIATIVE_ID,
                  f"Sprint {sprint_number} customer identity matching rules",
                  "cab_review",
                  (sprint_end - timedelta(days=change_age_days)).isoformat(),
                  None, 1)],
                ("initiative_id", "short_description", "state", "opened_at",
                 "planned_start", "is_blocking"),
            )
            if sprint_number == 3:
                _upsert_many(
                    connection,
                    "ukg_pto_requests",
                    ("external_id", "person_id", "start_date", "end_date", "hours", "status"),
                    [(f"UKG-SIM-PTO-{sprint_number:03d}", "lead-engineer",
                      (sprint_end - timedelta(days=2)).isoformat(),
                      (sprint_end - timedelta(days=1)).isoformat(), 40, "approved")],
                    ("person_id", "start_date", "end_date", "hours", "status"),
                )


def simulation_state(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT started_on, as_of, sprints_advanced, sprint_days FROM simulation_state WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def simulated_as_of(connection: sqlite3.Connection) -> date | None:
    state = simulation_state(connection)
    return date.fromisoformat(state["as_of"]) if state else None


def simulate_sprints(
    connection: sqlite3.Connection, sprints: int = 5, start: date | None = None
) -> dict[str, Any]:
    """Load the synthetic source scenario as it stands after N sprints and move the report date there."""
    if sprints < 1:
        raise ValueError("Sprints must be at least 1.")
    from .intake import latest_baseline

    start = start or date.today()
    as_of = start + timedelta(days=sprints * SPRINT_DAYS)
    seed_demo(connection, as_of)
    _seed_simulated_sprints(connection, start, sprints)
    baseline = latest_baseline(connection)
    with connection:
        connection.execute(
            "UPDATE delivery_initiatives SET planned_start = ? WHERE id = ?",
            (start.isoformat(), INITIATIVE_ID),
        )
        if baseline:
            connection.execute(
                """
                UPDATE delivery_initiatives
                SET baseline_p50_hours = ?, baseline_p80_hours = ?, integration_buffer_pct = ?,
                    initial_confidence_pct = ?, target_end = ?
                WHERE id = ?
                """,
                (baseline["p50_hours"], baseline["p80_hours"], baseline["integration_buffer_pct"],
                 baseline["confidence_pct"], baseline["target_date"], INITIATIVE_ID),
            )
        connection.execute(
            """
            INSERT INTO simulation_state (id, started_on, as_of, sprints_advanced, sprint_days)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET started_on = excluded.started_on, as_of = excluded.as_of,
                sprints_advanced = excluded.sprints_advanced, sprint_days = excluded.sprint_days
            """,
            (start.isoformat(), as_of.isoformat(), sprints, SPRINT_DAYS),
        )
    return {
        "started_on": start.isoformat(),
        "as_of": as_of.isoformat(),
        "sprints_advanced": sprints,
        "sprint_days": SPRINT_DAYS,
        "sprint_ends": [
            (start + timedelta(days=SPRINT_DAYS * n)).isoformat() for n in range(1, sprints + 1)
        ],
        "used_approved_baseline": baseline is not None,
    }


def advance_one_sprint(connection: sqlite3.Connection) -> dict[str, Any]:
    """Move the simulation forward exactly one sprint, starting a new one if none exists."""
    state = simulation_state(connection)
    if not state:
        return simulate_sprints(connection, 1)
    if state["sprints_advanced"] >= MAX_SIMULATED_SPRINTS:
        raise ValueError(f"The simulation is already at the {MAX_SIMULATED_SPRINTS}-sprint limit.")
    return simulate_sprints(
        connection, state["sprints_advanced"] + 1, date.fromisoformat(state["started_on"])
    )


def reset_simulation(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("DELETE FROM simulation_state")
    seed_demo(connection)


def clear_demo_data(connection: sqlite3.Connection) -> int:
    """Delete every application data row while preserving the SQLite schema."""
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        deleted = 0
        with connection:
            for table in tables:
                quoted_table = table.replace('"', '""')
                cursor = connection.execute(f'DELETE FROM "{quoted_table}"')
                deleted += max(cursor.rowcount, 0)
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
            ).fetchone():
                connection.execute("DELETE FROM sqlite_sequence")
        return deleted
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def build_report(connection: sqlite3.Connection, as_of: date | None = None) -> dict[str, Any]:
    """Aggregate the synthetic source records into a transparent cross-system signal report."""
    simulation = simulation_state(connection)
    report_date = as_of or (
        date.fromisoformat(simulation["as_of"]) if simulation else date.today()
    )
    if simulation:
        sprint_number = simulation["sprints_advanced"]
        sprint_id = f"ADO-SIM-SPRINT-{sprint_number:03d}"
        sprint_start = (
            date.fromisoformat(simulation["started_on"])
            + timedelta(days=(sprint_number - 1) * simulation["sprint_days"])
        )
    else:
        sprint_id = "ADO-SPRINT-DEMO"
        sprint_start = None
    initiative = connection.execute(
        "SELECT * FROM delivery_initiatives WHERE id = ?", (INITIATIVE_ID,)
    ).fetchone()
    if initiative is None:
        raise RuntimeError("Demo data is missing; run the seed-demo command first.")

    plan = connection.execute(
        """
        SELECT capacity_fte, allocated_fte FROM planview_projects
        WHERE initiative_id = ?
        """,
        (INITIATIVE_ID,),
    ).fetchone()
    scope = connection.execute(
        """
        SELECT COUNT(*) AS item_count, COALESCE(SUM(story_points), 0) AS story_points
        FROM ado_work_items AS wi
        JOIN ado_projects AS p ON p.external_id = wi.project_external_id
        WHERE p.initiative_id = ? AND wi.is_unplanned = 1 AND wi.sprint_external_id = ?
        """,
        (INITIATIVE_ID, sprint_id),
    ).fetchone()
    support = connection.execute(
        """
        SELECT COALESCE(SUM(hours), 0) AS hours
        FROM ukg_time_entries
        WHERE initiative_id = ? AND work_type = 'production_support'
          AND work_date BETWEEN ? AND ?
        """,
        (INITIATIVE_ID, (report_date - timedelta(days=6)).isoformat(), report_date.isoformat()),
    ).fetchone()
    if simulation:
        pto = connection.execute(
            """
            SELECT COALESCE(SUM(hours), 0) AS hours
            FROM ukg_pto_requests
            WHERE person_id = 'lead-engineer' AND status = 'approved'
              AND start_date <= ? AND end_date >= ?
            """,
            (report_date.isoformat(), sprint_start.isoformat()),
        ).fetchone()
        changes = connection.execute(
            """
            SELECT external_id, short_description, opened_at
            FROM servicenow_change_requests
            WHERE external_id = ? AND initiative_id = ? AND is_blocking = 1
              AND state NOT IN ('closed', 'cancelled')
            """,
            (f"CHG-SIM-SPRINT-{simulation['sprints_advanced']:03d}", INITIATIVE_ID),
        ).fetchall()
        incident_filter = "AND external_id LIKE ?"
        incident_parameters: tuple[Any, ...] = (
            INITIATIVE_ID, f"INC-SIM-SPRINT-{simulation['sprints_advanced']:03d}-%",
        )
    else:
        pto = connection.execute(
            """
            SELECT COALESCE(SUM(hours), 0) AS hours
            FROM ukg_pto_requests
            WHERE person_id = 'lead-engineer' AND status = 'approved'
            """,
        ).fetchone()
        changes = connection.execute(
            """
            SELECT external_id, short_description, opened_at
            FROM servicenow_change_requests
            WHERE initiative_id = ? AND is_blocking = 1
              AND state NOT IN ('closed', 'cancelled')
            """,
            (INITIATIVE_ID,),
        ).fetchall()
        incident_filter = ""
        incident_parameters = (INITIATIVE_ID,)
    max_change_age = max(
        ((report_date - date.fromisoformat(row["opened_at"][:10])).days for row in changes),
        default=0,
    )
    incident = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM servicenow_incidents
        WHERE initiative_id = ? AND priority = 1 AND state NOT IN ('resolved', 'closed')
        {incident_filter}
        """,
        incident_parameters,
    ).fetchone()

    signals = {
        "allocated_fte": plan["allocated_fte"] if plan else 0,
        "capacity_fte": plan["capacity_fte"] if plan else 0,
        "unplanned_work_item_count": scope["item_count"],
        "unplanned_story_points": scope["story_points"],
        "lead_production_support_hours_per_week": support["hours"],
        "lead_approved_pto_hours": pto["hours"],
        "active_priority_1_incidents": incident["count"],
        "blocking_change_requests": [
            {
                "external_id": row["external_id"],
                "description": row["short_description"],
                "age_days": (report_date - date.fromisoformat(row["opened_at"][:10])).days,
            }
            for row in changes
        ],
        "oldest_blocking_change_age_days": max_change_age,
    }
    collision_detected = (
        signals["lead_production_support_hours_per_week"] >= 8
        and signals["unplanned_story_points"] >= 32
        and signals["oldest_blocking_change_age_days"] >= 7
    )
    source_modes = [
        dict(row)
        for row in connection.execute(
            "SELECT source_key, display_name, integration_mode, writeback_enabled "
            "FROM source_systems ORDER BY source_key"
        )
    ]
    return {
        "as_of": report_date.isoformat(),
        "initiative": {
            "id": initiative["id"],
            "name": initiative["name"],
            "portfolio": initiative["portfolio"],
            "baseline_p50_hours": initiative["baseline_p50_hours"],
            "baseline_p80_hours": initiative["baseline_p80_hours"],
            "integration_buffer_pct": initiative["integration_buffer_pct"],
            "initial_confidence_pct": initiative["initial_confidence_pct"],
            "target_end": initiative["target_end"],
        },
        "signals": signals,
        "zero_prompt_finding": (
            "Cross-system delivery collision detected: production support is reducing effective "
            "capacity while unplanned scope is growing and a blocking change remains unresolved."
            if collision_detected
            else "No cross-system delivery collision met the configured thresholds."
        ),
        "collision_detected": collision_detected,
        "connectors": source_modes,
        "writeback_performed": False,
        "recovery_scenario": _latest_recovery_scenario(connection),
        **({"simulation": simulation} if simulation else {}),
    }


def build_workflow_report(
    connection: sqlite3.Connection, as_of: date | None = None
) -> dict[str, Any]:
    """Build the deterministic report and run it through the Agent Framework graph."""
    from .workflow import run_delivery_workflow_sync

    return run_delivery_workflow_sync(build_report(connection, as_of))


def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _latest_recovery_scenario(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM recovery_scenarios ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    scenario = dict(row)
    scenario["target_updates"] = [
        {
            "source_key": update["source_key"],
            "system_name": SOURCE_SYSTEMS[update["source_key"]][0],
            "payload": json.loads(update["payload_json"]),
        }
        for update in connection.execute(
            "SELECT source_key, payload_json FROM recovery_target_updates "
            "WHERE scenario_id = ? ORDER BY source_key",
            (row["id"],),
        )
    ]
    scenario["confidence_method"] = (
        "Illustrative adjustment to the current forecast: +5 points per additional FTE, "
        "+0.2 per deferred scope point, and +0.15 per target-date day moved later; bounded to 5–95%."
    )
    scenario["status"] = "simulated_updates_persisted"
    scenario["writeback_performed"] = False
    return scenario


def save_recovery_scenario(
    connection: sqlite3.Connection, scenario: dict[str, Any]
) -> dict[str, Any]:
    """Persist a recovery what-if and its simulated per-system update records in SQLite."""
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO recovery_scenarios
                (initiative_id, recommended_option, allocated_fte, scope_defer_pct,
                 target_date, incremental_cost_k, confidence_pct, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scenario["initiative_id"],
                scenario["recommended_option"],
                scenario["allocated_fte"],
                scenario["scope_defer_pct"],
                scenario["target_date"],
                scenario["incremental_cost_k"],
                scenario["confidence_pct"],
                saved_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO recovery_target_updates (scenario_id, source_key, payload_json)
            VALUES (?, ?, ?)
            """,
            [
                (cursor.lastrowid, update["source_key"], json.dumps(update["payload"]))
                for update in scenario["target_updates"]
            ],
        )
    return _latest_recovery_scenario(connection) or {}


def serve_dashboard(database: str | Path, host: str, port: int) -> None:
    """Serve the read-only dashboard and report endpoint on a local HTTP server."""
    with connect(database) as connection:
        initialize_database(connection, register_sources=False)

    class DashboardHandler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/api/chat/status":
                from .chat import chat_configuration_status

                self.send_json(200, chat_configuration_status())
                return
            if route == "/api/intake/baseline":
                from .intake import latest_baseline

                try:
                    with connect(database) as connection:
                        self.send_json(200, {"baseline": latest_baseline(connection)})
                except (OSError, sqlite3.Error) as error:
                    self.send_json(500, {"error": f"Unable to load baseline: {error}"})
                return
            if route == "/healthz":
                try:
                    with connect(database) as connection:
                        connection.execute("SELECT 1")
                except (OSError, sqlite3.Error) as error:
                    self.send_error(503, f"Dashboard health check failed: {error}")
                    return
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/report":
                try:
                    with connect(database) as connection:
                        state = simulation_state(connection)
                        if state is None:
                            payload = {"simulated": False}
                        else:
                            payload = {**build_workflow_report(connection), "simulated": True,
                                       "simulation": state}
                        body = json.dumps(payload).encode("utf-8")
                except (OSError, sqlite3.Error, RuntimeError) as error:
                    self.send_error(500, f"Unable to build dashboard report: {error}")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route not in ("/", "/index.html"):
                self.send_error(404, "Not found")
                return
            try:
                body = DASHBOARD_PATH.read_bytes()
            except OSError as error:
                self.send_error(500, f"Unable to load dashboard: {error}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json_object(self, max_bytes: int = MAX_CHAT_REQUEST_BYTES) -> dict[str, Any] | None:
            """Validate and parse a JSON object body; sends the error response and returns None on failure."""
            if self.headers.get_content_type() != "application/json":
                self.send_json(415, {"error": "Content-Type must be application/json."})
                return None
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                self.send_json(411, {"error": "Content-Length is required."})
                return None
            try:
                body_length = int(content_length)
            except ValueError:
                self.send_json(400, {"error": "Content-Length must be an integer."})
                return None
            if body_length <= 0:
                self.send_json(400, {"error": "Request body must not be empty."})
                return None
            if body_length > max_bytes:
                self.send_json(413, {"error": "Request is too large."})
                return None
            try:
                payload = json.loads(self.rfile.read(body_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_json(400, {"error": "Request body must be valid UTF-8 JSON."})
                return None
            if not isinstance(payload, dict):
                self.send_json(400, {"error": "Request body must be a JSON object."})
                return None
            return payload

        def handle_intake(self, route: str, payload: dict[str, Any]) -> None:
            from .intake import (
                analyze_intake, approve_baseline, evaluate_intake, forecast_from_history, simulate_rom,
            )

            try:
                with connect(database) as connection:
                    # Intake needs reference history and capacity; load them on demand after a clear.
                    if not connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM historical_projects) "
                        "AND EXISTS(SELECT 1 FROM historical_project_lessons) "
                        "AND EXISTS(SELECT 1 FROM planview_projects)"
                    ).fetchone()[0]:
                        seed_demo(connection)
                    if route == "/api/intake/analyze":
                        result = analyze_intake(connection, payload.get("text"), payload.get("documents"))
                    elif route == "/api/intake/forecast":
                        result = forecast_from_history(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                        )
                    elif route == "/api/intake/what-if":
                        evaluation = evaluate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                        )
                        commitment = evaluation["commitment"]
                        available_hours = (
                            commitment["allocated_fte"]
                            * commitment["weeks_to_target"]
                            * commitment["hours_per_fte_week"]
                        )
                        result = {
                            "simulation": simulate_rom(
                                evaluation["matches"],
                                payload.get("scope_change_pct", 0),
                                available_hours,
                            )
                        }
                    elif route == "/api/intake/evaluate":
                        result = evaluate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                            adjustments=payload.get("adjustments"),
                        )
                    else:
                        result = approve_baseline(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                            adjustments=payload.get("adjustments"),
                        )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except (OSError, sqlite3.Error, RuntimeError) as error:
                self.send_json(500, {"error": f"Unable to process intake request: {error}"})
                return
            self.send_json(200, result)

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            if route not in (
                "/api/chat", "/api/simulate", "/api/reset-all",
                *INTAKE_POST_ROUTES, *ACT_POST_ROUTES,
            ):
                self.send_error(404, "Not found")
                return
            payload = self.read_json_object(
                MAX_INTAKE_REQUEST_BYTES if route in INTAKE_POST_ROUTES else MAX_CHAT_REQUEST_BYTES
            )
            if payload is None:
                return
            if route == "/api/reset-all":
                if payload.get("confirm") is not True:
                    self.send_json(400, {"error": "Explicit confirmation is required to clear demo data."})
                    return
                try:
                    with connect(database) as connection:
                        deleted = clear_demo_data(connection)
                except (OSError, sqlite3.Error) as error:
                    self.send_json(500, {"error": f"Unable to clear demo data: {error}"})
                    return
                self.send_json(200, {"cleared": True, "rows_deleted": deleted})
                return
            if route == "/api/simulate":
                sprints = payload.get("sprints", 5)
                if (
                    not isinstance(sprints, int)
                    or isinstance(sprints, bool)
                    or not 1 <= sprints <= MAX_SIMULATED_SPRINTS
                ):
                    self.send_json(
                        400, {"error": f"Sprints must be a whole number from 1 to {MAX_SIMULATED_SPRINTS}."}
                    )
                    return
                try:
                    with connect(database) as connection:
                        if payload.get("reset") is True:
                            reset_simulation(connection)
                            result: dict[str, Any] = {"simulated": False}
                        elif payload.get("next_sprint") is True:
                            result = {"simulated": True, **advance_one_sprint(connection)}
                        else:
                            result = {"simulated": True, **simulate_sprints(connection, sprints)}
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, sqlite3.Error, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to run simulation: {error}"})
                    return
                self.send_json(200, result)
                return
            if route in ACT_POST_ROUTES:
                from .recovery import preview_recovery_scenario

                try:
                    with connect(database) as connection:
                        if simulation_state(connection) is None:
                            raise ValueError("Simulate a sprint before creating an ACT recovery scenario.")
                        report = build_workflow_report(connection)
                        result = preview_recovery_scenario(report, payload)
                        if route == "/api/act/apply":
                            result = save_recovery_scenario(connection, result)
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, sqlite3.Error, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to process recovery scenario: {error}"})
                    return
                self.send_json(200, result)
                return
            if route in INTAKE_POST_ROUTES:
                self.handle_intake(route, payload)
                return

            question = payload.get("question")
            if not isinstance(question, str) or not question.strip():
                self.send_json(400, {"error": "A non-empty question is required."})
                return
            if len(question) > MAX_CHAT_QUESTION_LENGTH:
                self.send_json(
                    413,
                    {
                        "error": (
                            f"Question must be no longer than "
                            f"{MAX_CHAT_QUESTION_LENGTH} characters."
                        )
                    },
                )
                return

            from .chat import (
                ChatNotConfiguredError,
                answer_delivery_question_sync,
                chat_configuration_status,
                validate_chat_history,
            )

            try:
                history = validate_chat_history(payload.get("history"))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            configuration = chat_configuration_status()
            if not configuration["available"]:
                self.send_json(503, {"error": configuration["message"]})
                return

            try:
                with connect(database) as connection:
                    if simulation_state(connection) is None:
                        self.send_json(
                            409,
                            {"error": "No delivery data yet. Run simulate.py to load test data first."},
                        )
                        return
                    report = build_workflow_report(connection)
            except (OSError, sqlite3.Error, RuntimeError) as error:
                self.send_json(500, {"error": f"Unable to load report context: {error}"})
                return

            try:
                answer = answer_delivery_question_sync(question, history, report)
            except ChatNotConfiguredError as error:
                self.send_json(503, {"error": str(error)})
                return
            except Exception:
                logger.exception("Delivery Twin Foundry chat request failed")
                self.send_json(
                    502,
                    {
                        "error": (
                            "The Foundry chat request failed. Check server logs, "
                            "endpoint/model settings, and Azure identity permissions."
                        )
                    },
                )
                return
            self.send_json(200, {"answer": answer})

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[dashboard] {self.address_string()} - {format % args}")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Delivery Twin dashboard: http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Delivery Twin dashboard.", flush=True)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic Delivery Twin source-system demo")
    parser.add_argument("--db", default="deliverytwin.db", help="SQLite database file (default: deliverytwin.db)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create the SQLite schema and register simulated sources")
    subparsers.add_parser("seed-demo", help="Load synthetic records from all five source systems")
    subparsers.add_parser("report", help="Print the cross-system delivery signal report")
    subparsers.add_parser("demo", help="Initialize, seed synthetic data, and print a report")
    simulate_parser = subparsers.add_parser(
        "simulate", help="Load synthetic test data and move the date forward by N sprints"
    )
    simulate_parser.add_argument("--sprints", type=int, default=5, help="Sprints to advance (default: 5)")
    simulate_parser.add_argument("--reset", action="store_true", help="Clear the simulation so the other tabs go blank")
    serve_parser = subparsers.add_parser("serve", help="Run the local read-only web dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind address")
    serve_parser.add_argument("--port", type=int, default=8765, help="Dashboard port")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve_dashboard(args.db, args.host, args.port)
        return 0

    with connect(args.db) as connection:
        if args.command == "init":
            initialize_database(connection)
            _write_json({"database": args.db, "initialized": True})
        elif args.command == "seed-demo":
            _write_json(seed_demo(connection))
        elif args.command == "simulate":
            initialize_database(connection)
            if args.reset:
                reset_simulation(connection)
                _write_json({"simulation": None, "message": "Simulation cleared; dashboard tabs are blank."})
            else:
                try:
                    _write_json(simulate_sprints(connection, args.sprints))
                except ValueError as error:
                    parser.error(str(error))
        elif args.command == "report":
            _write_json(build_workflow_report(connection))
        else:
            seed_info = seed_demo(connection)
            _write_json({"seed": seed_info, "report": build_workflow_report(connection)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
