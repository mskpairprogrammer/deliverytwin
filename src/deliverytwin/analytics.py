from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Any

from .storage.connection import Connection, Row


def seed_synthetic_intake_activity(connection: Connection) -> None:
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
    from .storage import IntakeSessionRepository

    IntakeSessionRepository(connection.app_database()).replace_all([
        {"session_id": sid, "started_at": started, "first_rom_at": first_rom, "approved_at": approved}
        for sid, started, first_rom, approved in rows
    ])


def record_intake_activity(
    connection: Connection,
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

    from .storage import IntakeSessionRepository

    IntakeSessionRepository(connection.app_database()).record(
        normalized_id, event, occurred_at or datetime.now(timezone.utc)
    )


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _similarity(left: Row, right: Row) -> float:
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


def _backtest(connection: Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT project_id, project_name, technologies, integrations, team_size,
               planned_effort_hours, actual_effort_hours
        FROM dt_historical_projects
        WHERE planned_effort_hours > 0 AND actual_effort_hours > 0
        ORDER BY project_id
        """
    ).fetchall()
    points = []
    plan_errors = []
    analog_errors = []
    plan_signed: list[float] = []
    analog_signed: list[float] = []
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
        plan_signed.append((target["planned_effort_hours"] - actual) / actual * 100)
        analog_signed.append((analog_estimate - actual) / actual * 100)
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
            "original_plan_error_sd_pts": round(pstdev(plan_signed), 1) if len(plan_signed) > 1 else None,
            "analog_error_sd_pts": round(pstdev(analog_signed), 1) if len(analog_signed) > 1 else None,
        },
        "sample_count": len(points),
        "points": points,
    }


def _calibration(connection: Connection) -> dict[str, Any]:
    """Do the P50 and P80 bands from similar projects cover real outcomes as often as they should?"""
    rows = connection.execute(
        """
        SELECT project_id, technologies, integrations, team_size, planned_effort_hours, actual_effort_hours,
               planned_duration_weeks, actual_duration_weeks
        FROM dt_historical_projects
        WHERE planned_effort_hours > 0 AND actual_effort_hours > 0
        ORDER BY project_id
        """
    ).fetchall()
    below_p50 = below_p80 = 0
    signed_errors: list[float] = []
    plan_errors: list[float] = []
    for target in rows:
        peers = sorted(
            (row for row in rows if row["project_id"] != target["project_id"]),
            key=lambda peer: (-_similarity(target, peer), peer["project_id"]),
        )[:5]
        if len(peers) < 3:
            continue
        values = sorted(peer["actual_effort_hours"] for peer in peers)
        p50 = median(values)
        position = 0.8 * (len(values) - 1)
        low = int(position)
        p80 = values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)
        actual = target["actual_effort_hours"]
        below_p50 += actual <= p50
        below_p80 += actual <= p80
        signed_errors.append((p50 - actual) / actual * 100)
        plan_errors.append((target["planned_effort_hours"] - actual) / actual * 100)
    count = len(signed_errors)
    if not count:
        return {"sample_count": 0}
    p80_cover = round(below_p80 / count * 100)
    on_time = [row for row in rows if row["actual_duration_weeks"] <= row["planned_duration_weeks"]]
    overruns = [
        (row["actual_duration_weeks"] - row["planned_duration_weeks"]) / row["planned_duration_weeks"] * 100
        for row in rows if row["planned_duration_weeks"]
    ]
    if p80_cover >= 85:
        verdict = "The P80 band is conservative: it covers more outcomes than its 80% target."
    elif p80_cover >= 75:
        verdict = "The P80 band is well calibrated against its 80% target."
    else:
        verdict = "The P80 band is optimistic: it covers fewer outcomes than its 80% target, so treat it with care."
    return {
        "method": (
            "Leave-one-out: each completed project is estimated from its five closest other projects. "
            "P50 is their median actual effort and P80 their 80th percentile; coverage is how often the real effort "
            "fell at or below each band."
        ),
        "sample_count": count,
        "p50_coverage_pct": round(below_p50 / count * 100), "p80_coverage_pct": p80_cover,
        "estimate_bias_pct": round(mean(signed_errors), 1), "original_plan_bias_pct": round(mean(plan_errors), 1),
        "on_time_rate_pct": round(len(on_time) / len(rows) * 100) if rows else None,
        "median_duration_overrun_pct": round(median(overruns), 1) if overruns else None,
        "verdict": verdict,
    }


def build_impact_report(
    connection: Connection,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build portfolio estimator metrics from completed history and instrumented UI sessions."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    from .storage import IntakeSessionRepository

    sessions = IntakeSessionRepository(connection.app_database()).sessions()
    elapsed_seconds = [
        max(0.0, (row["first_rom_at"] - row["started_at"]).total_seconds())
        for row in sessions
        if row["first_rom_at"] is not None
    ]
    cutoff = current_time - timedelta(days=7)
    recent_starts = sum(row["started_at"] >= cutoff for row in sessions)
    backtest = _backtest(connection)
    return {
        "as_of": current_time.isoformat(timespec="seconds"),
        "intake_metrics": {
            "median_seconds_to_first_rom": (
                round(median(elapsed_seconds), 1) if elapsed_seconds else None
            ),
            "rom_sample_count": len(elapsed_seconds),
            "intake_starts_last_7_days": recent_starts,
        },
        "backtest": backtest,
        "calibration": _calibration(connection),
        "roi": build_roi(connection, backtest),
    }


# Words that show a requester already named an external dependency in their own text.
DEPENDENCY_KEYWORDS = {
    "privacy-review": ("privacy", "pii", "gdpr", "personal data"),
    "consent-management": ("consent", "opt-in", "opt in", "opt-out"),
    "crm-mdm-integration": ("crm", "mdm", "master data"),
    "identity-provider": ("identity", "sso", "single sign", "okta", "entra"),
    "vendor-contract": ("vendor", "contract", "procurement"),
    "private-network": ("private network", "vpn", "private link", "firewall"),
}


def _dependency_set(value: str) -> set[str]:
    return {item for item in (value or "").split(",") if item}


def _flag_from_analogs(dependency_lists: list[set[str]]) -> set[str]:
    """A dependency is flagged when most of the closest analogs needed it."""
    counts: dict[str, int] = {}
    for dependencies in dependency_lists:
        for dependency in dependencies:
            counts[dependency] = counts.get(dependency, 0) + 1
    return {name for name, count in counts.items() if count * 2 > len(dependency_lists)}


def _dependency_recall(connection: Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT project_id, project_name, technologies, integrations, team_size, dependencies
        FROM dt_historical_projects ORDER BY project_id
        """
    ).fetchall()
    caught = actual = flagged_total = 0
    for target in rows:
        peers = sorted(
            (row for row in rows if row["project_id"] != target["project_id"]),
            key=lambda peer: (-_similarity(target, peer), peer["project_id"]),
        )[:3]
        flagged = _flag_from_analogs([_dependency_set(peer["dependencies"]) for peer in peers])
        gates = _dependency_set(target["dependencies"])
        caught += len(flagged & gates)
        actual += len(gates)
        flagged_total += len(flagged)
    return {
        "projects": len(rows), "dependencies": actual, "caught": caught,
        "recall_pct": round(caught / actual * 100) if actual else None,
        "precision_pct": round(caught / flagged_total * 100) if flagged_total else None,
    }


def build_roi(connection: Connection, backtest: dict[str, Any]) -> dict[str, Any]:
    """Two deterministic executive metrics; both are computed from history, never from the AI."""
    recall = _dependency_recall(connection)
    estimators = backtest["estimators"]
    plan_sd, analog_sd = estimators.get("original_plan_error_sd_pts"), estimators.get("analog_error_sd_pts")
    reduction = round((plan_sd - analog_sd) / plan_sd * 100) if plan_sd and analog_sd is not None else None
    return {
        "early_dependency_detection": {
            **recall,
            "headline": (
                f"{recall['recall_pct']}% of external dependencies flagged at SHAPE "
                f"({recall['caught']} of {recall['dependencies']})"
                if recall["recall_pct"] is not None else "Not enough history"
            ),
            "method": (
                "Leave-one-out: each completed project's approval-gate dependencies are predicted only from "
                "its three closest other projects (a dependency is flagged when most of them needed it). "
                "No requester input is used."
            ),
            "baseline_note": (
                "The history has no record of dependencies found mid-flight, so no mid-flight baseline is "
                "measured. Per session, SHAPE compares what it flags with what the request itself names."
            ),
        },
        "estimator_variance": {
            "original_plan_error_sd_pts": plan_sd, "analog_error_sd_pts": analog_sd,
            "spread_reduction_pct": reduction,
            "original_plan_mape_pct": estimators.get("original_plan_mape_pct"),
            "analog_mape_pct": estimators.get("analog_mape_pct"),
            "headline": (
                f"Estimate spread {'down' if reduction >= 0 else 'up'} {abs(reduction)}% vs original plans "
                f"({plan_sd} to {analog_sd} pts)" if reduction is not None else "Not enough history"
            ),
            "method": (
                "Standard deviation of the signed % error against actual effort, original plan versus the "
                "leave-one-out analog estimate. A smaller spread means more consistent estimates."
            ),
        },
    }


def session_dependency_detection(
    text: str, fingerprint: dict[str, Any], matches: list[dict[str, Any]], documents_text: str = ""
) -> dict[str, Any]:
    """For one request: dependencies SHAPE flags versus those the request text (and any uploads) already name."""
    analog_lists = [set(match["dimensions"]["dependencies"]["historical"]) for match in matches]
    flagged = sorted(set(fingerprint.get("dependencies") or []) | _flag_from_analogs(analog_lists))

    def named_in(value: str) -> list[str]:
        lowered = (value or "").casefold()
        return [name for name in flagged if any(word in lowered for word in DEPENDENCY_KEYWORDS.get(name, ()))]

    stated = named_in(text)
    in_documents = [name for name in named_in(documents_text) if name not in stated]
    return {
        "flagged": flagged, "stated_in_request": stated, "named_only_in_documents": in_documents,
        "flagged_count": len(flagged), "stated_count": len(stated), "surfaced_by_shape": len(flagged) - len(stated),
        "headline": (
            f"{len(flagged)} external dependencies flagged at SHAPE; the request text named {len(stated)}"
            if flagged else "No external dependencies flagged"
        ),
    }
