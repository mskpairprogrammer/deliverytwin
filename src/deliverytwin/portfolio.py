"""Portfolio-level Program Increment (PI) preemption: a P0 project takes shared FTE in Sprints 3-4.

Everything here is deterministic and synthetic. The P0 project and its two sibling projects are made-up
portfolio records (no source system is read); the capacity loss, the cost and the recovery math are plain
arithmetic over them, never a model's output.

Capacity: one FTE is 80 h per two-week sprint. The preemption removes PREEMPT_FTE from the current project
in each preempted sprint. Confidence follows the TRACK forecast formula: every preempted hour costs
POINTS_PER_PREEMPTED_HOUR points, and every hour a recovery plan defers to the next PI costs
POINTS_PER_DEFERRED_HOUR points.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from .data.connection import Connection

SETTING_KEY = "preemption"
PI_SPRINTS = 6
SPRINT_DAYS = 14
SPRINT_HOURS_PER_FTE = 80
PREEMPT_SPRINTS = (3, 4)
PREEMPT_FTE = 2.5
P0 = {"id": "P0-REG-2026", "name": "Urgent Regulatory Compliance", "tier": "P0"}
CURRENT_TIER = "P1"
SIBLINGS = (
    {"id": "PRJ-019", "name": "Billing Migration", "tier": "P2", "fte": 3.0, "floor_fte": 2.0},
    {"id": "PRJ-024", "name": "Claims Portal", "tier": "P3", "fte": 4.0, "floor_fte": 2.0},
)
DONOR = "PRJ-024"
BORROW_FTE = 2.0
POINTS_PER_PREEMPTED_HOUR = 0.12  # matches the TRACK forecast weight in workflow.FORECAST_PENALTIES
POINTS_PER_DEFERRED_HOUR = 0.02
HYBRID_DEFER_HOURS = 120  # the composed plan defers only a prudent share, then fills the rest with capacity
BACKFILL_PRODUCTIVITY = 0.8
FTE_LOT = 0.5
OPTION_ORDER = ("resequence", "cross_portfolio", "backfill", "hybrid_replan")


def preemption_active(connection: Connection) -> bool:
    row = connection.execute("SELECT value FROM simulation_settings WHERE key = ?", (SETTING_KEY,)).fetchone()
    return bool(row and row["value"] == P0["id"])


def set_preemption(connection: Connection, active: bool) -> None:
    with connection:
        connection.execute("DELETE FROM simulation_settings WHERE key = ?", (SETTING_KEY,))
        if active:
            connection.execute(
                "INSERT INTO simulation_settings (key, value) VALUES (?, ?)", (SETTING_KEY, P0["id"])
            )


def preempted_hours_so_far(sprints_advanced: int) -> int:
    """Capacity already taken from the current project by the preempted sprints that have happened."""
    return round(sum(PREEMPT_FTE * SPRINT_HOURS_PER_FTE for sprint in PREEMPT_SPRINTS if sprint <= sprints_advanced))


def planned_loss_hours() -> int:
    return round(PREEMPT_FTE * SPRINT_HOURS_PER_FTE * len(PREEMPT_SPRINTS))


def capacity_vector(allocated_fte: float, extra_fte_in_window: float = 0.0, preempted: bool = True) -> list[int]:
    """Hours the current project has in each PI sprint."""
    hours = []
    for sprint in range(1, PI_SPRINTS + 1):
        fte = allocated_fte
        if sprint in PREEMPT_SPRINTS:
            fte += (extra_fte_in_window - (PREEMPT_FTE if preempted else 0.0))
        hours.append(round(max(0.0, fte) * SPRINT_HOURS_PER_FTE))
    return hours


def portfolio_view(active: bool, sprints_advanced: int | None, allocated_fte: float, project_name: str) -> dict[str, Any]:
    """The Quarterly PI portfolio swimlane (Sprints 1-6): FTE per project per sprint, with the collision flagged."""
    if not active:
        return {"active": False}
    sprints = list(range(1, PI_SPRINTS + 1))
    current_cells, p0_cells = [], []
    for sprint in sprints:
        hit = sprint in PREEMPT_SPRINTS
        current_cells.append({
            "sprint": sprint, "planned_fte": allocated_fte,
            "effective_fte": round(allocated_fte - (PREEMPT_FTE if hit else 0.0), 1),
            "status": "collision" if hit else "ok",
        })
        p0_cells.append({
            "sprint": sprint, "planned_fte": PREEMPT_FTE if hit else 0.0,
            "effective_fte": PREEMPT_FTE if hit else 0.0, "status": "collision" if hit else "idle",
        })
    projects = [
        {"id": "CURRENT", "name": project_name, "tier": CURRENT_TIER, "role": "current", "cells": current_cells},
        {**P0, "role": "preempting", "cells": p0_cells},
    ]
    for sibling in SIBLINGS:
        projects.append({
            "id": sibling["id"], "name": sibling["name"], "tier": sibling["tier"], "role": "sibling",
            "cells": [
                {"sprint": sprint, "planned_fte": sibling["fte"], "effective_fte": sibling["fte"],
                 "status": "watch" if sprint in PREEMPT_SPRINTS else "ok"}
                for sprint in sprints
            ],
        })
    return {
        "active": True, "sprints": sprints, "current_sprint": sprints_advanced, "projects": projects,
        "preemption": {
            **P0, "fte": PREEMPT_FTE, "sprints": list(PREEMPT_SPRINTS),
            "hours_per_sprint": round(PREEMPT_FTE * SPRINT_HOURS_PER_FTE), "planned_loss_hours": planned_loss_hours(),
            "lost_hours_so_far": preempted_hours_so_far(sprints_advanced or 0),
        },
        "collision_sprints": list(PREEMPT_SPRINTS),
        "note": (
            "Synthetic portfolio records. The P0 project takes 2.5 FTE from the current project in Sprints 3-4; "
            "red marks the resource collision and amber the sibling projects exposed to a further reallocation."
        ),
    }


def _plan(kind: str, loss_hours: int) -> dict[str, Any]:
    """How much of the preempted capacity each lever covers, sized in the order re-sequence, borrow, backfill."""
    window_hours = SPRINT_HOURS_PER_FTE * len(PREEMPT_SPRINTS)  # hours one FTE gives over the window
    donor = next(item for item in SIBLINGS if item["id"] == DONOR)
    headroom = max(0.0, donor["fte"] - donor["floor_fte"])
    deferred = borrowed = backfill = 0.0
    remaining = float(loss_hours)
    if kind == "resequence":
        deferred = remaining  # a pure re-sequence may defer every preempted hour
        remaining = 0.0
    elif kind == "hybrid_replan":
        deferred = float(min(remaining, HYBRID_DEFER_HOURS))
        remaining -= deferred
    if kind == "cross_portfolio":
        borrowed = min(BORROW_FTE, headroom)
    elif kind == "hybrid_replan":
        borrowed = min(headroom, math.floor(remaining / window_hours / FTE_LOT) * FTE_LOT)
    elif kind == "backfill":
        backfill = PREEMPT_FTE
    remaining -= borrowed * window_hours
    if kind == "hybrid_replan" and remaining > 0:
        lot_hours = FTE_LOT * window_hours * BACKFILL_PRODUCTIVITY
        backfill = math.ceil(remaining / lot_hours) * FTE_LOT
    reclaimed_by_backfill = min(max(remaining, 0.0), backfill * window_hours * BACKFILL_PRODUCTIVITY) if kind == "hybrid_replan" \
        else backfill * window_hours * BACKFILL_PRODUCTIVITY
    reclaimed = deferred + borrowed * window_hours + reclaimed_by_backfill
    return {
        "deferred_hours": round(deferred), "borrowed_fte": borrowed, "backfill_fte": backfill,
        "reclaimed_hours": round(min(reclaimed, loss_hours)), "residual_hours": round(max(0.0, loss_hours - reclaimed)),
        "cost_usd": round(backfill * window_hours * _rate()),
    }


def _rate() -> float:
    from .recovery import blended_rate

    return blended_rate()


def option_confidence(base_without_preemption: float, plan: dict[str, Any]) -> int:
    value = (
        base_without_preemption - POINTS_PER_PREEMPTED_HOUR * plan["residual_hours"]
        - POINTS_PER_DEFERRED_HOUR * plan["deferred_hours"]
    )
    return round(max(5, min(95, value)))


def donor_impact(borrowed_fte: float) -> dict[str, Any] | None:
    """What lending FTE to the lower-priority project costs it: its lost hours and the sprints that adds."""
    if borrowed_fte <= 0:
        return None
    donor = next(item for item in SIBLINGS if item["id"] == DONOR)
    lost = borrowed_fte * SPRINT_HOURS_PER_FTE * len(PREEMPT_SPRINTS)
    sprints = lost / (donor["fte"] * SPRINT_HOURS_PER_FTE)
    return {
        "project_id": donor["id"], "project_name": donor["name"], "tier": donor["tier"],
        "borrowed_fte": borrowed_fte, "fte_after": donor["fte"] - borrowed_fte, "floor_fte": donor["floor_fte"],
        "lost_hours": round(lost), "slip_sprints": round(sprints, 1), "slip_weeks": round(sprints * 2, 1),
    }


TITLES = {
    "resequence": ("Re-sequence sprint backlog", "Shift non-critical Features out of Sprints 3-4 into Sprints 5-6 or the next PI."),
    "cross_portfolio": ("Cross-portfolio reallocation", "Borrow 2.0 FTE from the lower-priority PRJ-024 Claims Portal in Sprints 3-4."),
    "backfill": ("Backfill preempted FTE", "Surge contractor capacity in Sprints 3-4 to protect the whole PI plan."),
    "hybrid_replan": ("Composed re-plan", "Re-sequence first, borrow only the remaining gap from PRJ-024, then backfill whatever is left."),
}


def portfolio_options(report: dict[str, Any], base_without_preemption: float) -> list[dict[str, Any]]:
    """The four portfolio-aware recovery scenarios with their deterministic plans, cost and donor impact."""
    from .recovery import cost_impact

    loss = planned_loss_hours()
    signals, initiative = report["signals"], report["initiative"]
    target = initiative["target_end"]

    options = []
    for kind in OPTION_ORDER:
        plan = _plan(kind, loss)
        confidence = option_confidence(base_without_preemption, plan)
        donor = donor_impact(plan["borrowed_fte"])
        cost_k = plan["cost_usd"] / 1000
        impact = cost_impact(report, signals["allocated_fte"], date.fromisoformat(target), cost_k)
        trade_offs, risks = [], []
        if plan["deferred_hours"]:
            trade_offs.append(f"{plan['deferred_hours']} h of non-critical Feature work moves to Sprints 5-6 or the next PI.")
        if donor:
            trade_offs.append(
                f"{donor['project_id']} {donor['project_name']} [{donor['tier']}] drops to {donor['fte_after']:g} FTE "
                f"in Sprints 3-4: about +{donor['slip_weeks']:g} weeks of slip there."
            )
        if plan["backfill_fte"]:
            trade_offs.append(
                f"{plan['backfill_fte']:g} FTE of contractors for Sprints 3-4 at ${_rate():,.0f}/h "
                f"(assumed {int(BACKFILL_PRODUCTIVITY * 100)}% productive)."
            )
        if plan["residual_hours"]:
            risks.append(f"{plan['residual_hours']} h of the 400 h loss is still uncovered and will slip the plan.")
        options.append({
            "id": kind, "title": TITLES[kind][0], "summary": TITLES[kind][1], "portfolio_option": kind,
            "allocated_fte": signals["allocated_fte"], "target_date": target,
            "incremental_cost_k": cost_k, "cost_impact": impact,
            "predicted_on_time_pct": confidence, "model_check_pct": confidence,
            "levers": plan, "donor_impact": donor, "trade_offs": trade_offs, "risks": risks,
            "composed": kind == "hybrid_replan",
        })
    return options
