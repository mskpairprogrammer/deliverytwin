"""Repositories for application-written data, backed by any ``Database`` dialect.

They replace per-dialect SQL (autoincrement ids, ``ON CONFLICT DO NOTHING``, in-place
``UPDATE``) with portable reads and idempotent upserts, so the same code runs on
SQLite, DuckDB, and Delta tables.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .database import Database
from .schema import ALL_TABLES

INTAKE_EVENTS = ("started", "rom_ready", "approved")


def _text(value: Any) -> Any:
    """Normalise values returned by different backends (str, date, datetime) to text."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _columns(table: str) -> list[str]:
    return [column.name for column in ALL_TABLES[table].columns]


class BaselineRepository:
    TABLE = "commitment_baselines"

    def __init__(self, db: Database) -> None:
        self.db = db
        db.create_tables(names=(self.TABLE,))

    def save(self, baseline: dict[str, Any]) -> str:
        baseline_id = str(uuid.uuid4())
        approved_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self.db.upsert(self.TABLE, [{**baseline, "baseline_id": baseline_id, "approved_at": approved_at}])
        return baseline_id

    def latest(self) -> dict[str, Any] | None:
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "ORDER BY approved_at DESC, baseline_id DESC LIMIT 1"
        )
        return self._summary(rows[0]) if rows else None

    def get(self, baseline_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "WHERE baseline_id = ?",
            [baseline_id],
        )
        return self._summary(rows[0]) if rows else None

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["baseline_id"],
            "status": "approved",
            "approved_at": _text(row["approved_at"]),
            "p50_hours": row["p50_hours"],
            "p80_hours": row["p80_hours"],
            "integration_buffer_pct": row["integration_buffer_pct"],
            "allocated_fte": row["allocated_fte"],
            "capacity_fte": row["capacity_fte"],
            "target_date": _text(row["target_date"]),
            "confidence_pct": row["confidence_pct"],
        }


class IntakeSessionRepository:
    """Anonymous intake timing; never stores request text or document content."""

    TABLE = "intake_sessions"

    def __init__(self, db: Database) -> None:
        self.db = db
        db.create_tables(names=(self.TABLE,))

    def _select(self, where: str = "", params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)}{where}",
            list(params),
        )

    def record(self, session_id: str, event: str, occurred_at: datetime) -> None:
        if event not in INTAKE_EVENTS:
            raise ValueError(f"Unsupported intake activity event: {event}.")
        stamp = occurred_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = self._select(" WHERE session_id = ?", [session_id])
        row = dict(rows[0]) if rows else {
            "session_id": session_id, "started_at": stamp, "first_rom_at": None, "approved_at": None,
        }
        if event in ("rom_ready", "approved"):
            row["first_rom_at"] = row["first_rom_at"] or stamp
        if event == "approved":
            row["approved_at"] = row["approved_at"] or stamp
        self.db.upsert(self.TABLE, [row])

    def replace_all(self, rows: list[dict[str, Any]]) -> None:
        with self.db.batch():
            self.db.upsert(self.TABLE, rows)

    def sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": row["session_id"],
                "started_at": as_datetime(row["started_at"]),
                "first_rom_at": as_datetime(row["first_rom_at"]),
                "approved_at": as_datetime(row["approved_at"]),
            }
            for row in self._select()
        ]
