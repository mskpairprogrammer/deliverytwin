"""Epic and Feature backlog for Azure DevOps, derived from the approved COMMIT baseline.

Everything here is deterministic: the effort split, the dates and the acceptance criteria come from the
approved baseline and its delivery fingerprint, not from a model. The result is a preview and a CSV the
team can import into Azure DevOps themselves; Delivery Twin never writes to an enterprise system.
"""
from __future__ import annotations

import csv
import html
import io
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

ADO_COLUMNS = (
    "Work Item Type", "Title 1", "Title 2", "Description", "Tags", "Effort", "Start Date", "Target Date",
)
ACRONYMS = {"api", "cab", "crm", "erp", "etl", "hr", "mdm", "sso", "ui"}
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_EPIC_TITLE = 90
BASE_TAG = "DeliveryTwin"


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


def _split_hours(total: int, weights: list[int]) -> list[int]:
    """Split `total` hours in proportion to the weights so the parts add up exactly (in 5 h steps)."""
    unit = 5 if total % 5 == 0 else 1
    units = total // unit
    weight_sum = sum(weights)
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


def build_backlog(baseline: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """Build the Epic and its Features from an approved baseline (see `BaselineRepository.latest_scope`)."""
    fingerprint = baseline.get("fingerprint") or {}
    target = date.fromisoformat(str(baseline["target_date"])[:10])
    start = min(as_of or date.today(), target)
    span_days = (target - start).days
    p50, p80 = int(baseline["p50_hours"]), int(baseline["p80_hours"])
    specs = _feature_specs(fingerprint, baseline["allocated_fte"])
    efforts = _split_hours(p50, [spec["weight"] for spec in specs])
    features = []
    for spec, effort in zip(specs, efforts):
        feature_start = start + timedelta(days=round(span_days * spec["window"][0]))
        feature_end = start + timedelta(days=round(span_days * spec["window"][1]))
        features.append({
            "kind": spec["kind"], "title": spec["title"], "effort_hours": effort,
            "start_date": feature_start.isoformat(), "target_date": feature_end.isoformat(),
            "roles": spec["roles"], "why": spec["why"], "acceptance_criteria": spec["criteria"],
            "tags": [BASE_TAG, spec["kind"]],
        })
    epic_tags = [BASE_TAG, *(f"{key}:{fingerprint[key]}" for key in ("complexity", "delivery_pattern") if fingerprint.get(key))]
    backlog = {
        "source": "rules", "model": None, "baseline_id": baseline.get("id"),
        "epic": {
            "title": _epic_title(baseline.get("request_text") or ""),
            "description": (
                f"Approved COMMIT baseline: P50 {p50:,} h, P80 {p80:,} h, {baseline['allocated_fte']:g} FTE of "
                f"{baseline['capacity_fte']:g}, target {target.isoformat()}, "
                f"{baseline['confidence_pct']:g}% on-time confidence."
            ),
            "effort_hours": p50, "p80_hours": p80, "reserve_hours": p80 - p50,
            "start_date": start.isoformat(), "target_date": target.isoformat(),
            "tags": epic_tags, "features": features,
        },
    }
    backlog["csv"] = to_ado_csv(backlog)
    backlog["csv_filename"] = f"deliverytwin-ado-backlog-{start.isoformat()}.csv"
    return backlog


def _safe_cell(value: str) -> str:
    # Spreadsheet apps run cells that start with these characters as formulas.
    return f"'{value}" if value.startswith(CSV_FORMULA_PREFIXES) else value


def _html_list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"


def to_ado_csv(backlog: dict[str, Any]) -> str:
    """CSV in the Azure DevOps hierarchical import layout: the Epic in `Title 1`, Features in `Title 2`."""
    epic = backlog["epic"]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(ADO_COLUMNS)
    writer.writerow([
        "Epic", _safe_cell(epic["title"]), "", f"<p>{html.escape(epic['description'])}</p>",
        "; ".join(epic["tags"]), epic["effort_hours"], epic["start_date"], epic["target_date"],
    ])
    for feature in epic["features"]:
        description = (
            f"<p>{html.escape(feature['why'])}</p><p>Acceptance criteria</p>{_html_list(feature['acceptance_criteria'])}"
            f"<p>Suggested roles: {html.escape(', '.join(feature['roles']))}. Effort is in hours.</p>"
        )
        writer.writerow([
            "Feature", "", _safe_cell(feature["title"]), description, "; ".join(feature["tags"]),
            feature["effort_hours"], feature["start_date"], feature["target_date"],
        ])
    return out.getvalue()


def latest_backlog(connection: sqlite3.Connection, as_of: date | None = None) -> dict[str, Any] | None:
    """The backlog for the latest approved baseline, or None when nothing is approved yet."""
    from .data import BaselineRepository, SQLiteDatabase

    baseline = BaselineRepository(SQLiteDatabase(connection)).latest_scope()
    if baseline is None:
        return None
    approved = datetime.fromisoformat(baseline["approved_at"]).date()
    return build_backlog(baseline, as_of or max(approved, date.today()))
