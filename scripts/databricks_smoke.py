"""Smoke test for a data backend: create tables, load the dataset, read it back, write app rows.

``python scripts/databricks_smoke.py`` (or ``python -m deliverytwin databricks-smoke``) runs it against a
Databricks SQL warehouse in a throwaway schema (dropped afterwards), so someone with workspace access can
verify the adapter in about a minute. The same checks run locally against DuckDB in the unit tests.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if not __package__:  # run as `python scripts/databricks_smoke.py`: make the package in src/ importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deliverytwin.storage.app_repository import BaselineRepository, IntakeSessionRepository
from deliverytwin.storage.database import Database, DatabricksDatabase, DatabricksSettings
from deliverytwin.storage.repository import HistoryRepository, load_dataset
from deliverytwin.storage.schema import TABLES

BASELINE = {
    "request_text": "smoke", "answers_json": "{}", "fingerprint_json": "{}", "p50_hours": 4650,
    "p80_hours": 5750, "integration_buffer_pct": 19, "allocated_fte": 7.0, "capacity_fte": 8.5,
    "target_date": "2027-03-31", "confidence_pct": 62.2,
}


def run_checks(db: Database) -> list[tuple[str, bool, str]]:
    """Run every check, continuing past failures; returns (name, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, action: Callable[[], Any], verify: Callable[[Any], bool] = lambda _: True) -> Any:
        try:
            value = action()
            ok = bool(verify(value))
            results.append((name, ok, "" if ok else f"unexpected result: {value!r}"))
            return value
        except Exception as error:  # report every failure instead of stopping at the first
            results.append((name, False, f"{type(error).__name__}: {error}"))
            return None

    counts = check("create tables and load dataset", lambda: load_dataset(db),
                   lambda value: value["dt_project_estimate_summary"] == 50)
    check("read row counts back", lambda: {
        name: db.query(f"SELECT COUNT(*) AS n FROM {db.table(name)}")[0]["n"] for name in TABLES
    }, lambda value: counts is not None and all(value[name] == counts[name] for name in TABLES))
    check("derive history for 50 projects", lambda: HistoryRepository(db).list_projects(),
          lambda projects: len(projects) == 50 and all(p["lessons"] for p in projects))
    check("upsert is idempotent", lambda: (load_dataset(db), db.query(
        f"SELECT COUNT(*) AS n FROM {db.table('dt_project_estimate_summary')}")[0]["n"]),
        lambda value: value[1] == 50)
    saved = check("save a baseline", lambda: BaselineRepository(db).save(BASELINE))
    check("read the latest baseline", lambda: BaselineRepository(db).latest(),
          lambda row: row is not None and row["id"] == saved and row["target_date"] == "2027-03-31")
    start = datetime.now(timezone.utc)
    session_id = str(uuid.uuid4())
    check("record intake events", lambda: (
        IntakeSessionRepository(db).record(session_id, "started", start),
        IntakeSessionRepository(db).record(session_id, "rom_ready", start)))
    check("read intake sessions", lambda: IntakeSessionRepository(db).sessions(),
          lambda rows: any(r["session_id"] == session_id and r["first_rom_at"] for r in rows))
    return results


def format_report(results: list[tuple[str, bool, str]]) -> str:
    lines = [f"{'PASS' if ok else 'FAIL'}  {name}{f' - {detail}' if detail else ''}" for name, ok, detail in results]
    failed = sum(not ok for _, ok, _ in results)
    lines.append(f"\n{len(results) - failed}/{len(results)} checks passed")
    return "\n".join(lines)


def run_databricks_smoke(settings: DatabricksSettings | None = None) -> int:
    """Run the checks in a temporary schema of the configured catalog, then drop it."""
    base = settings or DatabricksSettings.from_env()
    schema = f"dt_smoke_{uuid.uuid4().hex[:8]}"
    admin = DatabricksDatabase(base)
    qualified = f"{admin.catalog}.{schema}"
    print(f"Creating temporary schema {qualified}")
    admin.execute(f"CREATE SCHEMA IF NOT EXISTS {qualified}")
    try:
        scoped = DatabricksDatabase(
            DatabricksSettings(base.server_hostname, base.http_path, base.catalog, schema, base.access_token),
            admin.connection,
        )
        results = run_checks(scoped)
    finally:
        print(f"Dropping temporary schema {qualified}")
        admin.execute(f"DROP SCHEMA IF EXISTS {qualified} CASCADE")
        admin.close()
    print(format_report(results))
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(run_databricks_smoke())
    except RuntimeError as error:
        raise SystemExit(f"databricks-smoke: {error}") from error
