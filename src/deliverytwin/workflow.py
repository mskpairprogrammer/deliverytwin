from __future__ import annotations

import asyncio
import os
from typing import Any

from agent_framework import (
    Executor,
    InMemoryCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)


STAGES = ("shape", "commit", "track", "intervene")
WORKFLOW_NAME = "deliverytwin"

# (key, label, signal, confidence points lost per unit, unit, source system). The TRACK forecast
# and its variance explanation both read this table, so attribution always matches the forecast.
FORECAST_PENALTIES = (
    ("production_support", "Production support load", "lead_production_support_hours_per_week", 0.8, "h/week", "ukg_wfm"),
    ("unplanned_scope", "Unplanned scope", "unplanned_story_points", 0.2, "points", "azure_devops"),
    ("change_blocker", "Blocked change approval", "oldest_blocking_change_age_days", 0.6, "days open", "servicenow"),
    ("approved_pto", "Approved time off", "lead_approved_pto_hours", 0.1, "hours", "ukg_wfm"),
    ("p1_incidents", "Priority-1 incidents", "active_priority_1_incidents", 2, "open", "servicenow"),
)
SLIP_DAYS_PER_POINT = 0.45


def forecast_confidence(baseline_pct: float, signals: dict[str, Any]) -> float:
    """Unclamped on-time confidence: the baseline minus each signal's penalty."""
    value = baseline_pct
    for _key, _label, signal, weight, _unit, _source in FORECAST_PENALTIES:
        value -= signals[signal] * weight
    return value


def _stage_result(stage: str, report: dict[str, Any]) -> dict[str, Any]:
    initiative = report["initiative"]
    signals = report["signals"]
    simulation = report.get("simulation", {})
    sprint_number = simulation.get("sprints_advanced", 4)
    on_time_pct = round(max(
        5,
        min(95, forecast_confidence(initiative["initial_confidence_pct"], signals)),
    ))
    projected_slip_days = round(max(0, (100 - on_time_pct) * SLIP_DAYS_PER_POINT))

    if stage == "shape":
        return {
            "status": "proposal_context_loaded",
            "target_end": initiative["target_end"],
            "baseline_p50_hours": initiative["baseline_p50_hours"],
            "baseline_p80_hours": initiative["baseline_p80_hours"],
            "initial_confidence_pct": initiative["initial_confidence_pct"],
        }
    if stage == "commit":
        return {
            "status": "commitment_context_loaded",
            "target_end": initiative["target_end"],
            "baseline_p50_hours": initiative["baseline_p50_hours"],
            "baseline_p80_hours": initiative["baseline_p80_hours"],
            "initial_confidence_pct": initiative["initial_confidence_pct"],
        }
    if stage == "track":
        return {
            "status": "signals_evaluated",
            "collision_detected": report["collision_detected"],
            "calculated": True,
            "source": "simulated_source_records",
            "sprint_number": sprint_number,
            "on_time_pct": on_time_pct,
            "projected_slip_days": projected_slip_days,
            "risk_level": (
                "high" if on_time_pct <= 50
                else "elevated" if on_time_pct <= 70
                else "managed"
            ),
            "unplanned_story_points": signals["unplanned_story_points"],
            "production_support_hours_per_week": signals[
                "lead_production_support_hours_per_week"
            ],
            "oldest_blocking_change_age_days": signals[
                "oldest_blocking_change_age_days"
            ],
        }
    has_blocker = bool(signals["blocking_change_requests"])
    options = [
        {
            "id": "A",
            "title": "OPTION A · Contractors",
            "description": "Adds temporary capacity after onboarding.",
            "cost": "+$118K",
            "confidence_pct": min(95, on_time_pct + 8),
            "tradeoff": "One-sprint onboarding",
        },
        {
            "id": "B",
            "title": "OPTION B · Squad reallocation",
            "description": "Moves capacity from Product Analytics.",
            "cost": "$0",
            "confidence_pct": min(95, on_time_pct + 16),
            "tradeoff": "Analytics +4 weeks",
        },
        {
            "id": "C",
            "title": "OPTION C · Phase + unblock",
            "description": (
                "Defer historical profile enrichment, fast-track privacy review, and shield the lead engineer."
                if has_blocker
                else "Phase remaining scope and protect the critical delivery capacity."
            ),
            "cost": "$0",
            "confidence_pct": min(95, on_time_pct + (22 if has_blocker else 10)),
            "tradeoff": "Historical profile enrichment deferred",
        },
    ]
    recommended_option = (
        "C" if has_blocker and on_time_pct <= 50
        else "B" if on_time_pct <= 70
        else "A"
    )
    for option in options:
        option["recommended"] = option["id"] == recommended_option

    support_hours = signals["lead_production_support_hours_per_week"]
    unplanned_points = signals["unplanned_story_points"]
    blocker_age = signals["oldest_blocking_change_age_days"]
    incident_count = signals["active_priority_1_incidents"]
    pto_hours = signals["lead_approved_pto_hours"]
    if recommended_option == "A":
        actions = {
            "milestone": (
                f"Keep the Customer 360 delivery target of {initiative['target_end']}; Sprint "
                f"{sprint_number} is forecast at {on_time_pct}% on-time confidence."
            ),
            "azure_devops": (
                f"Triage {unplanned_points:g} unplanned points and add temporary "
                "capacity without dropping committed scope."
            ),
            "servicenow": (
                f"Confirm the customer identity-matching CAB decision date; the blocker is "
                f"{blocker_age} days old."
                if has_blocker else "No active customer profile change blocker; monitor new dependencies."
            ),
            "workforce": (
                f"Onboard contractors to cover {support_hours:g}h/week of production "
                "support and protect the lead engineer's allocation."
            ),
        }
    elif recommended_option == "B":
        actions = {
            "milestone": (
                f"Reforecast the Customer 360 profile target for Sprint {sprint_number}: "
                f"{projected_slip_days} days of projected slip."
            ),
            "azure_devops": (
                f"Reallocate squad capacity and triage {unplanned_points:g} "
                "unplanned points against committed scope."
            ),
            "servicenow": (
                f"Get a dated CAB decision for customer identity matching ({blocker_age} days open)."
                if has_blocker else "No active customer profile change blocker; monitor new dependencies."
            ),
            "workforce": (
                f"Move capacity from Product Analytics and cover {support_hours:g}h/week "
                "of production support outside the Customer 360 squad."
            ),
        }
    else:
        if unplanned_points >= 64:
            scope_action = (
                f"Freeze new source intake and defer historical enrichment; {unplanned_points:g} "
                "unplanned points are beyond the sprint's delivery capacity."
            )
        else:
            scope_action = (
                f"Defer historical enrichment and prioritize the {unplanned_points:g} "
                "unplanned points on the critical delivery path."
            )
        if blocker_age >= 14:
            blocker_action = (
                f"Escalate the customer identity-matching CAB blocker ({blocker_age} days) "
                "to its sponsor for a decision before the next sprint."
            )
        elif has_blocker:
            blocker_action = (
                f"Secure an owner and decision date for the customer identity-matching "
                f"CAB request ({blocker_age} days open)."
            )
        else:
            blocker_action = "No active customer profile change blocker; monitor new dependencies."
        if support_hours >= 16 or incident_count >= 3:
            workforce_action = (
                f"Start a dedicated P1 rotation for {incident_count} incidents and "
                f"remove {support_hours:g}h/week of support from the lead engineer."
            )
        else:
            workforce_action = (
                f"Shield the lead engineer from {support_hours:g}h/week of support "
                "and protect specialist time."
            )
        if pto_hours:
            workforce_action += f" Reserve {pto_hours:g}h for approved PTO."
        actions = {
            "milestone": (
                f"Rebaseline the Customer 360 delivery target for Sprint {sprint_number}: "
                f"{on_time_pct}% on-time confidence and {projected_slip_days} projected "
                "slip days."
            ),
            "azure_devops": scope_action,
            "servicenow": blocker_action,
            "workforce": workforce_action,
        }
    return {
        "status": "approval_required",
        "recommended_option": recommended_option,
        "options": options,
        "actions": actions,
        "writeback_performed": False,
    }


class DeliveryStageExecutor(Executor):
    def __init__(self, stage: str) -> None:
        super().__init__(id=f"{stage}-stage")
        self.stage = stage

    @handler
    async def process(
        self,
        state: dict[str, Any],
        ctx: WorkflowContext[dict[str, Any], dict[str, Any]],
    ) -> None:
        updated_state = {
            **state,
            "completed_stages": [*state["completed_stages"], self.stage],
            "stages": {
                **state["stages"],
                self.stage: _stage_result(self.stage, state["report"]),
            },
        }
        if self.stage == STAGES[-1]:
            await ctx.yield_output(updated_state)
        else:
            await ctx.send_message(updated_state)


async def _execute_workflow(
    report: dict[str, Any],
    checkpoint_storage: Any,
    storage_name: str,
) -> dict[str, Any]:
    executors = [DeliveryStageExecutor(stage) for stage in STAGES]
    builder = WorkflowBuilder(
        start_executor=executors[0],
        name=WORKFLOW_NAME,
        checkpoint_storage=checkpoint_storage,
        output_from=[executors[-1]],
    )
    for source, destination in zip(executors, executors[1:]):
        builder.add_edge(source, destination)
    workflow = builder.build()
    result = await workflow.run(
        {"report": report, "completed_stages": [], "stages": {}}
    )
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], dict):
        raise RuntimeError(
            f"Delivery Twin workflow returned an unexpected output: {outputs!r}"
        )

    state = outputs[0]
    return {
        **report,
        "workflow": {
            "framework": "Microsoft Agent Framework",
            "name": WORKFLOW_NAME,
            "checkpoint_storage": storage_name,
            "completed_stages": state["completed_stages"],
            "stages": state["stages"],
        },
    }


async def run_delivery_workflow(report: dict[str, Any]) -> dict[str, Any]:
    """Run the deterministic four-stage graph, using Cosmos checkpoints when configured."""
    endpoint = os.environ.get("AZURE_COSMOS_ENDPOINT")
    if not endpoint:
        return await _execute_workflow(
            report, InMemoryCheckpointStorage(), "in_memory"
        )

    from agent_framework_azure_cosmos import CosmosCheckpointStorage
    from azure.identity.aio import DefaultAzureCredential

    async with DefaultAzureCredential() as credential:
        async with CosmosCheckpointStorage(
            endpoint=endpoint,
            credential=credential,
            database_name=os.environ.get(
                "AZURE_COSMOS_DATABASE_NAME", "deliverytwin"
            ),
            container_name=os.environ.get(
                "AZURE_COSMOS_CONTAINER_NAME", "workflow-checkpoints"
            ),
        ) as checkpoint_storage:
            return await _execute_workflow(
                report, checkpoint_storage, "azure_cosmos"
            )


def run_delivery_workflow_sync(report: dict[str, Any]) -> dict[str, Any]:
    """Synchronous adapter for the CLI and the current standard-library HTTP server."""
    return asyncio.run(run_delivery_workflow(report))
