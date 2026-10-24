"""Scripted scenario packs and injectable events for the TRACK simulation.

There is no connection to any source system. Each sprint's cross-system signals come from a named,
fully deterministic scenario, optionally changed by events a presenter injects (an engineer leaves,
a dependency slips). The forecast formula in `workflow.py` is unchanged; only its inputs differ.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .storage.connection import Connection

DEFAULT_SCENARIO = "collision"
POINTS_PER_UNPLANNED_ITEM = 4
IMPORT_SCENARIO = "import"
ENGINEER_LEAVES_HOURS = 80


@dataclass(frozen=True)
class SprintSignals:
    unplanned_items: int
    support_hours: int
    p1_incidents: int
    change_age_days: int | None  # None: no change request is blocking the delivery
    pto_hours: int


def _collision(n: int) -> SprintSignals:
    return SprintSignals(4 * n, 4 * n, min((n + 1) // 2, 3), 2 + 6 * (n - 1), 40 if n == 3 else 0)


def _healthy(n: int) -> SprintSignals:
    return SprintSignals(1, 2, 0, None, 0)


def _dependency_slips(n: int) -> SprintSignals:
    return SprintSignals(n, 3, 0, 3 + 9 * (n - 1), 0)


def _incident_spike(n: int) -> SprintSignals:
    return SprintSignals(2 * n, 3 * n + (6 if n >= 3 else 0), min(n, 4), 2 + 2 * (n - 1), 0)


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    description: str
    plan: Callable[[int], SprintSignals]


SCENARIOS: dict[str, Scenario] = {
    scenario.key: scenario for scenario in (
        Scenario("collision", "Capacity collision",
                 "Production support, unplanned scope and a stuck change approval grow together.", _collision),
        Scenario("healthy", "Healthy delivery",
                 "Few surprises: little unplanned work, light support load and no blocked change.", _healthy),
        Scenario("dependency_slips", "Dependency slips",
                 "A change approval ages sprint after sprint while the rest stays calm.", _dependency_slips),
        Scenario("incident_spike", "Incident spike",
                 "Priority-1 incidents and support load climb; approvals move normally.", _incident_spike),
    )
}


@dataclass(frozen=True)
class EventKind:
    key: str
    label: str
    description: str


EVENTS: dict[str, EventKind] = {
    event.key: event for event in (
        EventKind("engineer_leaves", "Engineer leaves",
                  f"Modeled as {ENGINEER_LEAVES_HOURS} lost lead-engineer hours in the sprint."),
        EventKind("dependency_slip", "Dependency slips two weeks", "The blocking change approval ages by 14 days."),
        EventKind("p1_incident", "New P1 incident", "One more open priority-1 incident."),
        EventKind("scope_surge", "Scope surge", "Four more unplanned work items (16 story points)."),
        EventKind("support_spike", "Support spike", "Twelve more production-support hours per week."),
    )
}


def scenario_key(connection: Connection) -> str:
    row = connection.execute("SELECT value FROM dt_simulation_settings WHERE key = 'scenario'").fetchone()
    return row["value"] if row and (row["value"] in SCENARIOS or row["value"] == IMPORT_SCENARIO) else DEFAULT_SCENARIO


def set_scenario(connection: Connection, key: str) -> None:
    if key not in SCENARIOS and key != IMPORT_SCENARIO:
        raise ValueError(f"Unknown scenario. Choose one of: {', '.join(SCENARIOS)}.")
    with connection:
        connection.upsert_rows(
            "dt_simulation_settings", ("key", "value"), ("key",), [("scenario", key)], ("value",),
        )


def list_events(connection: Connection) -> list[dict[str, object]]:
    return [
        {"id": row["id"], "sprint_number": row["sprint_number"], "kind": row["kind"],
         "label": EVENTS[row["kind"]].label if row["kind"] in EVENTS else row["kind"]}
        for row in connection.execute("SELECT id, sprint_number, kind FROM dt_simulation_events ORDER BY id")
    ]


def add_event(connection: Connection, kind: str, sprint_number: int) -> None:
    if kind not in EVENTS:
        raise ValueError(f"Unknown event. Choose one of: {', '.join(EVENTS)}.")
    with connection:
        connection.execute(
            "INSERT INTO dt_simulation_events (sprint_number, kind) VALUES (?, ?)", (sprint_number, kind)
        )


def clear_events(connection: Connection) -> None:
    with connection:
        connection.execute("DELETE FROM dt_simulation_events")


def effective_signals(key: str, sprint_number: int, events: list[dict[str, object]]) -> SprintSignals:
    """The scenario's signals for a sprint plus every event injected in that sprint or an earlier one."""
    signals = SCENARIOS[key].plan(sprint_number)
    for event in events:
        if event["sprint_number"] > sprint_number:
            continue
        kind = event["kind"]
        if kind == "engineer_leaves":
            signals = replace(signals, pto_hours=signals.pto_hours + ENGINEER_LEAVES_HOURS)
        elif kind == "dependency_slip":
            signals = replace(signals, change_age_days=(signals.change_age_days or 0) + 14)
        elif kind == "p1_incident":
            signals = replace(signals, p1_incidents=signals.p1_incidents + 1)
        elif kind == "scope_surge":
            signals = replace(signals, unplanned_items=signals.unplanned_items + 4)
        elif kind == "support_spike":
            signals = replace(signals, support_hours=signals.support_hours + 12)
    return signals


def catalog() -> dict[str, object]:
    return {
        "scenarios": [{"key": s.key, "label": s.label, "description": s.description} for s in SCENARIOS.values()],
        "event_kinds": [{"key": e.key, "label": e.label, "description": e.description} for e in EVENTS.values()],
    }
