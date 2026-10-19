from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any


def seed_synthetic_intake_activity(connection: sqlite3.Connection) -> None:
    """Populate rolling demo activity so portfolio process metrics have usable samples."""
    today = datetime.now(timezone.utc).date()
    rom_minutes = (5, 7, 8, 12, 15, 18, 23, 32, 46, None, None, None)
    rows = []
    for index, duration in enumerate(rom_minutes):
        started = datetime(
            (today - timedelta(days=index % 7)).year,
            (today - timedelta(days=index % 7)).month,
            (today - timedelta(days=index % 7)).day,
            9 + index % 6,
            (index * 7) % 60,
            tzinfo=timezone.utc,
        )
        first_rom = started + timedelta(minutes=duration) if duration is not None else None
        approved = (
            first_rom + timedelta(minutes=8 + index % 9)
            if first_rom is not None and index < 6
            else None
        )
        rows.append(
            (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"deliverytwin-demo-intake-{index + 1}")),
                started.isoformat(timespec="seconds"),
                first_rom.isoformat(timespec="seconds") if first_rom else None,
                approved.isoformat(timespec="seconds") if approved else None,
            )
        )
    with connection:
        connection.executemany(
            """
            INSERT INTO intake_sessions (session_id, started_at, first_rom_at, approved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                started_at = excluded.started_at,
                first_rom_at = excluded.first_rom_at,
                approved_at = excluded.approved_at
            """,
            rows,
        )


def record_intake_activity(
    connection: sqlite3.Connection,
    session_id: object,
    event: str,
    occurred_at: datetime | None = None,
) -> None:
    """Record intake timing without storing the request text or uploaded content."""
    if session_id is None:
        return
    if not isinstance(session_id, str):
        raise ValueError("Intake session ID must be a UUID.")
    try:
        normalized_id = str(uuid.UUID(session_id))
    except ValueError as error:
        raise ValueError("Intake session ID must be a UUID.") from error
    if event not in {"started", "rom_ready", "approved"}:
        raise ValueError(f"Unsupported intake activity event: {event}.")

    stamp = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    with connection:
        connection.execute(
            """
            INSERT INTO intake_sessions (session_id, started_at)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (normalized_id, stamp),
        )
        if event == "rom_ready":
            connection.execute(
                """
                UPDATE intake_sessions SET first_rom_at = COALESCE(first_rom_at, ?)
                WHERE session_id = ?
                """,
                (stamp, normalized_id),
            )
        elif event == "approved":
            connection.execute(
                """
                UPDATE intake_sessions
                SET first_rom_at = COALESCE(first_rom_at, ?),
                    approved_at = COALESCE(approved_at, ?)
                WHERE session_id = ?
                """,
                (stamp, stamp, normalized_id),
            )


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _similarity(left: sqlite3.Row, right: sqlite3.Row) -> float:
    left_tech = {
        item.strip().casefold() for item in left["technologies"].split(",") if item.strip()
    }
    right_tech = {
        item.strip().casefold() for item in right["technologies"].split(",") if item.strip()
    }
    union = left_tech | right_tech
    technology_score = len(left_tech & right_tech) / len(union) if union else 1.0
    integration_score = 1 / (
        1 + abs(left["integrations"] - right["integrations"])
        / max(left["integrations"], right["integrations"], 1)
    )
    team_score = 1 / (
        1 + abs(left["team_size"] - right["team_size"])
        / max(left["team_size"], right["team_size"], 1)
    )
    return 0.6 * technology_score + 0.2 * integration_score + 0.2 * team_score


def _backtest(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT project_id, project_name, technologies, integrations, team_size,
               planned_effort_hours, actual_effort_hours
        FROM historical_projects
        WHERE planned_effort_hours > 0 AND actual_effort_hours > 0
        ORDER BY project_id
        """
    ).fetchall()
    points = []
    plan_errors = []
    analog_errors = []
    for target in rows:
        peers = [row for row in rows if row["project_id"] != target["project_id"]]
        if not peers:
            continue
        ranked = sorted(
            peers,
            key=lambda peer: (-_similarity(target, peer), peer["project_id"]),
        )[: min(3, len(peers))]
        analog_estimate = round(median(peer["actual_effort_hours"] for peer in ranked))
        actual = target["actual_effort_hours"]
        plan_errors.append(abs(target["planned_effort_hours"] - actual) / actual * 100)
        analog_errors.append(abs(analog_estimate - actual) / actual * 100)
        points.append(
            {
                "project_id": target["project_id"],
                "project_name": target["project_name"],
                "actual_hours": actual,
                "planned_hours": target["planned_effort_hours"],
                "analog_hours": analog_estimate,
                "peer_ids": [peer["project_id"] for peer in ranked],
            }
        )
    return {
        "method": (
            "Leave-one-out: each project is estimated using the median actual effort of its "
            "three closest other projects by technology, integration count, and team size."
        ),
        "estimators": {
            "original_plan_mape_pct": round(mean(plan_errors), 1) if plan_errors else None,
            "analog_mape_pct": round(mean(analog_errors), 1) if analog_errors else None,
        },
        "sample_count": len(points),
        "points": points,
    }


def build_impact_report(
    connection: sqlite3.Connection,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build portfolio estimator metrics from completed history and instrumented UI sessions."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sessions = connection.execute(
        """
        SELECT started_at, first_rom_at
        FROM intake_sessions
        WHERE first_rom_at IS NOT NULL
        """
    ).fetchall()
    elapsed_seconds = [
        max(
            0.0,
            (datetime.fromisoformat(row["first_rom_at"]) - datetime.fromisoformat(row["started_at"]))
            .total_seconds(),
        )
        for row in sessions
    ]
    cutoff = current_time - timedelta(days=7)
    recent_starts = sum(
        datetime.fromisoformat(row["started_at"]) >= cutoff
        for row in connection.execute("SELECT started_at FROM intake_sessions")
    )
    return {
        "as_of": current_time.isoformat(timespec="seconds"),
        "intake_metrics": {
            "median_seconds_to_first_rom": (
                round(median(elapsed_seconds), 1) if elapsed_seconds else None
            ),
            "rom_sample_count": len(elapsed_seconds),
            "intake_starts_last_7_days": recent_starts,
        },
        "backtest": _backtest(connection),
    }
