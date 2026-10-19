from __future__ import annotations

from datetime import date
import math
from typing import Any


MIN_TARGET_DAYS = 7
MAX_TARGET_DAYS = 1095
MAX_INCREMENTAL_COST_K = 100_000
SCOPE_LOAD_PER_POINT = 0.005
CONFIDENCE_SENSITIVITY = 3.0
SYSTEM_NAMES = {
    "planview": "Planview",
    "azure_devops": "Azure DevOps",
    "servicenow": "ServiceNow",
    "oracle_hcm": "Oracle HR / HCM",
    "ukg_wfm": "UKG Pro WFM",
}


def preview_recovery_scenario(report: dict[str, Any], payload: object) -> dict[str, Any]:
    """Validate recovery assumptions and prepare simulated target-system updates."""
    if not isinstance(payload, dict):
        raise ValueError("Recovery scenario must be an object.")
    signals = report["signals"]
    initiative = report["initiative"]
    intervention = report["workflow"]["stages"]["intervene"]

    fte = payload.get("allocated_fte", signals["allocated_fte"])
    if isinstance(fte, bool) or not isinstance(fte, (int, float)) or not math.isfinite(fte):
        raise ValueError("Allocated FTE must be a number.")
    if not 0.5 <= fte <= signals["capacity_fte"]:
        raise ValueError(f"Allocated FTE must be between 0.5 and the {signals['capacity_fte']:.1f} FTE available.")
    fte = round(float(fte), 1)

    scope_defer_pct = payload.get("scope_defer_pct", 0)
    if (
        isinstance(scope_defer_pct, bool)
        or not isinstance(scope_defer_pct, int)
        or not 0 <= scope_defer_pct <= 100
    ):
        raise ValueError("Scope deferred must be a whole-number percentage from 0% to 100%.")

    try:
        target_date = date.fromisoformat(str(payload.get("target_date", initiative["target_end"])))
        as_of = date.fromisoformat(report["as_of"])
        current_target = date.fromisoformat(initiative["target_end"])
    except (TypeError, ValueError) as error:
        raise ValueError("Target date must be a valid YYYY-MM-DD date.") from error
    target_days = (target_date - as_of).days
    if not MIN_TARGET_DAYS <= target_days <= MAX_TARGET_DAYS:
        raise ValueError(
            f"Target date must be between {MIN_TARGET_DAYS} days and {MAX_TARGET_DAYS} days "
            f"from {as_of.isoformat()}."
        )

    cost_k = payload.get("incremental_cost_k", 0)
    if isinstance(cost_k, bool) or not isinstance(cost_k, (int, float)) or not math.isfinite(cost_k):
        raise ValueError("Incremental cost must be a number in thousands of dollars.")
    if not 0 <= cost_k <= MAX_INCREMENTAL_COST_K:
        raise ValueError(f"Incremental cost must be between $0K and ${MAX_INCREMENTAL_COST_K:,}K.")
    cost_k = round(float(cost_k), 1)

    unplanned_points = float(signals["unplanned_story_points"])
    deferred_points = round(unplanned_points * scope_defer_pct / 100, 1)
    retained_points = round(unplanned_points - deferred_points, 1)
    date_delta_days = (target_date - current_target).days
    base_confidence = float(report["workflow"]["stages"]["track"]["on_time_pct"])
    base_days = max(1, (current_target - as_of).days)
    base_fte = max(0.5, float(signals["allocated_fte"]))
    workload = max(0.2, 1 - deferred_points * SCOPE_LOAD_PER_POINT)
    capacity_ratio = (fte / base_fte) * (target_days / base_days) / workload
    base_p = min(0.95, max(0.05, base_confidence / 100))
    log_odds = math.log(base_p / (1 - base_p)) + CONFIDENCE_SENSITIVITY * math.log(capacity_ratio)
    confidence = round(max(5, min(95, 100 / (1 + math.exp(-log_odds)))))

    target_updates = {
        "planview": {
            "allocated_fte": fte,
            "target_date": target_date.isoformat(),
            "incremental_cost_k": cost_k,
        },
        "azure_devops": {
            "unplanned_scope_points": unplanned_points,
            "deferred_scope_points": deferred_points,
            "remaining_unplanned_scope_points": retained_points,
        },
        "servicenow": {
            "change_request_action": intervention["actions"]["servicenow"],
        },
        "oracle_hcm": {
            "recovery_team_allocation_fte": fte,
        },
        "ukg_wfm": {
            "support_hours_per_week": signals["lead_production_support_hours_per_week"],
            "recovery_team_allocation_fte": fte,
        },
    }
    return {
        "status": "preview",
        "initiative_id": initiative["id"],
        "recommended_option": intervention["recommended_option"],
        "allocated_fte": fte,
        "scope_defer_pct": scope_defer_pct,
        "deferred_scope_points": deferred_points,
        "remaining_unplanned_scope_points": retained_points,
        "target_date": target_date.isoformat(),
        "incremental_cost_k": cost_k,
        "confidence_pct": confidence,
        "confidence_method": (
            "Capacity-vs-load model anchored to the current forecast: capacity ratio = "
            "(FTE ÷ current FTE) × (days to new date ÷ days to current date) ÷ remaining workload "
            "(each deferred scope point removes 0.5% of load). Confidence moves on the log-odds "
            "scale with sensitivity 3, bounded to 5–95%."
        ),
        "target_updates": [
            {
                "source_key": source_key,
                "system_name": SYSTEM_NAMES[source_key],
                "payload": update,
            }
            for source_key, update in target_updates.items()
        ],
        "writeback_performed": False,
    }
