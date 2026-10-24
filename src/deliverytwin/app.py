from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .storage.connection import Connection, default_database_path, resolve_dialect
from .storage.connection import connect as open_connection
from .storage.database import DatabaseError
from .approvals import approver_from_headers
from .portfolio import PREEMPT_SPRINTS, planned_loss_hours, portfolio_view, preemption_active, set_preemption
from .extras import (
    EXTRA_GET_ROUTES, EXTRA_POST_ROUTES, SIMULATION_ROUTES, BaselineRequired, handle_get, handle_post,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
INITIATIVE_ID = "autopay-modernization"
SOURCE_SYSTEMS = {
    "planview": ("Planview", ("dt_planview_projects", "dt_planview_milestones", "dt_planview_resource_assignments")),
    "azure_devops": ("Azure DevOps", ("dt_ado_projects", "dt_ado_sprints", "dt_ado_work_items")),
    "servicenow": ("ServiceNow", ("dt_servicenow_incidents", "dt_servicenow_change_requests")),
    "oracle_hcm": ("Oracle HR / HCM", ("dt_oracle_workers", "dt_oracle_worker_skills")),
    "ukg_wfm": ("UKG Pro WFM", ("dt_ukg_shifts", "dt_ukg_time_entries", "dt_ukg_pto_requests")),
}
MAX_CHAT_REQUEST_BYTES = 16_384
MAX_INTAKE_REQUEST_BYTES = 7_500_000
INTAKE_POST_ROUTES = (
    "/api/intake/analyze", "/api/intake/evaluate", "/api/intake/approve", "/api/intake/what-if",
    "/api/intake/forecast", "/api/intake/estimate", "/api/intake/challenge", "/api/intake/ask",
)
ACT_POST_ROUTES = ("/api/act/analyze", "/api/act/what-if", "/api/act/apply", "/api/act/tune")
TRACK_POST_ROUTES = ("/api/track/brief",)
COMMIT_POST_ROUTES = ("/api/commit/backlog",)
MAX_CHAT_QUESTION_LENGTH = 1_200
logger = logging.getLogger(__name__)
BASELINE_REQUIRED = "Approve a baseline in COMMIT before tracking delivery or reviewing interventions."


class BaselineRequiredError(Exception):
    """TRACK and INTERVENE only work against an approved COMMIT baseline."""


def baseline_approved(connection: Connection) -> bool:
    from .intake import latest_baseline

    return latest_baseline(connection) is not None


def connect(database: str | Path | None = None, dialect: str | None = None) -> Connection:
    """Open the configured database: DuckDB by default, or Databricks when enabled."""
    return open_connection(database, dialect)


def initialize_database(connection: Connection, register_sources: bool = True) -> None:
    """Create the source-system and deliverytwin schema."""
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if register_sources:
        with connection:
            connection.upsert_rows(
                "dt_source_systems",
                ("source_key", "display_name", "integration_mode", "writeback_enabled"),
                ("source_key",),
                [(key, name, "simulated", 0) for key, (name, _) in SOURCE_SYSTEMS.items()],
                (),
            )


def _upsert_many(
    connection: Connection,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple[Any, ...]],
    update_columns: tuple[str, ...],
) -> None:
    connection.upsert_rows(table, columns, (columns[0],), rows, update_columns)


def seed_demo(connection: Connection, as_of: date | None = None) -> dict[str, Any]:
    """Load the synthetic Customer 360 source-system scenario."""
    from .analytics import seed_synthetic_intake_activity

    initialize_database(connection)
    seed_synthetic_intake_activity(connection)
    demo_date = as_of or date.today()
    opened_nine_days_ago = (demo_date - timedelta(days=9)).isoformat()
    target_date = (demo_date + timedelta(days=39)).isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connection:
        _upsert_many(
            connection,
            "dt_delivery_initiatives",
            ("id", "name", "portfolio", "planned_start", "target_end", "baseline_p50_hours",
             "baseline_p80_hours", "integration_buffer_pct", "initial_confidence_pct"),
            [(INITIATIVE_ID, "Customer 360", "Q4 value stream",
              demo_date.isoformat(), target_date, 4650, 5750, 19, 62)],
            ("name", "portfolio", "planned_start", "target_end", "baseline_p50_hours",
             "baseline_p80_hours", "integration_buffer_pct", "initial_confidence_pct"),
        )
        _upsert_many(
            connection,
            "dt_people",
            ("id", "display_name", "active"),
            [("lead-engineer", "Customer 360 Lead", 1),
             ("data-engineer", "Customer Data Engineer", 1),
             ("delivery-engineer", "CRM Integration Engineer", 1),
             ("privacy-engineer", "Customer Privacy Engineer", 1)],
            ("display_name", "active"),
        )
        connection.execute("DELETE FROM dt_person_skills WHERE skill_id = 'kafka'")
        connection.execute("DELETE FROM dt_oracle_worker_skills WHERE skill_id = 'kafka'")
        connection.execute("DELETE FROM dt_skills WHERE id = 'kafka'")
        _upsert_many(
            connection,
            "dt_skills",
            ("id", "name"),
            [("data-engineering", "Data Engineering"),
             ("customer-data-management", "Customer Data Management"),
             ("identity-resolution", "Identity Resolution"),
             ("crm-integration", "CRM Integration"),
             ("privacy-review", "Customer Privacy Review"),
             ("azure", "Azure")],
            ("name",),
        )
        connection.upsert_rows(
            "dt_person_skills", ("person_id", "skill_id", "proficiency"), ("person_id", "skill_id"),
            [("data-engineer", "data-engineering", "senior"),
             ("data-engineer", "customer-data-management", "senior"),
             ("data-engineer", "identity-resolution", "senior"),
             ("delivery-engineer", "crm-integration", "senior"),
             ("privacy-engineer", "privacy-review", "senior"),
             ("privacy-engineer", "customer-data-management", "intermediate"),
             ("lead-engineer", "azure", "senior")],
            ("proficiency",),
        )
        _upsert_many(
            connection,
            "dt_delivery_milestones",
            ("id", "initiative_id", "name", "target_date", "status"),
            [("customer-profile-api-v1", INITIATIVE_ID, "Customer profile API v1", target_date, "at_risk")],
            ("name", "target_date", "status"),
        )

        _upsert_many(
            connection,
            "dt_planview_projects",
            ("external_id", "initiative_id", "project_status", "capacity_fte", "allocated_fte", "planned_hours"),
            [("PV-AUTO-001", INITIATIVE_ID, "approved_baseline", 8.5, 7.0, 4650)],
            ("project_status", "capacity_fte", "allocated_fte", "planned_hours"),
        )
        _upsert_many(
            connection,
            "dt_planview_milestones",
            ("external_id", "initiative_id", "name", "baseline_date", "status"),
            [("PV-MS-API", INITIATIVE_ID, "Customer profile API v1", target_date, "at_risk")],
            ("name", "baseline_date", "status"),
        )
        _upsert_many(
            connection,
            "dt_planview_resource_assignments",
            ("external_id", "initiative_id", "person_id", "role_name", "allocated_fte"),
            [("PV-RES-LEAD", INITIATIVE_ID, "lead-engineer", "Customer 360 Lead", 1.0),
             ("PV-RES-DATA", INITIATIVE_ID, "data-engineer", "Customer Data Engineer", 2.0),
             ("PV-RES-DELIVERY", INITIATIVE_ID, "delivery-engineer", "CRM Integration Engineer", 2.8),
             ("PV-RES-PRIVACY", INITIATIVE_ID, "privacy-engineer", "Customer Privacy Engineer", 1.2)],
            ("person_id", "role_name", "allocated_fte"),
        )

        _upsert_many(
            connection,
            "dt_ado_projects",
            ("external_id", "initiative_id", "name"),
            [("ADO-AUTO", INITIATIVE_ID, "Customer 360")],
            ("initiative_id", "name"),
        )
        sprint_start = (demo_date - timedelta(days=7)).isoformat()
        sprint_end = (demo_date + timedelta(days=7)).isoformat()
        _upsert_many(
            connection,
            "dt_ado_sprints",
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
            "dt_ado_work_items",
            ("external_id", "project_external_id", "sprint_external_id", "title",
             "work_item_type", "state", "story_points", "is_unplanned"),
            unplanned_items,
            ("project_external_id", "sprint_external_id", "title", "work_item_type",
             "state", "story_points", "is_unplanned"),
        )

        _upsert_many(
            connection,
            "dt_servicenow_incidents",
            ("external_id", "initiative_id", "assignee_person_id", "priority", "state",
             "short_description", "opened_at"),
            [("INC-P1-001", INITIATIVE_ID, "lead-engineer", 1, "in_progress",
              "Customer profile reconciliation P1 incident", opened_nine_days_ago)],
            ("assignee_person_id", "priority", "state", "short_description", "opened_at"),
        )
        _upsert_many(
            connection,
            "dt_servicenow_change_requests",
            ("external_id", "initiative_id", "short_description", "state", "opened_at",
             "planned_start", "is_blocking"),
            [("CHG-C360-001", INITIATIVE_ID, "Customer identity matching rules deployment", "cab_review",
              opened_nine_days_ago, None, 1)],
            ("short_description", "state", "opened_at", "planned_start", "is_blocking"),
        )

        _upsert_many(
            connection,
            "dt_oracle_workers",
            ("external_id", "person_id", "department", "job_title", "worker_status"),
            [("ORCL-EMP-1001", "lead-engineer", "Customer 360", "Customer 360 Lead", "active"),
             ("ORCL-EMP-1002", "data-engineer", "Customer Data", "Customer Data Engineer", "active"),
             ("ORCL-EMP-1003", "delivery-engineer", "Customer Platforms", "CRM Integration Engineer", "active"),
             ("ORCL-EMP-1004", "privacy-engineer", "Data Governance", "Customer Privacy Engineer", "active")],
            ("person_id", "department", "job_title", "worker_status"),
        )
        connection.upsert_rows(
            "dt_oracle_worker_skills", ("worker_external_id", "skill_id", "proficiency"),
            ("worker_external_id", "skill_id"),
            [("ORCL-EMP-1002", "data-engineering", "senior"),
             ("ORCL-EMP-1002", "customer-data-management", "senior"),
             ("ORCL-EMP-1002", "identity-resolution", "senior"),
             ("ORCL-EMP-1003", "crm-integration", "senior"),
             ("ORCL-EMP-1004", "privacy-review", "senior"),
             ("ORCL-EMP-1004", "customer-data-management", "intermediate"),
             ("ORCL-EMP-1001", "azure", "senior")],
            ("proficiency",),
        )
        connection.upsert_rows(
            "dt_source_entity_map",
            ("source_key", "entity_type", "external_id", "twin_entity_id"),
            ("source_key", "entity_type", "external_id"),
            [("planview", "initiative", "PV-AUTO-001", INITIATIVE_ID),
             ("azure_devops", "initiative", "ADO-AUTO", INITIATIVE_ID),
             ("oracle_hcm", "person", "ORCL-EMP-1001", "lead-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1002", "data-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1003", "delivery-engineer"),
             ("oracle_hcm", "person", "ORCL-EMP-1004", "privacy-engineer")],
            ("twin_entity_id",),
        )

        _upsert_many(
            connection,
            "dt_ukg_shifts",
            ("external_id", "person_id", "shift_date", "start_time", "end_time", "shift_type"),
            [("UKG-SHIFT-001", "lead-engineer", demo_date.isoformat(), "09:00", "17:00", "regular")],
            ("person_id", "shift_date", "start_time", "end_time", "shift_type"),
        )
        _upsert_many(
            connection,
            "dt_ukg_time_entries",
            ("external_id", "person_id", "initiative_id", "work_date", "work_type", "hours"),
            [("UKG-TIME-SUPPORT-001", "lead-engineer", INITIATIVE_ID,
              (demo_date - timedelta(days=4)).isoformat(), "production_support", 8),
             ("UKG-TIME-SUPPORT-002", "lead-engineer", INITIATIVE_ID,
              demo_date.isoformat(), "production_support", 8)],
            ("person_id", "initiative_id", "work_date", "work_type", "hours"),
        )
        _upsert_many(
            connection,
            "dt_ukg_pto_requests",
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
                INSERT INTO dt_ingestion_runs
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


def _purge_simulated_rows(connection: Connection) -> None:
    """Remove every simulated record so a scenario change never leaves rows of the previous one behind.

    Children are deleted before parents, each in its own statement: DuckDB cannot delete a child and then its
    parent inside one transaction while foreign keys are enforced.
    """
    for table, pattern in (
        ("dt_ado_work_items", "ADO-SIM-WI-%"), ("dt_ado_sprints", "ADO-SIM-SPRINT-%"),
        ("dt_ukg_time_entries", "UKG-SIM-%"), ("dt_servicenow_incidents", "INC-SIM-%"),
        ("dt_servicenow_change_requests", "CHG-SIM-%"), ("dt_ukg_pto_requests", "UKG-SIM-PTO-%"),
    ):
        connection.execute(f"DELETE FROM {table} WHERE external_id LIKE ?", (pattern,))


def _seed_simulated_sprints(
    connection: Connection, start: date, sprints: int
) -> None:
    """Load stable, deterministic source records for every completed and current sprint."""
    from .scenarios import IMPORT_SCENARIO, effective_signals, list_events, scenario_key

    _purge_simulated_rows(connection)
    scenario = scenario_key(connection)
    events = list_events(connection)
    with connection:
        connection.execute(
            """
            UPDATE dt_ukg_time_entries SET hours = 0
            WHERE external_id IN ('UKG-TIME-SUPPORT-001', 'UKG-TIME-SUPPORT-002')
            """
        )
        connection.execute(
            "UPDATE dt_servicenow_incidents SET state = 'resolved' WHERE external_id = 'INC-P1-001'"
        )
        connection.execute(
            """
            UPDATE dt_servicenow_change_requests SET state = 'closed', is_blocking = 0
            WHERE external_id = 'CHG-C360-001'
            """
        )
    if scenario == IMPORT_SCENARIO:
        return

    for sprint_number in range(1, sprints + 1):
        signals = effective_signals(scenario, sprint_number, events)
        sprint_start = start + timedelta(days=(sprint_number - 1) * SPRINT_DAYS)
        sprint_end = start + timedelta(days=sprint_number * SPRINT_DAYS)
        sprint_id = f"ADO-SIM-SPRINT-{sprint_number:03d}"
        with connection:
            _upsert_many(
                connection,
                "dt_ado_sprints",
                ("external_id", "project_external_id", "name", "start_date", "end_date"),
                [(sprint_id, "ADO-AUTO", f"Sprint {sprint_number}",
                  sprint_start.isoformat(), sprint_end.isoformat())],
                ("project_external_id", "name", "start_date", "end_date"),
            )
            _upsert_many(
                connection,
                "dt_ado_work_items",
                ("external_id", "project_external_id", "sprint_external_id", "title",
                 "work_item_type", "state", "story_points", "is_unplanned"),
                [
                    (f"ADO-SIM-WI-{sprint_number:03d}-{index:03d}", "ADO-AUTO", sprint_id,
                     f"Sprint {sprint_number} customer profile work item {index}",
                     "User Story", "Done" if sprint_number < sprints else "New", 4, 1)
                    for index in range(1, signals.unplanned_items + 1)
                ],
                ("project_external_id", "sprint_external_id", "title", "work_item_type",
                 "state", "story_points", "is_unplanned"),
            )
            _upsert_many(
                connection,
                "dt_ukg_time_entries",
                ("external_id", "person_id", "initiative_id", "work_date", "work_type", "hours"),
                [(f"UKG-SIM-SUPPORT-{sprint_number:03d}", "lead-engineer", INITIATIVE_ID,
                  sprint_end.isoformat(), "production_support", signals.support_hours)],
                ("person_id", "initiative_id", "work_date", "work_type", "hours"),
            )
            _upsert_many(
                connection,
                "dt_servicenow_incidents",
                ("external_id", "initiative_id", "assignee_person_id", "priority", "state",
                 "short_description", "opened_at"),
                [
                    (f"INC-SIM-SPRINT-{sprint_number:03d}-{index:02d}", INITIATIVE_ID,
                     "lead-engineer", 1, "in_progress",
                     f"Sprint {sprint_number} customer profile sync incident {index}",
                     (sprint_start + timedelta(days=2)).isoformat())
                    for index in range(1, signals.p1_incidents + 1)
                ],
                ("initiative_id", "assignee_person_id", "priority", "state",
                 "short_description", "opened_at"),
            )
            if signals.change_age_days is not None:
                _upsert_many(
                    connection,
                    "dt_servicenow_change_requests",
                    ("external_id", "initiative_id", "short_description", "state", "opened_at",
                     "planned_start", "is_blocking"),
                    [(f"CHG-SIM-SPRINT-{sprint_number:03d}", INITIATIVE_ID,
                      f"Sprint {sprint_number} customer identity matching rules",
                      "cab_review",
                      (sprint_end - timedelta(days=signals.change_age_days)).isoformat(),
                      None, 1)],
                    ("initiative_id", "short_description", "state", "opened_at",
                     "planned_start", "is_blocking"),
                )
            if signals.pto_hours:
                _upsert_many(
                    connection,
                    "dt_ukg_pto_requests",
                    ("external_id", "person_id", "start_date", "end_date", "hours", "status"),
                    [(f"UKG-SIM-PTO-{sprint_number:03d}", "lead-engineer",
                      (sprint_end - timedelta(days=2)).isoformat(),
                      (sprint_end - timedelta(days=1)).isoformat(), signals.pto_hours, "approved")],
                    ("person_id", "start_date", "end_date", "hours", "status"),
                )


def simulation_state(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT started_on, as_of, sprints_advanced, sprint_days FROM dt_simulation_state WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def simulated_as_of(connection: Connection) -> date | None:
    state = simulation_state(connection)
    return date.fromisoformat(state["as_of"]) if state else None


def simulate_sprints(
    connection: Connection, sprints: int = 5, start: date | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Load the synthetic source scenario as it stands after N sprints and move the report date there."""
    if sprints < 1:
        raise ValueError("Sprints must be at least 1.")
    from .intake import latest_baseline
    from .scenarios import IMPORT_SCENARIO, SCENARIOS, list_events, scenario_key, set_scenario

    if scenario is not None:
        from .scenarios import clear_events

        if scenario not in SCENARIOS and scenario != IMPORT_SCENARIO:
            raise ValueError(f"Unknown scenario. Choose one of: {', '.join(SCENARIOS)}.")
        if scenario != scenario_key(connection):
            clear_events(connection)
            set_preemption(connection, False)
        set_scenario(connection, scenario)
    start = start or date.today()
    as_of = start + timedelta(days=sprints * SPRINT_DAYS)
    seed_demo(connection, as_of)
    _seed_simulated_sprints(connection, start, sprints)
    baseline = latest_baseline(connection)
    with connection:
        connection.execute(
            "UPDATE dt_delivery_initiatives SET planned_start = ? WHERE id = ?",
            (start.isoformat(), INITIATIVE_ID),
        )
        if baseline:
            connection.execute(
                """
                UPDATE dt_delivery_initiatives
                SET baseline_p50_hours = ?, baseline_p80_hours = ?, integration_buffer_pct = ?,
                    initial_confidence_pct = ?, target_end = ?
                WHERE id = ?
                """,
                (baseline["p50_hours"], baseline["p80_hours"], baseline["integration_buffer_pct"],
                 baseline["confidence_pct"], baseline["target_date"], INITIATIVE_ID),
            )
        connection.upsert_rows(
            "dt_simulation_state", ("id", "started_on", "as_of", "sprints_advanced", "sprint_days"),
            ("id",),
            [(1, start.isoformat(), as_of.isoformat(), sprints, SPRINT_DAYS)],
            ("started_on", "as_of", "sprints_advanced", "sprint_days"),
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
        "scenario": scenario_key(connection),
        "events": list_events(connection),
    }


def advance_one_sprint(connection: Connection) -> dict[str, Any]:
    """Move the simulation forward exactly one sprint, starting a new one if none exists."""
    state = simulation_state(connection)
    if not state:
        return simulate_sprints(connection, 1)
    if state["sprints_advanced"] >= MAX_SIMULATED_SPRINTS:
        raise ValueError(f"The simulation is already at the {MAX_SIMULATED_SPRINTS}-sprint limit.")
    from .scenarios import IMPORT_SCENARIO, scenario_key

    if scenario_key(connection) == IMPORT_SCENARIO:
        raise ValueError("Imported data is a snapshot. Import new exports or apply a scenario to keep simulating.")
    return simulate_sprints(
        connection, state["sprints_advanced"] + 1, date.fromisoformat(state["started_on"])
    )


def inject_event(connection: Connection, kind: str) -> dict[str, Any]:
    """Apply an event to the current sprint and recompute the simulated records."""
    from .scenarios import IMPORT_SCENARIO, add_event, scenario_key

    state = simulation_state(connection)
    if not state:
        raise ValueError("Simulate a sprint before injecting an event.")
    if scenario_key(connection) == IMPORT_SCENARIO:
        raise ValueError("Events apply to scripted scenarios. Switch back to a scenario to inject one.")
    add_event(connection, kind, state["sprints_advanced"])
    return simulate_sprints(
        connection, state["sprints_advanced"], date.fromisoformat(state["started_on"])
    )


def trigger_preemption(connection: Connection, active: bool = True) -> dict[str, Any]:
    """Inject (or clear) the synthetic P0 preemption; the simulation moves to Sprint 3 so the collision is live."""
    from .scenarios import IMPORT_SCENARIO, scenario_key

    state = simulation_state(connection)
    if not active:
        set_preemption(connection, False)
        return simulate_sprints(connection, state["sprints_advanced"], date.fromisoformat(state["started_on"])) \
            if state else {}
    if state and scenario_key(connection) == IMPORT_SCENARIO:
        raise ValueError("Preemption applies to scripted scenarios. Switch back to a scenario to simulate it.")
    set_preemption(connection, True)
    sprint = max(PREEMPT_SPRINTS[0], state["sprints_advanced"] if state else 0)
    return simulate_sprints(
        connection, sprint, date.fromisoformat(state["started_on"]) if state else None
    )


def reset_simulation(connection: Connection) -> None:
    from .scenarios import clear_events

    with connection:
        connection.execute("DELETE FROM dt_simulation_state")
        connection.execute("DELETE FROM dt_simulation_settings")
    clear_events(connection)
    _purge_simulated_rows(connection)
    seed_demo(connection)


def clear_demo_data(connection: Connection) -> int:
    """Delete every application data row while preserving the schema (local tables and portable app tables)."""
    return connection.local.clear_all()


def build_report(connection: Connection, as_of: date | None = None) -> dict[str, Any]:
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
        "SELECT * FROM dt_delivery_initiatives WHERE id = ?", (INITIATIVE_ID,)
    ).fetchone()
    if initiative is None:
        raise RuntimeError("Demo data is missing; run the seed-demo command first.")

    plan = connection.execute(
        """
        SELECT capacity_fte, allocated_fte FROM dt_planview_projects
        WHERE initiative_id = ?
        """,
        (INITIATIVE_ID,),
    ).fetchone()
    scope = connection.execute(
        """
        SELECT COUNT(*) AS item_count, COALESCE(SUM(story_points), 0) AS story_points
        FROM dt_ado_work_items AS wi
        JOIN dt_ado_projects AS p ON p.external_id = wi.project_external_id
        WHERE p.initiative_id = ? AND wi.is_unplanned = 1 AND wi.sprint_external_id = ?
        """,
        (INITIATIVE_ID, sprint_id),
    ).fetchone()
    support = connection.execute(
        """
        SELECT COALESCE(SUM(hours), 0) AS hours
        FROM dt_ukg_time_entries
        WHERE initiative_id = ? AND work_type = 'production_support'
          AND work_date BETWEEN ? AND ?
        """,
        (INITIATIVE_ID, (report_date - timedelta(days=6)).isoformat(), report_date.isoformat()),
    ).fetchone()
    if simulation:
        pto = connection.execute(
            """
            SELECT COALESCE(SUM(hours), 0) AS hours
            FROM dt_ukg_pto_requests
            WHERE person_id = 'lead-engineer' AND status = 'approved'
              AND start_date <= ? AND end_date >= ?
            """,
            (report_date.isoformat(), sprint_start.isoformat()),
        ).fetchone()
        changes = connection.execute(
            """
            SELECT external_id, short_description, opened_at
            FROM dt_servicenow_change_requests
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
            FROM dt_ukg_pto_requests
            WHERE person_id = 'lead-engineer' AND status = 'approved'
            """,
        ).fetchone()
        changes = connection.execute(
            """
            SELECT external_id, short_description, opened_at
            FROM dt_servicenow_change_requests
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
        FROM dt_servicenow_incidents
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
        "portfolio_preempted_hours": 0,
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
    from .intake import latest_baseline

    baseline = latest_baseline(connection)
    if baseline:
        # TRACK measures against the commitment, so the approved FTE replaces the seeded Planview allocation.
        signals["allocated_fte"] = baseline["allocated_fte"]
    data_origin: dict[str, Any] = {
        "mode": "synthetic_default", "label": "Synthetic demo data",
        "detail": "Seeded records for five simulated systems.",
    }
    if simulation:
        from .scenarios import IMPORT_SCENARIO, SCENARIOS, list_events, scenario_key

        key = scenario_key(connection)
        if key == IMPORT_SCENARIO:
            from .importer import load_import

            imported = load_import(connection)
            if imported:
                signals.update(imported["signals"])
                data_origin = {
                    "mode": "imported", "label": "Imported exports",
                    "detail": ", ".join(item["name"] for item in imported["files"] if item.get("accepted"))
                              + f" (as of {imported['as_of']}). Signals without a file count as zero.",
                }
        else:
            events = list_events(connection)
            data_origin = {
                "mode": "synthetic_scenario", "label": f"Synthetic scenario: {SCENARIOS[key].label}",
                "detail": SCENARIOS[key].description, "scenario": key, "events": events,
            }
    portfolio_active = bool(simulation) and preemption_active(connection)
    if portfolio_active:
        # A forecast prices the whole announced preemption, not only the sprints already lost to it.
        signals["portfolio_preempted_hours"] = planned_loss_hours()
    collision_detected = (
        signals["lead_production_support_hours_per_week"] >= 8
        and signals["unplanned_story_points"] >= 32
        and signals["oldest_blocking_change_age_days"] >= 7
    )
    source_modes = [
        dict(row)
        for row in connection.execute(
            "SELECT source_key, display_name, integration_mode, writeback_enabled "
            "FROM dt_source_systems ORDER BY source_key"
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
        "data_origin": data_origin,
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
        "portfolio": portfolio_view(
            portfolio_active, simulation["sprints_advanced"] if simulation else None,
            signals["allocated_fte"], initiative["name"],
        ),
        "resource_allocations": [
            dict(row)
            for row in connection.execute(
                """
                SELECT role_name, ROUND(SUM(allocated_fte), 1) AS allocated_fte
                FROM dt_planview_resource_assignments
                WHERE initiative_id = ?
                GROUP BY role_name
                ORDER BY role_name
                """,
                (INITIATIVE_ID,),
            )
        ],
        **({"simulation": simulation} if simulation else {}),
    }


def build_workflow_report(
    connection: Connection, as_of: date | None = None
) -> dict[str, Any]:
    """Build the deterministic report and run it through the Agent Framework graph."""
    from .workflow import run_delivery_workflow_sync

    return run_delivery_workflow_sync(build_report(connection, as_of))


def _managed_host() -> bool:
    """Databricks Apps and Azure App Service route traffic to the app from outside its own loopback."""
    from .integrations import azure_app_service

    return bool(os.environ.get("DATABRICKS_APP_PORT")) or azure_app_service()


def _data_script(parser: argparse.ArgumentParser, name: str) -> Any:
    """Import a maintenance script from the repository's `scripts/`; deployments ship only `src/`, not scripts."""
    import importlib

    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError as error:
        if error.name not in ("scripts", f"scripts.{name}"):
            raise
        parser.error(f"scripts/{name}.py is not available here; run this command from a clone of the repository.")


def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _latest_recovery_scenario(connection: Connection) -> dict[str, Any] | None:
    from .storage import RecoveryScenarioRepository

    scenario = RecoveryScenarioRepository(connection.app_database()).latest()
    if scenario is None:
        return None
    updates = scenario.pop("updates")
    scenario["target_updates"] = [
        {
            "source_key": update["source_key"],
            "system_name": SOURCE_SYSTEMS[update["source_key"]][0],
            "payload": update["payload"],
        }
        for update in updates
    ]
    from .recovery import CONFIDENCE_METHOD

    scenario["confidence_method"] = (
        f"{CONFIDENCE_METHOD} When an AI prediction was applied, its on-time figure is stored instead."
    )
    scenario["status"] = "simulated_updates_persisted"
    scenario["writeback_performed"] = False
    from .approvals import with_approvers

    return with_approvers(connection, "intervene", [scenario])[0]


def save_recovery_scenario(
    connection: Connection, scenario: dict[str, Any], approver: dict[str, str] | None = None
) -> dict[str, Any]:
    """Persist a recovery what-if and its simulated per-system update records, recording who applied it."""
    from .approvals import record_approval
    from .storage import RecoveryScenarioRepository

    saved = {key: scenario[key] for key in (
        "initiative_id", "recommended_option", "allocated_fte", "scope_defer_pct", "target_date",
        "incremental_cost_k", "confidence_pct",
    )}
    scenario_id = RecoveryScenarioRepository(connection.app_database()).save(saved, scenario["target_updates"])
    record_approval(connection, "intervene", scenario_id, approver, saved)
    return _latest_recovery_scenario(connection) or {}


def record_track_snapshot(connection: Connection, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep this sprint's TRACK outlook (idempotent per sprint) and return the whole trend."""
    from .storage import TrackSnapshotRepository

    repository = TrackSnapshotRepository(connection.app_database())
    outlook = report["workflow"]["stages"]["track"]
    signals = report["signals"]
    repository.record({
        "initiative_id": report["initiative"]["id"], "sprint_number": outlook["sprint_number"],
        "as_of": report["as_of"], "on_time_pct": outlook["on_time_pct"],
        "projected_slip_days": outlook["projected_slip_days"],
        "unplanned_story_points": signals["unplanned_story_points"],
        "support_hours_per_week": signals["lead_production_support_hours_per_week"],
    })
    return repository.trend(report["initiative"]["id"])


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name)
    return None if value is None else value.strip().lower() in ("1", "true", "yes", "on")


DESTRUCTIVE_ROUTES = ("/api/reset-all",)


def serve_dashboard(
    database: str | Path | None, host: str, port: int, dialect: str | None = None
) -> None:
    """Serve the dashboard and API; refuse unauthenticated exposure beyond loopback."""
    loopback = _is_loopback(host)
    if not loopback and not _env_flag("DELIVERYTWIN_TRUSTED_AUTH"):
        raise SystemExit(
            f"Refusing to bind {host!r}: the API has no built-in authentication. "
            "Bind 127.0.0.1, or set DELIVERYTWIN_TRUSTED_AUTH=1 when an authenticating "
            "gateway (Entra ID, App Service, Databricks Apps) fronts this server."
        )
    configured_demo = _env_flag("DELIVERYTWIN_DEMO_MODE")
    demo_mode = loopback if configured_demo is None else configured_demo
    # Simulation only rewrites synthetic records, so it can be allowed without the data-wiping demo mode.
    simulation_enabled = demo_mode or bool(_env_flag("DELIVERYTWIN_SIMULATION"))
    chosen_dialect = resolve_dialect(dialect)
    database = database if database is not None else default_database_path(chosen_dialect)

    def connect(path: str | Path) -> Connection:
        """Per-request connection: ``with connect(...) as connection`` scopes and closes it."""
        return open_connection(path, chosen_dialect, session=True)

    with connect(database) as connection:
        initialize_database(connection, register_sources=False)
        if chosen_dialect == "databricks" and not connection.execute(
            "SELECT EXISTS(SELECT 1 FROM dt_delivery_initiatives) "
            "AND EXISTS(SELECT 1 FROM dt_historical_projects)"
        ).fetchone()[0]:
            seed_demo(connection)

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
            if route in EXTRA_GET_ROUTES:
                try:
                    if route in ("/api/ai/status", "/api/samples"):
                        self.send_json(200, handle_get(route, None))
                    else:
                        with connect(database) as connection:
                            self.send_json(200, handle_get(route, connection))
                except (OSError, *DatabaseError, RuntimeError, ValueError) as error:
                    self.send_json(500, {"error": f"Unable to load {route}: {error}"})
                return
            if route == "/api/impact":
                from .analytics import build_impact_report

                try:
                    with connect(database) as connection:
                        self.send_json(200, build_impact_report(connection))
                except (OSError, *DatabaseError, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to build impact report: {error}"})
                return
            if route == "/api/intake/baseline":
                from .intake import latest_baseline

                try:
                    with connect(database) as connection:
                        self.send_json(200, {"baseline": latest_baseline(connection)})
                except (OSError, *DatabaseError) as error:
                    self.send_json(500, {"error": f"Unable to load baseline: {error}"})
                return
            if route == "/healthz":
                try:
                    with connect(database) as connection:
                        connection.execute("SELECT 1")
                except (OSError, *DatabaseError) as error:
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
                        approved = baseline_approved(connection)
                        if state is None or not approved:
                            payload = {"simulated": False, "baseline_required": not approved}
                        else:
                            payload = {**build_workflow_report(connection), "simulated": True,
                                       "simulation": state}
                        body = json.dumps(payload).encode("utf-8")
                except (OSError, *DatabaseError, RuntimeError) as error:
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
            from .analytics import record_intake_activity
            from .intake import (
                analyze_intake, approve_baseline, estimate_intake, evaluate_intake,
                forecast_from_history, simulate_rom,
            )

            try:
                with connect(database) as connection:
                    # Intake needs reference history and capacity; load them on demand after a clear.
                    if not connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM dt_historical_projects) "
                        "AND EXISTS(SELECT 1 FROM dt_historical_project_lessons) "
                        "AND EXISTS(SELECT 1 FROM dt_planview_projects)"
                    ).fetchone()[0]:
                        seed_demo(connection)
                    if route == "/api/intake/analyze":
                        result = analyze_intake(connection, payload.get("text"), payload.get("documents"))
                    elif route == "/api/intake/forecast":
                        result = forecast_from_history(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"), questions=payload.get("questions"),
                        )
                    elif route == "/api/intake/estimate":
                        result = estimate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"), questions=payload.get("questions"),
                        )
                    elif route == "/api/intake/what-if":
                        evaluation = evaluate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"), questions=payload.get("questions"),
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
                    elif route == "/api/intake/challenge":
                        from .commit_ai import challenge_commitment

                        evaluation = evaluate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"), adjustments=payload.get("adjustments"),
                            questions=payload.get("questions"), basis=payload.get("basis"),
                        )
                        result = challenge_commitment(connection, evaluation)
                    elif route == "/api/intake/ask":
                        from .commit_ai import ask_whatif

                        result = ask_whatif(
                            connection, payload.get("question"), date.today(),
                            lambda adjustments: evaluate_intake(
                                connection, payload.get("text"), payload.get("answers"),
                                documents=payload.get("documents"), adjustments=adjustments,
                                questions=payload.get("questions"), basis=payload.get("basis"),
                            ),
                            payload.get("adjustments") or {},
                        )
                    elif route == "/api/intake/evaluate":
                        result = evaluate_intake(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                            adjustments=payload.get("adjustments"),
                            questions=payload.get("questions"), basis=payload.get("basis"),
                        )
                    else:
                        result = approve_baseline(
                            connection, payload.get("text"), payload.get("answers"),
                            documents=payload.get("documents"),
                            adjustments=payload.get("adjustments"),
                            questions=payload.get("questions"), basis=payload.get("basis"),
                            approver=approver_from_headers(self.headers),
                        )
                    event = {
                        "/api/intake/analyze": "started",
                        "/api/intake/evaluate": "rom_ready",
                        "/api/intake/forecast": "rom_ready",
                        "/api/intake/estimate": "rom_ready",
                        "/api/intake/approve": "approved",
                    }.get(route)
                    if event:
                        record_intake_activity(
                            connection, payload.get("intake_session_id"), event
                        )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except (OSError, *DatabaseError, RuntimeError) as error:
                self.send_json(500, {"error": f"Unable to process intake request: {error}"})
                return
            self.send_json(200, result)

        def reject(self, status: int, message: str) -> None:
            # Consume the small request body first so the client sees the response, not a reset.
            try:
                remaining = min(int(self.headers.get("Content-Length") or 0), MAX_CHAT_REQUEST_BYTES)
            except ValueError:
                remaining = 0
            if remaining:
                self.rfile.read(remaining)
            self.send_json(status, {"error": message})

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            if route not in (
                "/api/chat", "/api/simulate", "/api/reset-all",
                *INTAKE_POST_ROUTES, *ACT_POST_ROUTES, *TRACK_POST_ROUTES, *COMMIT_POST_ROUTES,
                *EXTRA_POST_ROUTES,
            ):
                self.send_error(404, "Not found")
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.reject(403, "Cross-origin requests are not allowed.")
                return
            if route in DESTRUCTIVE_ROUTES and not demo_mode:
                self.reject(403, "Demo data operations are disabled (DELIVERYTWIN_DEMO_MODE).")
                return
            if route in SIMULATION_ROUTES and not simulation_enabled:
                self.reject(403, "Simulation is disabled (set DELIVERYTWIN_SIMULATION=1 or DELIVERYTWIN_DEMO_MODE=1).")
                return
            payload = self.read_json_object(
                MAX_INTAKE_REQUEST_BYTES
                if route in INTAKE_POST_ROUTES or route == "/api/import/signals" else MAX_CHAT_REQUEST_BYTES
            )
            if payload is None:
                return
            if route == "/api/simulate" and payload.get("reset") is True and not demo_mode:
                self.send_json(403, {"error": "Resetting the simulation is disabled (DELIVERYTWIN_DEMO_MODE)."})
                return
            if route in EXTRA_POST_ROUTES:
                try:
                    with connect(database) as connection:
                        result = handle_post(route, payload, connection)
                except BaselineRequired:
                    self.send_json(409, {"error": BASELINE_REQUIRED})
                    return
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, *DatabaseError, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to process the request: {error}"})
                    return
                self.send_json(200, result)
                return
            if route == "/api/reset-all":
                if payload.get("confirm") is not True:
                    self.send_json(400, {"error": "Explicit confirmation is required to clear demo data."})
                    return
                try:
                    with connect(database) as connection:
                        deleted = clear_demo_data(connection)
                except (OSError, *DatabaseError) as error:
                    self.send_json(500, {"error": f"Unable to clear demo data: {error}"})
                    return
                except Exception as error:
                    logger.exception("Unable to clear demo data")
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
                        elif not baseline_approved(connection):
                            raise BaselineRequiredError(BASELINE_REQUIRED)
                        elif payload.get("preempt") is not None:
                            if not isinstance(payload["preempt"], bool):
                                raise ValueError("preempt must be true or false.")
                            result = {"simulated": True, **trigger_preemption(connection, payload["preempt"])}
                        elif payload.get("next_sprint") is True:
                            result = {"simulated": True, **advance_one_sprint(connection)}
                        else:
                            scenario = payload.get("scenario")
                            if scenario is not None and not isinstance(scenario, str):
                                raise ValueError("The scenario must be a string.")
                            current = simulation_state(connection) if scenario else None
                            result = {"simulated": True, **simulate_sprints(
                                connection, sprints,
                                date.fromisoformat(current["started_on"]) if current else None,
                                scenario=scenario,
                            )}
                except BaselineRequiredError as error:
                    self.send_json(409, {"error": str(error)})
                    return
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, *DatabaseError, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to run simulation: {error}"})
                    return
                self.send_json(200, result)
                return
            if route in COMMIT_POST_ROUTES:
                from .backlog import latest_backlog

                try:
                    with connect(database) as connection:
                        result = latest_backlog(connection)
                        if result is None:
                            raise BaselineRequiredError(
                                "Approve a baseline in COMMIT before generating the Azure DevOps backlog."
                            )
                except BaselineRequiredError as error:
                    self.send_json(409, {"error": str(error)})
                    return
                except (OSError, *DatabaseError, RuntimeError, ValueError) as error:
                    self.send_json(500, {"error": f"Unable to build the backlog: {error}"})
                    return
                self.send_json(200, result)
                return
            if route in TRACK_POST_ROUTES:
                from .intake import latest_baseline
                from .track_ai import build_brief

                try:
                    with connect(database) as connection:
                        if not baseline_approved(connection):
                            raise BaselineRequiredError(BASELINE_REQUIRED)
                        if simulation_state(connection) is None:
                            raise ValueError("Simulate a sprint before requesting a TRACK briefing.")
                        report = build_workflow_report(connection)
                        result = build_brief(report, latest_baseline(connection))
                        result["trend"] = record_track_snapshot(connection, report)
                except BaselineRequiredError as error:
                    self.send_json(409, {"error": str(error)})
                    return
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, *DatabaseError, RuntimeError) as error:
                    self.send_json(500, {"error": f"Unable to build the TRACK briefing: {error}"})
                    return
                self.send_json(200, result)
                return
            if route in ACT_POST_ROUTES:
                from .recovery import analyze_intervention, predict_scenario, tune_scenario

                try:
                    with connect(database) as connection:
                        if not baseline_approved(connection):
                            raise BaselineRequiredError(BASELINE_REQUIRED)
                        if simulation_state(connection) is None:
                            raise ValueError("Simulate a sprint in TRACK before creating an intervention scenario.")
                        report = build_workflow_report(connection)
                        if report["portfolio"]["active"]:
                            from .backlog import latest_backlog

                            report["portfolio"]["backlog"] = latest_backlog(connection)
                        late = [
                            dict(row) for row in connection.execute(
                                "SELECT project_name, planned_effort_hours, actual_effort_hours, "
                                "planned_duration_weeks, actual_duration_weeks, outcome, lessons_learned "
                                "FROM dt_historical_projects WHERE actual_duration_weeks > planned_duration_weeks "
                                "ORDER BY (actual_duration_weeks - planned_duration_weeks) * 1.0 / planned_duration_weeks DESC "
                                "LIMIT 6"
                            )
                        ]
                        if route == "/api/act/analyze":
                            result = analyze_intervention(report, late)
                        elif route == "/api/act/tune":
                            result = tune_scenario(report, payload, late)
                        else:
                            result = predict_scenario(report, payload, late)
                            if route == "/api/act/apply":
                                result = save_recovery_scenario(
                                    connection, result, approver_from_headers(self.headers),
                                )
                except BaselineRequiredError as error:
                    self.send_json(409, {"error": str(error)})
                    return
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                    return
                except (OSError, *DatabaseError, RuntimeError) as error:
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
                            {"error": "No delivery data yet. Approve a baseline in COMMIT, then use Simulate next sprint in TRACK."},
                        )
                        return
                    report = build_workflow_report(connection)
            except (OSError, *DatabaseError, RuntimeError) as error:
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
    from .integrations import describe
    from .llm import ai_mode

    switches = describe()
    print(f"Data: {chosen_dialect} · AI: {ai_mode()} · Databricks: {switches['databricks']} · "
          f"Foundry: {switches['foundry']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Delivery Twin dashboard.", flush=True)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic Delivery Twin source-system demo")
    parser.add_argument(
        "--db", default=None,
        help="DuckDB database file (default: deliverytwin.duckdb; DELIVERYTWIN_DB_PATH overrides)",
    )
    engine = argparse.ArgumentParser(add_help=False)
    engine.add_argument(
        "--dialect", choices=("duckdb", "databricks"), default=None,
        help="Database engine (default: DELIVERYTWIN_DB_DIALECT, else databricks when DATABRICKS_HOST is set, "
             "else duckdb). Databricks stores sample, simulation and application tables in Unity Catalog.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", parents=[engine], help="Create the schema and register simulated sources")
    subparsers.add_parser("seed-demo", parents=[engine], help="Load synthetic records from all five source systems")
    subparsers.add_parser("report", parents=[engine], help="Print the cross-system delivery signal report")
    subparsers.add_parser("demo", parents=[engine], help="Initialize, seed synthetic data, and print a report")
    simulate_parser = subparsers.add_parser(
        "simulate", parents=[engine], help="Load synthetic test data and move the date forward by N sprints"
    )
    simulate_parser.add_argument("--sprints", type=int, default=5, help="Sprints to advance (default: 5)")
    simulate_parser.add_argument("--reset", action="store_true", help="Clear the simulation so the other tabs go blank")
    serve_parser = subparsers.add_parser("serve", parents=[engine], help="Run the local web dashboard")
    serve_parser.add_argument(
        "--host", default=os.environ.get("DELIVERYTWIN_HOST") or ("0.0.0.0" if _managed_host() else "127.0.0.1"),
        help="Dashboard bind address (default: 127.0.0.1; 0.0.0.0 inside Databricks Apps or Azure App Service)",
    )
    serve_parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("DELIVERYTWIN_PORT") or os.environ.get("PORT") or 8765),
        help="Dashboard port (default: DATABRICKS_APP_PORT, DELIVERYTWIN_PORT, PORT (Azure App Service), else 8765)",
    )
    data_parser = subparsers.add_parser(
        "generate-data", help="Regenerate the deterministic delivery_intel-shaped CSV dataset"
    )
    data_parser.add_argument("--out", default=None, help="Output directory (default: packaged dataset)")
    ddl_parser = subparsers.add_parser("ddl", help="Print table DDL for a SQL dialect")
    ddl_parser.add_argument("--dialect", choices=("databricks", "duckdb"), default="databricks")
    ddl_parser.add_argument("--catalog", default="dpm_dev")
    ddl_parser.add_argument("--schema", default="mlops")
    subparsers.add_parser(
        "databricks-smoke",
        help="Verify the Databricks adapter in a throwaway schema (needs workspace access)",
    )
    args = parser.parse_args(argv)

    if args.command == "databricks-smoke":
        try:
            return _data_script(parser, "databricks_smoke").run_databricks_smoke()
        except RuntimeError as error:
            parser.error(str(error))
    if args.command == "generate-data":
        from .storage import DATASET_DIR

        _write_json(_data_script(parser, "generate_data").write_dataset(args.out or DATASET_DIR))
        return 0
    if args.command == "ddl":
        from .storage import render_ddl

        qualified = args.dialect == "databricks"
        print(render_ddl(args.dialect, args.catalog if qualified else None, args.schema if qualified else None), end="")
        return 0
    try:
        dialect = resolve_dialect(getattr(args, "dialect", None))
    except ValueError as error:
        parser.error(str(error))
    database = args.db or default_database_path(dialect)
    if args.command == "serve":
        serve_dashboard(database, args.host, args.port, dialect)
        return 0

    with open_connection(database, dialect, session=True) as connection:
        if args.command == "init":
            initialize_database(connection)
            _write_json({"database": database, "dialect": dialect, "initialized": True})
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
