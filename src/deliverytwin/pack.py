"""A shareable decision pack for the latest approved baseline.

The pack is plain Markdown built from stored, server-computed facts. An optional AI review that the
person saw on screen can be included; it is validated, labeled with its source, and never changes a number.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from .backlog import _label, latest_backlog, to_ado_rest

MAX_REVIEW_TEXT = 500
VERDICTS = {"ready": "Ready to approve", "ready_with_conditions": "Ready with conditions", "not_ready": "Not ready"}


def _clean_text(value: object, limit: int = MAX_REVIEW_TEXT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:limit].replace("|", "/")


def clean_review(review: object) -> dict[str, Any] | None:
    """Accept the review shown on screen only if it is well formed; anything else is left out of the pack."""
    if not isinstance(review, dict) or review.get("verdict") not in VERDICTS:
        return None
    summary = _clean_text(review.get("summary"))
    if not summary:
        return None
    raw_risks = review.get("risks") if isinstance(review.get("risks"), list) else []
    risks = []
    for item in raw_risks[:5]:
        if isinstance(item, dict) and _clean_text(item.get("title")) and _clean_text(item.get("detail")):
            risks.append({
                "title": _clean_text(item["title"], 120),
                "severity": item.get("severity") if item.get("severity") in ("high", "medium", "low") else "low",
                "detail": _clean_text(item["detail"]),
            })
    raw_conditions = review.get("conditions") if isinstance(review.get("conditions"), list) else []
    conditions = [c for c in (_clean_text(x, 240) for x in raw_conditions[:4]) if c]
    model = _clean_text(review.get("model"), 60)
    source = f"AI review ({model})" if review.get("source") == "llm" and model else (
        "AI review" if review.get("source") == "llm" else "Rules-based review"
    )
    return {"verdict": review["verdict"], "summary": summary, "risks": risks, "conditions": conditions, "source": source}


def _signal_origin(connection: sqlite3.Connection) -> str:
    from .scenarios import IMPORT_SCENARIO, SCENARIOS, scenario_key

    if not connection.execute("SELECT 1 FROM simulation_state WHERE id = 1").fetchone():
        return "No delivery signals have been simulated or imported yet."
    key = scenario_key(connection)
    if key == IMPORT_SCENARIO:
        return "TRACK signals come from exports uploaded by a person, not from a live connection."
    return f"TRACK signals come from the synthetic scenario “{SCENARIOS[key].label}”, not from a live connection."


def build_pack(connection: sqlite3.Connection, review: object = None, as_of: date | None = None) -> dict[str, Any] | None:
    from .data import BaselineRepository, SQLiteDatabase

    baseline = BaselineRepository(SQLiteDatabase(connection)).latest_scope()
    backlog = latest_backlog(connection, as_of)
    if baseline is None or backlog is None:
        return None
    fingerprint = baseline["fingerprint"]
    checked = clean_review(review)
    epic = backlog["epic"]
    today = date.today().isoformat()
    lines = [
        "# Delivery Twin decision pack", "",
        f"Generated {today}. All source data is synthetic or uploaded by a person; nothing was "
        "written back to any enterprise system, including Azure DevOps.", "",
        "## Request", "", baseline["request_text"].strip() or "(none recorded)", "",
        "## Approved baseline", "",
        "| Item | Value |", "|---|---|",
        f"| Baseline id | {baseline['id']} |", f"| Approved at | {baseline['approved_at']} |",
        f"| Most likely effort (P50) | {baseline['p50_hours']:,.0f} h |",
        f"| Pessimistic effort (P80) | {baseline['p80_hours']:,.0f} h |",
        f"| Integration buffer | {baseline['integration_buffer_pct']}% |",
        f"| Allocated FTE | {baseline['allocated_fte']:g} of {baseline['capacity_fte']:g} |",
        f"| Target date | {baseline['target_date']} |", f"| On-time confidence | {baseline['confidence_pct']:g}% |", "",
        "## Delivery fingerprint", "",
        f"- Structure: {_label(str(fingerprint.get('structure', 'unknown')))}",
        f"- Complexity: {fingerprint.get('complexity', 'unknown')}",
        f"- Delivery pattern: {_label(str(fingerprint.get('delivery_pattern', 'unknown')))}",
        f"- Systems: {', '.join(_label(s) for s in fingerprint.get('systems', [])) or 'none'}",
        f"- Roles: {', '.join(_label(r) for r in fingerprint.get('roles', [])) or 'none'}",
        f"- Dependencies: {', '.join(_label(d) for d in fingerprint.get('dependencies', [])) or 'none'}", "",
    ]
    if checked:
        lines += [
            "## Pre-commit review", "",
            f"Source: {checked['source']}. Verdict: **{VERDICTS[checked['verdict']]}**. "
            "Advisory only; the feasibility check and the human approval decide.", "", checked["summary"], "",
        ]
        lines += [f"- **{risk['title']}** ({risk['severity']}): {risk['detail']}" for risk in checked["risks"]]
        if checked["conditions"]:
            lines += ["", "Conditions to attach before approving:"] + [f"- {c}" for c in checked["conditions"]]
        lines.append("")
    lines += [
        "## Azure DevOps backlog (preview)", "",
        f"One Epic, {len(epic['features'])} Features. Effort adds up to the approved P50 "
        f"({epic['effort_hours']:,} h in scope). Import the CSV yourself; nothing is sent.", "",
        "| Feature | Effort | Start | Target | Suggested roles |", "|---|---|---|---|---|",
    ]
    for feature in epic["features"]:
        roles = ", ".join(f"{_label(a['role'])} {a['hours']:,} h" for a in feature["assignments"]) or "-"
        lines.append(
            f"| {feature['title']} | {feature['effort_hours']:,} h | {feature['start_date'] or '-'} | "
            f"{feature['target_date'] or '-'} | {roles} |"
        )
    lines.append("")
    if backlog["plan"]:
        scenario = backlog["plan"]["scenario"]
        lines += [
            "## Applied recovery scenario (simulated)", "",
            f"Option: {_label(scenario['recommended_option'])}. {scenario['allocated_fte']:g} FTE, target "
            f"{scenario['target_date']}, {scenario['scope_defer_pct']}% of build scope deferred. "
            "Recorded locally only; no source system was changed.", "", "Changes to the backlog:", "",
        ]
        lines += [f"- **{item['item']}**: {item['change']}" for item in backlog["diff"]] or ["- No backlog changes."]
        lines.append("")
    lines += [
        "## Data lineage", "",
        "- The effort evidence comes from synthetic completed reference projects, not from a live system.",
        f"- {_signal_origin(connection)}",
        "- Backlog hours, dates and roles are computed by code from the approved baseline.", "",
    ]
    return {
        "filename": f"deliverytwin-decision-pack-{today}.md",
        "markdown": "\n".join(lines),
        "ado_rest": to_ado_rest(backlog["plan"] or backlog),
        "ado_rest_filename": f"deliverytwin-ado-rest-{today}.json",
        "includes_review": checked is not None,
    }
