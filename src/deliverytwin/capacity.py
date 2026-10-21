"""Role × Phase capacity bottlenecks for COMMIT, with an HR privacy guardrail.

The aggregate FTE check (P80 effort at 40 h per FTE-week) is unchanged. This adds a deterministic view of
*where* the plan is tight: the P80 effort is spread across four phases and four role families using the
delivery fingerprint, and compared with the planned team's capacity in each phase.

HR privacy guardrail: Oracle HCM and UKG data enter only as aggregates. Worker names and ids never leave this
module, and any role group with fewer than MIN_GROUP_SIZE workers is suppressed instead of reported.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .storage.connection import Connection

MIN_GROUP_SIZE = 3
HOURS_PER_FTE_WEEK = 40
TIGHT_UTILIZATION = 0.85
BOTTLENECK_UTILIZATION = 1.0
MAX_UNAVAILABLE_SHARE = 0.5

PHASES = ("Discovery", "Build", "Test", "Cutover")
ROLES = {
    "data": "Data Engineering",
    "integration": "Integration / Backend",
    "qa": "QA",
    "security": "Security & Privacy",
}
# Share of the P80 effort that falls in each phase, by complexity, and of the schedule each phase occupies.
EFFORT_BY_PHASE = {
    "low": (0.10, 0.58, 0.18, 0.14),
    "medium": (0.10, 0.55, 0.20, 0.15),
    "high": (0.12, 0.50, 0.22, 0.16),
}
TIME_BY_PHASE = (0.15, 0.50, 0.20, 0.15)
# Base share of each phase's work by role family.
ROLE_SHARE = {
    "Discovery": {"data": 0.30, "integration": 0.35, "qa": 0.15, "security": 0.20},
    "Build": {"data": 0.35, "integration": 0.45, "qa": 0.10, "security": 0.10},
    "Test": {"data": 0.15, "integration": 0.20, "qa": 0.55, "security": 0.10},
    "Cutover": {"data": 0.25, "integration": 0.35, "qa": 0.20, "security": 0.20},
}
GUARDRAIL_MESSAGE = (
    "Privacy Guardrail Active: Oracle HCM & UKG capacity aggregated at role level only "
    "(minimum N >= 3 anonymity threshold; worker-level PII isolated)."
)


def role_family(name: str) -> str:
    lowered = name.lower()
    if re.search(r"privacy|security|governance", lowered):
        return "security"
    if re.search(r"\bqa\b|test|quality", lowered):
        return "qa"
    if "data" in lowered:
        return "data"
    return "integration"


def _weights(fingerprint: dict[str, Any]) -> dict[str, float]:
    """How much the fingerprint leans on each role family (multipliers on the base shares)."""
    roles = set(fingerprint.get("roles") or [])
    dependencies = " ".join(fingerprint.get("dependencies") or [])
    systems = len(fingerprint.get("systems") or [])
    return {
        "data": 1.3 if "data-engineer" in roles else 0.8,
        "integration": 1 + 0.05 * min(systems, 8),
        "qa": 1.0 + (0.2 if fingerprint.get("complexity") == "high" else 0.0),
        "security": 1.8 if "privacy-engineer" in roles or re.search(r"privacy|consent", dependencies) else 0.6,
    }


def hr_guardrail(connection: Connection, target: date, today: date, weeks: float) -> dict[str, Any]:
    """Role-level HR aggregates with small groups suppressed; returns counts and one team-wide factor only."""
    groups: dict[str, int] = {}
    for row in connection.execute(
        "SELECT job_title, COUNT(*) AS workers FROM dt_oracle_workers WHERE worker_status = 'active' GROUP BY job_title"
    ):
        family = role_family(row["job_title"])
        groups[family] = groups.get(family, 0) + int(row["workers"])
    team_size = sum(groups.values())
    suppressed_team = team_size < MIN_GROUP_SIZE
    factor = 1.0
    time_away_hours = None
    if not suppressed_team and weeks > 0:
        total = connection.execute(
            "SELECT COALESCE(SUM(hours), 0) FROM dt_ukg_pto_requests WHERE status = 'approved' "
            "AND end_date >= ? AND start_date <= ?", (today.isoformat(), target.isoformat()),
        ).fetchone()[0]
        time_away_hours = round(float(total))
        factor = 1 - min(MAX_UNAVAILABLE_SHARE, float(total) / (team_size * weeks * HOURS_PER_FTE_WEEK))
    return {
        "active": True, "min_group_size": MIN_GROUP_SIZE, "message": GUARDRAIL_MESSAGE,
        "team_size": team_size if not suppressed_team else None,
        "team_aggregate_suppressed": suppressed_team,
        "role_groups": [
            {"role": ROLES[family], "workers": count if count >= MIN_GROUP_SIZE else None,
             "suppressed": count < MIN_GROUP_SIZE}
            for family, count in sorted(groups.items())
        ],
        "suppressed_groups": sum(1 for count in groups.values() if count < MIN_GROUP_SIZE),
        "approved_time_away_hours_team": time_away_hours,
        "availability_factor": round(factor, 3),
    }


def role_phase_breakdown(
    connection: Connection, fingerprint: dict[str, Any], commitment: dict[str, Any], as_of: date | None = None
) -> dict[str, Any]:
    today = as_of or date.today()
    weeks = float(commitment["weeks_to_target"])
    target = date.fromisoformat(commitment["target_date"])
    guardrail = hr_guardrail(connection, target, today, weeks)
    availability = guardrail["availability_factor"]

    planned: dict[str, float] = {family: 0.0 for family in ROLES}
    for allocation in commitment.get("resource_allocations") or []:
        planned[role_family(allocation["role_name"])] += float(allocation["allocated_fte"])
    planned_total = sum(planned.values())
    scale = float(commitment["allocated_fte"]) / planned_total if planned_total else 0.0
    fte = {family: planned[family] * scale for family in ROLES}

    effort_shares = EFFORT_BY_PHASE.get(fingerprint.get("complexity"), EFFORT_BY_PHASE["medium"])
    weights = _weights(fingerprint)
    p80 = float(commitment["p80_hours"])
    rows, bottlenecks = [], []
    for family, label in ROLES.items():
        cells = []
        for index, phase in enumerate(PHASES):
            shares = {key: ROLE_SHARE[phase][key] * weights[key] for key in ROLES}
            share = shares[family] / sum(shares.values())
            required = p80 * effort_shares[index] * share
            capacity = fte[family] * weeks * TIME_BY_PHASE[index] * HOURS_PER_FTE_WEEK * availability
            utilization = required / capacity if capacity > 0 else None
            status = (
                "unstaffed" if capacity <= 0
                else "bottleneck" if utilization > BOTTLENECK_UTILIZATION
                else "tight" if utilization > TIGHT_UTILIZATION else "ok"
            )
            cells.append({
                "phase": phase, "required_hours": round(required), "capacity_hours": round(capacity),
                "utilization_pct": round(utilization * 100) if utilization is not None else None, "status": status,
            })
            if status in ("bottleneck", "unstaffed"):
                bottlenecks.append({
                    "role": label, "phase": phase, "required_hours": round(required),
                    "capacity_hours": round(capacity), "gap_hours": round(required - capacity),
                    "utilization_pct": round(utilization * 100) if utilization is not None else None,
                    "status": status,
                })
        rows.append({"key": family, "label": label, "planned_fte": round(fte[family], 1), "cells": cells})
    bottlenecks.sort(key=lambda item: (-item["gap_hours"], item["role"], item["phase"]))
    return {
        "phases": list(PHASES), "roles": rows, "bottlenecks": bottlenecks[:4],
        "bottleneck_count": len(bottlenecks), "privacy_guardrail": guardrail,
        "method": (
            "P80 effort is spread across Discovery, Build, Test and Cutover using the delivery fingerprint "
            "(complexity, systems, roles, dependencies) and compared with the planned team's hours in each phase. "
            "Utilization above 100% is a bottleneck, a role with no planned FTE is unstaffed; the aggregate FTE check is unchanged."
        ),
    }
