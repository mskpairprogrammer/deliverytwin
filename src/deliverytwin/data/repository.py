"""Portfolio repositories: load the dataset and read completed-project history."""
from __future__ import annotations

import csv
from pathlib import Path
from statistics import median
from typing import Any

from .database import Database
from .generator import DATASET_DIR
from .schema import TABLES


def read_csv_rows(table_name: str, directory: Path | str = DATASET_DIR) -> list[dict[str, str]]:
    with (Path(directory) / f"{table_name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_dataset(db: Database, directory: Path | str = DATASET_DIR) -> dict[str, int]:
    """Create the portfolio tables if needed and idempotently upsert every CSV row."""
    db.create_tables(include_app=False)
    with db.batch():
        return {name: db.upsert(name, read_csv_rows(name, directory)) for name in TABLES}


def dataset_loaded(db: Database) -> bool:
    try:
        rows = db.query(f"SELECT COUNT(*) AS n FROM {db.table('project_estimate_summary')}")
    except Exception:
        return False
    return bool(rows and rows[0]["n"])


def _select(db: Database, table_name: str) -> list[dict[str, Any]]:
    columns = ", ".join(column.name for column in TABLES[table_name].columns)
    return db.query(f"SELECT {columns} FROM {db.table(table_name)}")


def _split(value: str | None) -> list[str]:
    return [item for item in (value or "").split(",") if item]


class HistoryRepository:
    """Joins the portfolio tables in Python so the SQL stays portable across dialects."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list_projects(self) -> list[dict[str, Any]]:
        estimates = {row["project_id"]: row for row in _select(self.db, "project_estimate_summary")}
        intakes = {row["project_id"]: row for row in _select(self.db, "project_intakes")}
        roles: dict[str, list[dict[str, Any]]] = {}
        for row in _select(self.db, "project_role_estimates"):
            roles.setdefault(row["project_id"], []).append(row)
        lessons: dict[str, list[dict[str, Any]]] = {}
        for row in _select(self.db, "project_lessons"):
            lessons.setdefault(row["project_id"], []).append(row)

        projects = []
        for project_id in sorted(estimates):
            estimate, intake = estimates[project_id], intakes.get(project_id)
            if intake is None:
                continue
            role_rows = roles.get(project_id, [])
            systems = _split(intake["affected_systems"])
            contingency = float(estimate["contingency_percent"])
            factor = 1 + contingency / 100
            fte = sum(float(row["average_fte"]) for row in role_rows) or 1.0
            project_lessons = sorted(lessons.get(project_id, []), key=lambda row: row["category"])
            projects.append({
                "project_id": project_id,
                "project_name": estimate["project_name"],
                "description": (
                    f"Domain: {intake['line_of_business'].lower()}. "
                    f"Delivery model: {intake['delivery_pattern']}. "
                    f"{intake['problem_statement_and_desired_outcome']}"
                ),
                "technologies": ",".join(systems),
                "integrations": len(systems),
                "team_size": max(2, round(fte)),
                "planned_duration_weeks": int(estimate["duration_weeks"]),
                "actual_duration_weeks": int(estimate["actual_duration_weeks"]),
                "planned_effort_hours": int(estimate["p50_hours"]),
                "actual_effort_hours": int(estimate["actual_hours"]),
                "outcome": estimate["outcome"],
                "lessons_learned": " ".join(row["lesson"] for row in project_lessons),
                "structure": intake["request_category"],
                "complexity": intake["estimated_complexity"],
                "systems": ",".join(systems),
                "roles": ",".join(sorted(row["role"] for row in role_rows)),
                "dependencies": ",".join(_split(intake["known_business_dependencies"])),
                "delivery_pattern": intake["delivery_pattern"],
                "base_p50_hours": round(int(estimate["p50_hours"]) / factor, 1),
                "base_p80_hours": round(int(estimate["p80_hours"]) / factor, 1),
                "integration_buffer_pct": contingency,
                "on_time_confidence_pct": round(float(estimate["confidence_score"]) * 100),
                "lessons": project_lessons,
            })
        return projects

    def portfolio_summary(self) -> dict[str, Any]:
        rows = _select(self.db, "project_estimate_summary")
        if not rows:
            return {"projects": 0}
        overruns = [int(row["actual_hours"]) / int(row["p50_hours"]) for row in rows]
        return {
            "projects": len(rows),
            "median_effort_overrun": round(median(overruns), 3),
            "late_projects": sum(
                int(row["actual_duration_weeks"]) > int(row["duration_weeks"]) for row in rows
            ),
        }
