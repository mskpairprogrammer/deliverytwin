"""Handlers for the demo-environment routes: scenarios, events, file import, baseline history, samples, pack.

None of these connect to a source system. They work on synthetic data, files a person uploads, and the
approved baselines already stored in the local database.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .storage.connection import Connection
from .storage.paths import SAMPLES_DIR

SAMPLE_DOCUMENTS = (
    ("sample_rom_customer_360.md", "rom"),
    ("sample_meeting_notes_customer_360.md", "meeting_notes"),
)

EXTRA_GET_ROUTES = ("/api/ai/status", "/api/simulate/catalog", "/api/baselines", "/api/samples")
EXTRA_POST_ROUTES = ("/api/simulate/event", "/api/import/signals", "/api/commit/pack")
SIMULATION_ROUTES = ("/api/simulate", "/api/simulate/event", "/api/import/signals")


class BaselineRequired(Exception):
    pass


def baseline_history(connection: Connection) -> dict[str, Any]:
    from .storage import BaselineRepository

    history = BaselineRepository(connection.app_database()).history()
    result = []
    previous = None
    for baseline in history:
        delta = None
        if previous:
            delta = {
                "p50_hours": baseline["p50_hours"] - previous["p50_hours"],
                "p80_hours": baseline["p80_hours"] - previous["p80_hours"],
                "allocated_fte": round(baseline["allocated_fte"] - previous["allocated_fte"], 2),
                "target_days": (
                    date.fromisoformat(baseline["target_date"][:10]) - date.fromisoformat(previous["target_date"][:10])
                ).days,
                "confidence_pct": round(baseline["confidence_pct"] - previous["confidence_pct"], 1),
            }
        result.append({**baseline, "delta_vs_previous": delta})
        previous = baseline
    return {"baselines": list(reversed(result))}


def simulation_catalog(connection: Connection) -> dict[str, Any]:
    from .app import simulation_state
    from .importer import load_import
    from .scenarios import IMPORT_SCENARIO, catalog, list_events, scenario_key

    key = scenario_key(connection)
    state = simulation_state(connection)
    imported = load_import(connection) if key == IMPORT_SCENARIO and state else None
    return {
        **catalog(), "scenario": key if state else None, "events": list_events(connection) if state else [],
        "simulation": state,
        "import": (
            {"as_of": imported["as_of"], "files": imported["files"], "not_provided": imported["not_provided"]}
            if imported else None
        ),
    }


def apply_import(connection: Connection, files: object) -> dict[str, Any]:
    from .app import SPRINT_DAYS, baseline_approved, simulate_sprints
    from .importer import parse_exports, save_import
    from .scenarios import IMPORT_SCENARIO, clear_events

    if not baseline_approved(connection):
        raise BaselineRequired()
    as_of = date.today()
    parsed = parse_exports(files, as_of)
    save_import(connection, parsed)
    clear_events(connection)
    simulate_sprints(connection, 1, as_of - timedelta(days=SPRINT_DAYS), scenario=IMPORT_SCENARIO)
    return {"imported": True, **parsed}


def _fill_dates(text: str, today: date) -> str:
    """Sample exports use {today-3}-style placeholders so their dates always fall inside the sprint window."""
    return re.sub(
        r"\{today([+-]\d+)?\}", lambda match: (today + timedelta(days=int(match.group(1) or 0))).isoformat(), text
    )


def load_samples() -> dict[str, Any]:
    documents = [
        {"name": name, "kind": kind, "text": (SAMPLES_DIR / name).read_text(encoding="utf-8")}
        for name, kind in SAMPLE_DOCUMENTS
    ]
    today = date.today()
    exports = [
        {"name": path.name, "text": _fill_dates(path.read_text(encoding="utf-8"), today)}
        for path in sorted((SAMPLES_DIR / "exports").glob("*.csv"))
    ]
    return {"documents": documents, "exports": exports}


def handle_get(route: str, connection: Connection | None) -> dict[str, Any]:
    if route == "/api/ai/status":
        from .llm import ai_status

        return ai_status()
    if route == "/api/samples":
        return load_samples()
    assert connection is not None
    if route == "/api/baselines":
        return baseline_history(connection)
    return simulation_catalog(connection)


def handle_post(route: str, payload: dict[str, Any], connection: Connection) -> dict[str, Any]:
    from .app import baseline_approved, inject_event

    if route == "/api/import/signals":
        return apply_import(connection, payload.get("files"))
    if not baseline_approved(connection):
        raise BaselineRequired()
    if route == "/api/simulate/event":
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise ValueError("An event kind is required.")
        return {"simulated": True, **inject_event(connection, kind)}
    from .pack import build_pack

    pack = build_pack(connection, payload.get("review"))
    if pack is None:
        raise BaselineRequired()
    return pack
