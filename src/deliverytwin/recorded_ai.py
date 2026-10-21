"""Recorded stand-in for the language model, so the whole product works with no key and no internet.

Enabled with DELIVERYTWIN_AI_MODE=recorded. Answers are deterministic and built from the same data the real
model would be given, but they are NOT produced by a model. The UI labels every such answer
"recorded-sample", and the app shows a banner while this mode is on.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from statistics import median
from typing import Any

MODEL_NAME = "recorded-sample"


class RecordedError(RuntimeError):
    """Raised for prompts the recorded mode has no answer for; callers fall back to their rules."""


def _questions() -> dict[str, Any]:
    return {"questions": [
        {"id": "cutover", "category": "Delivery", "question": "How will consumers move over?",
         "options": [
             {"value": "phased", "label": "Move consumers in phases",
              "effects": {"structure": "legacy-modernization", "delivery_pattern": "incremental-integration",
                          "systems": ["crm", "service-management"], "points": 0}},
             {"value": "big_bang", "label": "One coordinated cutover",
              "effects": {"delivery_pattern": "big-bang-cutover", "points": 1}}],
         "suggested": "phased", "rationale": "Similar completed projects phased the move to limit cutover risk.",
         "evidence_project_ids": ["HIST-001"]},
        {"id": "sync", "category": "Data", "question": "How fresh must profiles be?",
         "options": [
             {"value": "events", "label": "Near real time",
              "effects": {"systems": ["event-streaming", "commerce"], "roles": ["data-engineer"], "points": 2}},
             {"value": "batch", "label": "Scheduled batch",
              "effects": {"systems": ["batch-integration"], "points": 1}}],
         "suggested": "events", "rationale": "Customer-facing teams expect fresh data.", "evidence_project_ids": []},
        {"id": "privacy", "category": "Privacy", "question": "Which privacy controls apply?",
         "options": [
             {"value": "full", "label": "PII, consent, and retention controls",
              "effects": {"roles": ["privacy-engineer"], "dependencies": ["privacy-review", "consent-management"],
                          "points": 2}},
             {"value": "light", "label": "Standard access review", "effects": {"points": 0}}],
         "suggested": "full", "rationale": "Customer data is personal data.", "evidence_project_ids": []},
        {"id": "identity", "category": "Reuse", "question": "What can be reused?",
         "options": [
             {"value": "reuse", "label": "Existing match rules and profile APIs",
              "effects": {"systems": ["mdm", "identity-resolution", "profile-api"],
                          "dependencies": ["crm-mdm-integration"]}},
             {"value": "build", "label": "Build new identity services",
              "effects": {"roles": ["platform-engineer"], "points": 1}}],
         "suggested": "reuse", "rationale": "A master data hub already exists.", "evidence_project_ids": []},
    ]}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _forecast(payload: dict[str, Any]) -> dict[str, Any]:
    projects = payload["similar_historical_projects"]
    actuals = [float(p["actual_effort_hours"]) for p in projects]
    on_time = [p for p in projects if p["actual_duration_weeks"] <= p["planned_duration_weeks"]]
    return {
        "p50_effort_hours": median(actuals), "p80_effort_hours": _percentile(actuals, 0.8),
        "p90_effort_hours": max(max(actuals), _percentile(actuals, 0.9)),
        "on_time_probability": round(len(on_time) / len(projects), 2), "confidence": 0.6,
        "key_risks": [
            "Recorded sample: effort spread across similar projects is wide.",
            f"{projects[0]['project_name']} is the closest completed project.",
        ],
        "recommendations": ["Phase the delivery and clear dependencies early."],
        "historical_projects_used": [p["project_name"] for p in projects[:3]],
    }


def _challenge(payload: dict[str, Any]) -> dict[str, Any]:
    commitment = payload["commitment"]
    risks: list[dict[str, str]] = []
    if not commitment["feasible"]:
        risks.append({
            "title": "Capacity does not cover the P80 estimate", "severity": "high",
            "detail": f"P80 needs {commitment['required_fte_p80']} FTE; {commitment['allocated_fte']} are allocated.",
            "evidence": f"Required {commitment['required_fte_p80']} vs allocated {commitment['allocated_fte']} FTE",
        })
    elif commitment["required_fte_p80"] >= 0.9 * commitment["allocated_fte"]:
        risks.append({
            "title": "Thin capacity headroom", "severity": "medium",
            "detail": "Required FTE is close to allocated, so one absence breaks the date.",
            "evidence": f"Required {commitment['required_fte_p80']} vs allocated {commitment['allocated_fte']} FTE",
        })
    for project in payload.get("similar_completed_projects", []):
        planned, actual = project.get("planned_duration_weeks"), project.get("actual_duration_weeks")
        if planned and actual and actual > planned:
            risks.append({
                "title": "Similar projects ran late", "severity": "medium",
                "detail": f"{project['project_name']} took {actual} weeks against a plan of {planned}.",
                "evidence": f"{project['project_name']}: {actual} vs {planned} weeks",
            })
            break
    if not risks:
        risks.append({
            "title": "Confidence rests on history", "severity": "low",
            "detail": "The on-time figure comes from similar completed projects.",
            "evidence": f"{commitment['confidence_pct']:g}% on-time confidence",
        })
    dependencies = payload["fingerprint"].get("dependencies", [])
    conditions = [f"Confirm owners and dates for: {', '.join(dependencies)}."] if dependencies else []
    if not commitment["feasible"]:
        verdict = "not_ready"
    elif conditions or any(risk["severity"] != "low" for risk in risks):
        verdict = "ready_with_conditions"
    else:
        verdict = "ready"
    return {
        "verdict": verdict,
        "summary": "Recorded sample review built from the supplied figures and similar completed projects.",
        "risks": risks[:4], "conditions": conditions,
    }


def _ask(payload: dict[str, Any]) -> dict[str, Any]:
    question, current = payload["question"].lower(), payload["current_levers"]
    levers = {"answerable": True, "allocated_fte": current["allocated_fte"], "target_date": current["target_date"],
              "scope_change_pct": current["scope_change_pct"], "caveats": ["Recorded sample interpretation."]}
    scope = re.search(r"(\d{1,2})\s*%", question)
    if re.search(r"\b(lose|loses|lost|leave|leaves|quit)\b", question):
        levers["allocated_fte"] = max(0.5, current["allocated_fte"] - 1)
        levers["reading"] = "Reads as losing one engineer, so allocated FTE drops by 1."
    elif re.search(r"\b(delay|delayed|push|later|slip|month)\b", question):
        moved = date.fromisoformat(current["target_date"]) + timedelta(days=28)
        levers.update(target_date=moved.isoformat(), reading="Reads as moving the date out four weeks.")
    elif "scope" in question or scope:
        pct = int(scope.group(1)) if scope else 20
        levers.update(scope_change_pct=min(50, current["scope_change_pct"] + pct), reading=f"Reads as {pct}% more scope.")
    else:
        return {"answerable": False, "reason": "That cannot be expressed as capacity, date, or scope."}
    return levers


def _brief(payload: dict[str, Any]) -> dict[str, Any]:
    drivers = payload["variance"]["drivers"]
    top = drivers[0]["label"].lower() if drivers else "no signal"
    return {
        "headline": f"The largest driver of the forecast change is {top}.",
        "narrative": "Recorded sample briefing: the variance table is computed from the delivery signals; "
                     "this text only summarizes it.",
        "drivers": [{"key": d["key"], "explanation": f"{d['signal_value']:g} {d['unit']} reported by {d['source_name']}."}
                    for d in drivers],
        "watch_next": [f"{d['label']} next sprint" for d in drivers[:2]],
    }


ACTIONS = {
    "milestone": "Rebaseline the milestone with the predicted slip.",
    "azure_devops": "Defer the lowest-value unplanned work items.",
    "servicenow": "Escalate the blocking change approval to its sponsor.",
    "workforce": "Protect the lead engineer from production support.",
}


def _recovery_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    signals, forecast = payload["signals"], payload["current_forecast"]
    base, slip = forecast["on_time_pct"], forecast["projected_slip_days"]
    target = payload["initiative"]["current_target_date"]
    later = (date.fromisoformat(target) + timedelta(days=28)).isoformat()

    def scenario(identifier: str, title: str, fte: float, when: str, defer: int, cost: int, gain: int) -> dict[str, Any]:
        pct = min(95, base + gain)
        return {
            "id": identifier, "title": title, "summary": f"{title}: recorded sample scenario.",
            "allocated_fte": fte, "target_date": when, "scope_defer_pct": defer, "incremental_cost_k": cost,
            "predicted_on_time_pct": pct, "predicted_slip_days": max(0, round(slip * (100 - pct) / max(1, 100 - base))),
            "commentary": "Recorded sample estimate relative to the current forecast.",
            "trade_offs": ["Trade-off not assessed in recorded mode."], "risks": ["Risk not assessed in recorded mode."],
            "actions": ACTIONS,
        }

    return {
        "diagnosis": f"The forecast is {base}% on-time with {slip} projected slip days. Recorded sample diagnosis.",
        "recommended": "phase_scope",
        "scenarios": [
            scenario("add_contractors", "Add contractors", min(signals["capacity_fte"], signals["allocated_fte"] + 1.5), target, 0, 118, 7),
            scenario("phase_scope", "Phase scope", signals["allocated_fte"], target, 40, 0, 12),
            scenario("extend_date", "Extend the date", signals["allocated_fte"], later, 0, 0, 16),
        ],
    }


def _recovery_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    forecast = payload["context"]["current_forecast"]
    pct = int(round(payload["rules_check_pct"]))
    base = forecast["on_time_pct"]
    return {
        "predicted_on_time_pct": pct,
        "predicted_slip_days": max(0, round(forecast["projected_slip_days"] * (100 - pct) / max(1, 100 - base))),
        "commentary": "Recorded sample: the on-time figure equals the capacity-model check.",
        "trade_offs": ["Not assessed in recorded mode."], "risks": ["Not assessed in recorded mode."], "actions": ACTIONS,
    }


def _suggestions(payload: dict[str, Any]) -> dict[str, str]:
    preferred = {item["id"]: item["suggested"] for item in _questions()["questions"]}
    return {
        item["id"]: preferred[item["id"]] if preferred.get(item["id"]) in item["options"] else next(iter(item["options"]))
        for item in payload["questions"]
    }


def _rank(payload: dict[str, Any]) -> dict[str, Any]:
    projects = payload["candidate_projects"]
    return {
        "ranked_project_ids": [p["project_id"] for p in projects],
        "match_reasons": [
            {"project_id": p["project_id"],
             "reason": "Recorded sample ranking: shares " + (", ".join(p.get("shared_technologies") or []) or "no listed systems")
                       + f" and is {p.get('similarity_pct', 0)}% similar."}
            for p in projects
        ],
    }


def recorded_complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    system = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    try:
        if "clarifying questions" in system:
            return json.dumps(_questions())
        if "delivery forecasting expert" in system:
            return json.dumps(_forecast(payload))
        if "governance reviewer" in system:
            return json.dumps(_challenge(payload))
        if "what-if question" in system:
            return json.dumps(_ask(payload))
        if "delivery tracking analyst" in system:
            return json.dumps(_brief(payload))
        if "tuning a recovery scenario" in system:
            raise RecordedError("Recorded mode has no tuning sample; the deterministic lever search is used instead.")
        if "recovery advisor" in system:
            return json.dumps(_recovery_analysis(payload) if "propose 3 distinct recovery scenarios" in system
                              else _recovery_prediction(payload))
        if "Pick the best option" in system:
            return json.dumps(_suggestions(payload))
        if "Rank all supplied completed projects" in system:
            return json.dumps(_rank(payload))
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as error:
        raise RecordedError(f"The recorded answer could not be built: {error}") from error
    raise RecordedError("No recorded answer exists for this request.")
