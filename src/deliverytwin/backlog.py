"""Epic, Feature and User Story backlog for Azure DevOps, derived from the approved COMMIT baseline.

Everything here is deterministic: the effort split, the dates, the role assignments and the acceptance
criteria come from the approved baseline, its delivery fingerprint and the planned team allocation, not
from a model. When an INTERVENE recovery scenario has been applied, a second, plan-adjusted backlog is
built and compared with the baseline one. The result is a preview and exports the team can import into
Azure DevOps themselves; Delivery Twin never writes to an enterprise system.
"""
from __future__ import annotations

import csv
import html
import io
import re
from datetime import date, datetime, timedelta
from typing import Any

from .data.connection import Connection

ADO_COLUMNS = (
    "Work Item Type", "Title 1", "Title 2", "Title 3", "Description", "Tags", "Effort", "Start Date", "Target Date",
)
ACRONYMS = {"api", "cab", "crm", "erp", "etl", "hr", "mdm", "sso", "ui"}
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_EPIC_TITLE = 90
MAX_STORY_TITLE = 120
BASE_TAG = "DeliveryTwin"
ADO_PROJECT_URL = "https://dev.azure.com/{organization}/{project}"


def _label(slug: str) -> str:
    return " ".join(
        word.upper() if word in ACRONYMS else word.capitalize()
        for word in re.split(r"[-_\s]+", slug.strip()) if word
    )


def _epic_title(request: str) -> str:
    collapsed = " ".join(request.split())
    sentence = re.split(r"(?<=[.!?])\s", collapsed, maxsplit=1)[0].rstrip(".!? ")
    title = sentence or "Delivery Twin commitment"
    return title if len(title) <= MAX_EPIC_TITLE else f"{title[:MAX_EPIC_TITLE - 1].rstrip()}…"


def _split_hours(total: int, weights: list[int | float]) -> list[int]:
    """Split `total` hours in proportion to the weights so the parts add up exactly (in 5 h steps)."""
    if not weights:
        return []
    unit = 5 if total % 5 == 0 else 1
    units = total // unit
    weight_sum = sum(weights) or 1
    raw = [units * weight / weight_sum for weight in weights]
    parts = [int(value) for value in raw]
    by_remainder = sorted(range(len(raw)), key=lambda index: (parts[index] - raw[index], index))
    for index in by_remainder[: units - sum(parts)]:
        parts[index] += 1
    return [part * unit for part in parts]


def _feature_specs(fingerprint: dict[str, Any], allocated_fte: float) -> list[dict[str, Any]]:
    roles = list(fingerprint.get("roles") or [])
    complexity = fingerprint.get("complexity")
    release_weight = 15 + {"high": 10, "medium": 5}.get(complexity, 0)
    specs: list[dict[str, Any]] = [{
        "kind": "discovery", "title": "Discovery, design and delivery set-up", "weight": 10,
        "window": (0.0, 0.15), "roles": roles,
        "why": "Every similar completed project starts by agreeing scope and approach before build.",
        "criteria": [
            "Scope, in-scope systems and success measures are agreed with the sponsor.",
            "Target design and delivery plan are reviewed by the lead engineer.",
            f"Team roles and the {allocated_fte:g} FTE allocation are confirmed.",
        ],
    }]
    for dependency in fingerprint.get("dependencies") or []:
        needs_privacy = re.search(r"privacy|consent", dependency) and "privacy-engineer" in roles
        specs.append({
            "kind": "dependency", "title": f"Clear dependency: {_label(dependency)}", "weight": 6,
            "window": (0.0, 0.5), "roles": ["lead-engineer", *(["privacy-engineer"] if needs_privacy else [])],
            "own_roles": True,
            "why": "The clarified answers named this as an external dependency that can block the date.",
            "criteria": [
                f"An owner and a delivery date for {_label(dependency)} are confirmed in writing.",
                "The dependency is cleared before build features reach integration.",
                "Status is reported in TRACK until the dependency is closed.",
            ],
        })
    for system in fingerprint.get("systems") or []:
        specs.append({
            "kind": "system", "title": f"Deliver changes to {_label(system)}", "weight": 25,
            "window": (0.15, 0.75), "roles": roles,
            "why": "This system is part of the delivery fingerprint that the estimate was based on.",
            "criteria": [
                f"Changes to {_label(system)} needed for the request are built and demonstrated.",
                "Interfaces to the other in-scope systems are documented and tested.",
                "Effort stays within this feature's allocation, or the forecast is updated in TRACK.",
            ],
        })
    specs.append({
        "kind": "release", "title": "Test, cutover and hand-over", "weight": release_weight,
        "window": (0.75, 1.0), "roles": roles,
        "why": "Testing and cutover are sized from the integration buffer in similar completed projects.",
        "criteria": [
            "End-to-end and regression testing has passed.",
            "The cutover and rollback plan has been rehearsed.",
            "Hand-over to operations is complete by the target date.",
        ],
    })
    return specs


def _assignments(spec: dict[str, Any], effort: int, allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split a Feature's hours across roles, by planned FTE where the team plan is known."""
    if allocations and not spec.get("own_roles"):
        names = [item["role_name"] for item in allocations if item["allocated_fte"] > 0]
        weights = [item["allocated_fte"] for item in allocations if item["allocated_fte"] > 0]
    else:
        names, weights = list(spec["roles"]), [1] * len(spec["roles"])
    return [
        {"role": name, "hours": hours}
        for name, hours in zip(names, _split_hours(effort, weights)) if hours > 0
    ]


def build_backlog(
    baseline: dict[str, Any],
    as_of: date | None = None,
    allocations: list[dict[str, Any]] | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Epic, its Features and their User Stories from an approved baseline.

    `recovery` is an applied INTERVENE scenario (target_date, scope_defer_pct, allocated_fte): it moves the
    target date and defers that share of the build work into a follow-on Feature.
    """
    fingerprint = baseline.get("fingerprint") or {}
    committed = date.fromisoformat(str(baseline["target_date"])[:10])
    target = date.fromisoformat(str(recovery["target_date"])[:10]) if recovery else committed
    start = min(as_of or date.today(), target)
    span_days = (target - start).days
    p50, p80 = int(baseline["p50_hours"]), int(baseline["p80_hours"])
    fte = recovery["allocated_fte"] if recovery else baseline["allocated_fte"]
    allocations = allocations or []
    specs = _feature_specs(fingerprint, fte)
    efforts = _split_hours(p50, [spec["weight"] for spec in specs])
    defer_pct = float(recovery["scope_defer_pct"]) if recovery else 0.0
    deferred = 0
    features = []
    for spec, effort in zip(specs, efforts):
        if spec["kind"] == "system" and defer_pct:
            cut = int(round(effort * defer_pct / 100 / 5)) * 5
            effort, deferred = effort - cut, deferred + cut
        assignments = _assignments(spec, effort, allocations)
        primary = max(assignments, key=lambda item: item["hours"])["role"] if assignments else None
        features.append({
            "kind": spec["kind"], "title": spec["title"], "effort_hours": effort,
            "start_date": (start + timedelta(days=round(span_days * spec["window"][0]))).isoformat(),
            "target_date": (start + timedelta(days=round(span_days * spec["window"][1]))).isoformat(),
            "roles": [item["role"] for item in assignments], "assignments": assignments, "why": spec["why"],
            "acceptance_criteria": spec["criteria"],
            "stories": [{"title": criterion.rstrip(".")[:MAX_STORY_TITLE], "role": primary} for criterion in spec["criteria"]],
            "tags": [BASE_TAG, spec["kind"]],
        })
    if deferred:
        features.append({
            "kind": "deferred", "title": "Deferred scope: follow-on release", "effort_hours": deferred,
            "start_date": None, "target_date": None, "roles": [], "assignments": [],
            "why": "The applied recovery scenario defers this share of the build work to protect the committed date.",
            "acceptance_criteria": [
                "The deferred scope is re-planned for a follow-on release.",
                "The sponsor has accepted the deferral.",
            ],
            "stories": [], "tags": [BASE_TAG, "deferred"],
        })
    in_scope = sum(feature["effort_hours"] for feature in features if feature["kind"] != "deferred")
    epic_tags = [BASE_TAG, *(f"{key}:{fingerprint[key]}" for key in ("complexity", "delivery_pattern") if fingerprint.get(key))]
    if recovery:
        epic_tags.append("recovery-plan")
    description = (
        f"Approved COMMIT baseline: P50 {p50:,} h, P80 {p80:,} h, {baseline['allocated_fte']:g} FTE of "
        f"{baseline['capacity_fte']:g}, target {committed.isoformat()}, {baseline['confidence_pct']:g}% on-time confidence."
    )
    if recovery:
        description += (
            f" Adjusted for the applied recovery scenario: {fte:g} FTE, target {target.isoformat()}, "
            f"{defer_pct:g}% of build scope deferred ({deferred:,} h)."
        )
    backlog = {
        "source": "rules", "model": None, "baseline_id": baseline.get("id"),
        "variant": "recovery_plan" if recovery else "baseline",
        "epic": {
            "title": _epic_title(baseline.get("request_text") or ""), "description": description,
            "effort_hours": in_scope, "p80_hours": p80, "reserve_hours": p80 - p50, "deferred_hours": deferred,
            "start_date": start.isoformat(), "target_date": target.isoformat(),
            "tags": epic_tags, "features": features,
        },
    }
    backlog["csv"] = to_ado_csv(backlog)
    backlog["csv_filename"] = (
        f"deliverytwin-ado-{'recovery-plan' if recovery else 'baseline'}-{start.isoformat()}.csv"
    )
    return backlog


def diff_backlogs(base: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, str]]:
    """Human-readable changes between the baseline backlog and the recovery-plan backlog."""
    changes: list[dict[str, str]] = []
    left, right = base["epic"], plan["epic"]
    if left["target_date"] != right["target_date"]:
        days = (date.fromisoformat(right["target_date"]) - date.fromisoformat(left["target_date"])).days
        changes.append({"item": "Epic", "change": f"Target date {left['target_date']} → {right['target_date']} ({days:+d} days)"})
    if left["effort_hours"] != right["effort_hours"]:
        changes.append({"item": "Epic", "change": f"In-scope effort {left['effort_hours']:,} h → {right['effort_hours']:,} h"})
    before = {feature["title"]: feature for feature in left["features"]}
    for feature in right["features"]:
        old = before.get(feature["title"])
        if old is None:
            changes.append({"item": feature["title"], "change": f"Added with {feature['effort_hours']:,} h"})
            continue
        parts = []
        if old["effort_hours"] != feature["effort_hours"]:
            parts.append(f"effort {old['effort_hours']:,} h → {feature['effort_hours']:,} h")
        if old["target_date"] != feature["target_date"]:
            parts.append(f"target {old['target_date']} → {feature['target_date']}")
        if parts:
            changes.append({"item": feature["title"], "change": ", ".join(parts)})
    return changes


def _safe_cell(value: str) -> str:
    # Spreadsheet apps run cells that start with these characters as formulas.
    return f"'{value}" if value.startswith(CSV_FORMULA_PREFIXES) else value


def _html_list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"


def _feature_description(feature: dict[str, Any]) -> str:
    description = (
        f"<p>{html.escape(feature['why'])}</p><p>Acceptance criteria</p>{_html_list(feature['acceptance_criteria'])}"
    )
    if feature["assignments"]:
        split = ", ".join(f"{_label(item['role'])} {item['hours']:,} h" for item in feature["assignments"])
        description += f"<p>Suggested role split: {html.escape(split)}. Effort is in hours.</p>"
    return description


def to_ado_csv(backlog: dict[str, Any]) -> str:
    """CSV in the Azure DevOps hierarchical import layout: Epic in `Title 1`, Features in `Title 2`, Stories in `Title 3`."""
    epic = backlog["epic"]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(ADO_COLUMNS)
    writer.writerow([
        "Epic", _safe_cell(epic["title"]), "", "", f"<p>{html.escape(epic['description'])}</p>",
        "; ".join(epic["tags"]), epic["effort_hours"], epic["start_date"], epic["target_date"],
    ])
    for feature in epic["features"]:
        writer.writerow([
            "Feature", "", _safe_cell(feature["title"]), "", _feature_description(feature),
            "; ".join(feature["tags"]), feature["effort_hours"], feature["start_date"] or "", feature["target_date"] or "",
        ])
        for story in feature["stories"]:
            tags = [BASE_TAG, *([f"role:{story['role']}"] if story["role"] else [])]
            writer.writerow([
                "User Story", "", "", _safe_cell(story["title"]),
                f"<p>Part of: {html.escape(feature['title'])}</p>", "; ".join(tags), "", "", "",
            ])
    return out.getvalue()


def to_ado_rest(backlog: dict[str, Any]) -> dict[str, Any]:
    """The same backlog as Azure DevOps REST work-item requests (JSON Patch). Nothing here is sent anywhere."""
    epic = backlog["epic"]

    def patch(title: str, description: str, tags: list[str], effort: int | None, start: str | None, target: str | None,
              parent: str | None) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": description},
            {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)},
        ]
        for field, value in (("Effort", effort), ("StartDate", start), ("TargetDate", target)):
            if value not in (None, ""):
                operations.append({"op": "add", "path": f"/fields/Microsoft.VSTS.Scheduling.{field}", "value": value})
        if parent:
            operations.append({
                "op": "add", "path": "/relations/-",
                "value": {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": f"{ADO_PROJECT_URL}/_apis/wit/workItems/{{id of {parent}}}"},
            })
        return operations

    items = [{
        "tempId": "epic-1", "type": "Epic", "parent": None,
        "method": "POST", "url": f"{ADO_PROJECT_URL}/_apis/wit/workitems/$Epic?api-version=7.1",
        "contentType": "application/json-patch+json",
        "body": patch(epic["title"], f"<p>{html.escape(epic['description'])}</p>", epic["tags"], epic["effort_hours"],
                      epic["start_date"], epic["target_date"], None),
    }]
    for number, feature in enumerate(epic["features"], start=1):
        feature_id = f"feature-{number}"
        items.append({
            "tempId": feature_id, "type": "Feature", "parent": "epic-1", "method": "POST",
            "url": f"{ADO_PROJECT_URL}/_apis/wit/workitems/$Feature?api-version=7.1",
            "contentType": "application/json-patch+json",
            "body": patch(feature["title"], _feature_description(feature), feature["tags"], feature["effort_hours"],
                          feature["start_date"], feature["target_date"], "epic-1"),
        })
        for index, story in enumerate(feature["stories"], start=1):
            items.append({
                "tempId": f"{feature_id}-story-{index}", "type": "User Story", "parent": feature_id, "method": "POST",
                "url": f"{ADO_PROJECT_URL}/_apis/wit/workitems/$User%20Story?api-version=7.1",
                "contentType": "application/json-patch+json",
                "body": patch(story["title"], f"<p>Part of: {html.escape(feature['title'])}</p>",
                              [BASE_TAG, *([f"role:{story['role']}"] if story["role"] else [])], None, None, None, feature_id),
            })
    return {
        "note": (
            "Generated by Delivery Twin and NOT sent to Azure DevOps. Replace {organization} and {project}, create "
            "work items in order, and replace each {id of ...} with the id Azure DevOps returned for that parent."
        ),
        "workItems": items,
    }


def _plan_inputs(connection: Connection, approved_at: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    from .data import RecoveryScenarioRepository

    allocations = [
        dict(row) for row in connection.execute(
            "SELECT role_name, ROUND(SUM(allocated_fte), 1) AS allocated_fte FROM planview_resource_assignments "
            "GROUP BY role_name ORDER BY role_name"
        )
    ]
    row = RecoveryScenarioRepository(connection.app_database()).latest()
    recovery = None
    if row:
        # saved_at has second precision and approved_at microseconds, so compare as times at second precision.
        approved = datetime.fromisoformat(approved_at).replace(microsecond=0)
        if datetime.fromisoformat(str(row["saved_at"])) >= approved:
            recovery = row
    return allocations, recovery


def latest_backlog(connection: Connection, as_of: date | None = None) -> dict[str, Any] | None:
    """The backlog for the latest approved baseline, plus a recovery-plan variant once a scenario is applied."""
    from .data import BaselineRepository

    baseline = BaselineRepository(connection.app_database()).latest_scope()
    if baseline is None:
        return None
    approved = datetime.fromisoformat(baseline["approved_at"]).date()
    start = as_of or max(approved, date.today())
    allocations, recovery = _plan_inputs(connection, baseline["approved_at"])
    result = build_backlog(baseline, start, allocations)
    result["plan"], result["diff"] = None, []
    if recovery:
        plan = build_backlog(baseline, start, allocations, recovery)
        plan["scenario"] = {key: recovery[key] for key in ("recommended_option", "allocated_fte", "scope_defer_pct", "target_date")}
        result["plan"], result["diff"] = plan, diff_backlogs(result, plan)
    return result
