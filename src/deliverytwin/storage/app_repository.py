"""Repositories for application-written data, backed by any ``Database`` dialect.

They replace per-dialect SQL (autoincrement ids, ``ON CONFLICT DO NOTHING``, in-place
``UPDATE``) with portable reads and idempotent upserts, so the same code runs on
DuckDB and Delta tables.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .database import Database
from .schema import ALL_TABLES

INTAKE_EVENTS = ("started", "rom_ready", "approved")
APPROVAL_STAGES = ("commit", "intervene")


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
    TABLE = "dt_commitment_baselines"

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

    def history(self) -> list[dict[str, Any]]:
        """Every approved baseline, oldest first."""
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "ORDER BY approved_at ASC, baseline_id ASC"
        )
        return [self._summary(row) for row in rows]

    def latest_scope(self) -> dict[str, Any] | None:
        """The latest approved baseline plus the request and fingerprint it was approved for."""
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "ORDER BY approved_at DESC, baseline_id DESC LIMIT 1"
        )
        if not rows:
            return None
        return {
            **self._summary(rows[0]),
            "request_text": rows[0]["request_text"],
            "fingerprint": json.loads(rows[0]["fingerprint_json"] or "{}"),
        }

    def get(self, baseline_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "WHERE baseline_id = ?",
            [baseline_id],
        )
        return self._summary(rows[0]) if rows else None

    def clear(self) -> None:
        self.db.clear_table(self.TABLE)

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

    TABLE = "dt_intake_sessions"

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

    def clear(self) -> None:
        self.db.clear_table(self.TABLE)

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


class RecoveryScenarioRepository:
    """INTERVENE scenarios saved after a what-if preview, with their simulated per-system update records."""

    TABLE = "dt_recovery_scenarios"
    UPDATES = "dt_recovery_target_updates"

    def __init__(self, db: Database) -> None:
        self.db = db
        db.create_tables(names=(self.TABLE, self.UPDATES))

    def save(self, scenario: dict[str, Any], updates: list[dict[str, Any]]) -> int:
        """Store a scenario and its updates in one unit; returns the new scenario id."""
        saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.batch():
            current = self.db.query(f"SELECT MAX(scenario_id) AS latest FROM {self.db.table(self.TABLE)}")
            scenario_id = int(current[0]["latest"] or 0) + 1
            self.db.upsert(self.TABLE, [{**scenario, "scenario_id": scenario_id, "saved_at": saved_at}])
            self.db.upsert(self.UPDATES, [
                {"scenario_id": scenario_id, "source_key": update["source_key"],
                 "payload_json": json.dumps(update["payload"])}
                for update in updates
            ])
        return scenario_id

    def latest(self) -> dict[str, Any] | None:
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "ORDER BY scenario_id DESC LIMIT 1"
        )
        if not rows:
            return None
        row = rows[0]
        updates = self.db.query(
            f"SELECT source_key, payload_json FROM {self.db.table(self.UPDATES)} "
            "WHERE scenario_id = ? ORDER BY source_key", [row["scenario_id"]],
        )
        return {
            "id": row["scenario_id"], "initiative_id": row["initiative_id"],
            "recommended_option": row["recommended_option"], "allocated_fte": row["allocated_fte"],
            "scope_defer_pct": row["scope_defer_pct"], "target_date": _text(row["target_date"]),
            "incremental_cost_k": row["incremental_cost_k"], "confidence_pct": row["confidence_pct"],
            "saved_at": _text(row["saved_at"]),
            "updates": [{"source_key": u["source_key"], "payload": json.loads(u["payload_json"])} for u in updates],
        }

    def clear(self) -> None:
        for name in (self.UPDATES, self.TABLE):
            self.db.clear_table(name)


class ApprovalRepository:
    """Who approved each COMMIT baseline and applied each INTERVENE plan, with the figures they approved."""

    TABLE = "dt_approvals"

    def __init__(self, db: Database) -> None:
        self.db = db
        db.create_tables(names=(self.TABLE,))

    def record(self, stage: str, record_id: object, approver: dict[str, str], summary: dict[str, Any]) -> dict[str, Any]:
        if stage not in APPROVAL_STAGES:
            raise ValueError(f"Unsupported approval stage: {stage}.")
        row = {
            "approval_id": str(uuid.uuid4()), "stage": stage, "record_id": str(record_id),
            "approver": approver["name"], "approver_source": approver["source"],
            "summary_json": json.dumps(summary, default=str),
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        self.db.upsert(self.TABLE, [row])
        return self._entry(row)

    def by_record(self, stage: str) -> dict[str, dict[str, Any]]:
        """The approval of every record of a stage, keyed by record id (the latest one if recorded twice)."""
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "WHERE stage = ? ORDER BY approved_at ASC, approval_id ASC", [stage],
        )
        return {row["record_id"]: self._entry(row) for row in rows}

    def history(self) -> list[dict[str, Any]]:
        """Every approval, newest first."""
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "ORDER BY approved_at DESC, approval_id DESC"
        )
        return [self._entry(row) for row in rows]

    def clear(self) -> None:
        self.db.clear_table(self.TABLE)

    @staticmethod
    def _entry(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["approval_id"], "stage": row["stage"], "record_id": row["record_id"],
            "approver": row["approver"], "approver_source": row["approver_source"],
            "summary": json.loads(row["summary_json"] or "{}"), "approved_at": _text(row["approved_at"]),
        }


class TrackSnapshotRepository:
    """One TRACK outlook per initiative and sprint, so the trend survives restarts."""

    TABLE = "dt_track_snapshots"

    def __init__(self, db: Database) -> None:
        self.db = db
        db.create_tables(names=(self.TABLE,))

    def record(self, snapshot: dict[str, Any]) -> str:
        """Idempotent per sprint: recording the same sprint again replaces its outlook."""
        snapshot_id = f"{snapshot['initiative_id']}-sprint-{int(snapshot['sprint_number']):03d}"
        captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.upsert(self.TABLE, [{**snapshot, "snapshot_id": snapshot_id, "captured_at": captured_at}])
        return snapshot_id

    def trend(self, initiative_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            f"SELECT {', '.join(_columns(self.TABLE))} FROM {self.db.table(self.TABLE)} "
            "WHERE initiative_id = ? ORDER BY sprint_number ASC", [initiative_id],
        )
        return [
            {**row, "as_of": _text(row["as_of"]), "captured_at": _text(row["captured_at"])} for row in rows
        ]

    def clear(self) -> None:
        self.db.clear_table(self.TABLE)
