"""Deterministic synthetic portfolio shaped like the delivery_intel Delta tables.

The generator models fifty completed projects (HIST-001..HIST-050) whose effort,
duration, and risk follow explicit drivers (complexity, integration breadth,
delivery pattern). Teams plan optimistically, so planned effort is systematically
below actual effort and the overrun grows with risk. Everything is derived from
a fixed seed; no real project data is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

if not __package__:  # run as `python scripts/generate_data.py`: make the package in src/ importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deliverytwin.storage.paths import DATASET_DIR, TRANSCRIPT_DIRNAME
from deliverytwin.storage.schema import TABLE_PREFIX, TABLES

SEED = 2026

ROLE_SKILL = {
    "lead-engineer": "senior",
    "delivery-engineer": "mid",
    "data-engineer": "senior",
    "privacy-engineer": "senior",
    "platform-engineer": "senior",
    "customer-experience-engineer": "mid",
    "vendor-liaison": "mid",
}
ROLE_TITLE = {role: role.replace("-", " ").title() for role in ROLE_SKILL}

FAMILIES = (
    {
        "key": "customer-data", "structure": "legacy-modernization",
        "pattern": "incremental-integration", "lob": "Customer Experience",
        "base_hours": 4300, "team": 6.0,
        "core_systems": ["customer-data-platform", "crm"],
        "optional_systems": ["service-management", "commerce", "event-streaming", "mdm",
                             "identity-resolution", "profile-api", "batch-integration"],
        "core_roles": {"lead-engineer": 0.14, "delivery-engineer": 0.40, "data-engineer": 0.34},
        "optional_roles": {"privacy-engineer": 0.12, "platform-engineer": 0.08},
        "dependencies": ["privacy-review", "consent-management", "crm-mdm-integration"],
        "problem": "unify customer records across CRM, commerce, and service channels",
        "names": [
            "Customer Profile Unification", "CRM and Service Profile Consolidation",
            "Customer Identity Resolution Service", "Customer Master Data Hub",
            "Consent & Preference Hub", "Contact Center Profile API", "Loyalty Data Platform",
            "Customer Data Lakehouse", "Household Graph Service", "Customer Segmentation Feed",
            "Preference Center Modernization", "Support Case Context Service",
        ],
    },
    {
        "key": "customer-greenfield", "structure": "greenfield-service",
        "pattern": "iterative-greenfield", "lob": "Digital Channels",
        "base_hours": 2300, "team": 4.5,
        "core_systems": ["customer-profile-service"],
        "optional_systems": ["customer-portal", "profile-api", "identity-provider"],
        "core_roles": {"lead-engineer": 0.18, "delivery-engineer": 0.50,
                       "customer-experience-engineer": 0.32},
        "optional_roles": {},
        "dependencies": ["identity-provider"],
        "problem": "launch a new customer-facing profile experience with explicit data contracts",
        "names": [
            "Customer Profile Workspace", "Agent Console Profile Panel",
            "Self-Service Account Portal", "Customer Timeline Service",
            "Digital Onboarding Profile",
        ],
    },
    {
        "key": "vendor-integration", "structure": "vendor-integration",
        "pattern": "big-bang-cutover", "lob": "Shared Services",
        "base_hours": 5400, "team": 6.5,
        "core_systems": ["batch-integration", "crm"],
        "optional_systems": ["erp", "service-management"],
        "core_roles": {"lead-engineer": 0.12, "delivery-engineer": 0.42, "data-engineer": 0.30,
                       "vendor-liaison": 0.16},
        "optional_roles": {},
        "dependencies": ["vendor-contract", "privacy-review"],
        "problem": "integrate a hosted vendor platform and cut over from the incumbent system",
        "names": [
            "CRM Vendor Integration", "Hosted Service Desk Cutover", "ERP Customer Master Sync",
            "Marketing Cloud Migration", "Contact Center Platform Replacement",
        ],
    },
    {
        "key": "commerce", "structure": "legacy-modernization",
        "pattern": "incremental-integration", "lob": "Commerce",
        "base_hours": 3400, "team": 5.0,
        "core_systems": ["commerce", "event-streaming"],
        "optional_systems": ["service-management", "batch-integration", "crm", "profile-api"],
        "core_roles": {"lead-engineer": 0.14, "delivery-engineer": 0.50, "data-engineer": 0.36},
        "optional_roles": {"privacy-engineer": 0.08},
        "dependencies": ["crm-mdm-integration", "privacy-review"],
        "problem": "synchronize commerce events with downstream order and service workflows",
        "names": [
            "Order History Sync", "Cart and Checkout Event Stream", "Returns Portal Integration",
            "Subscription Billing Profile Sync", "Product Catalog Feed",
            "Fulfillment Status Gateway",
        ],
    },
    {
        "key": "lakehouse", "structure": "legacy-modernization",
        "pattern": "incremental-integration", "lob": "Data & Analytics",
        "base_hours": 5600, "team": 7.0,
        "core_systems": ["customer-data-platform", "azure", "databricks", "delta-lake"],
        "optional_systems": ["event-streaming", "identity-resolution", "mdm"],
        "core_roles": {"lead-engineer": 0.10, "platform-engineer": 0.28, "data-engineer": 0.40,
                       "delivery-engineer": 0.22},
        "optional_roles": {"privacy-engineer": 0.08},
        "dependencies": ["private-network", "identity-provider", "privacy-review"],
        "problem": "build governed Azure lakehouse capabilities on Databricks and Delta Lake",
        "names": [
            "Enterprise Lakehouse Foundation", "Streaming Ingestion Platform",
            "Feature Store Enablement", "Data Quality Observability",
            "Regional Data Residency Program", "Unity Catalog Governance Rollout",
        ],
    },
    {
        "key": "api", "structure": "legacy-modernization",
        "pattern": "incremental-integration", "lob": "Integration",
        "base_hours": 2900, "team": 4.5,
        "core_systems": ["api-management", "event-streaming"],
        "optional_systems": ["identity-provider", "payments-gateway", "service-management"],
        "core_roles": {"lead-engineer": 0.15, "delivery-engineer": 0.55, "platform-engineer": 0.30},
        "optional_roles": {},
        "dependencies": ["identity-provider", "private-network"],
        "problem": "expose governed integration APIs to partners and internal consumers",
        "names": [
            "Partner API Gateway", "Payments Event Integration", "Order Management Integration",
            "Supplier Connectivity API", "Partner Portal Single Sign-On",
            "Mobile Backend Modernization",
        ],
    },
    {
        "key": "security", "structure": "legacy-modernization",
        "pattern": "incremental-integration", "lob": "Risk & Compliance",
        "base_hours": 3800, "team": 5.5,
        "core_systems": ["identity-resolution", "mdm"],
        "optional_systems": ["identity-provider", "profile-api", "crm"],
        "core_roles": {"lead-engineer": 0.12, "privacy-engineer": 0.30, "platform-engineer": 0.20,
                       "delivery-engineer": 0.38},
        "optional_roles": {},
        "dependencies": ["privacy-review", "consent-management", "identity-provider"],
        "problem": "automate privacy, consent, and access controls across customer systems",
        "names": [
            "Privacy Request Automation", "Consent Ledger Modernization",
            "PII Discovery and Masking", "Access Governance Refresh",
            "Data Retention Enforcement",
        ],
    },
    {
        "key": "erp", "structure": "legacy-modernization",
        "pattern": "big-bang-cutover", "lob": "Finance",
        "base_hours": 6200, "team": 8.0,
        "core_systems": ["erp", "batch-integration"],
        "optional_systems": ["service-management", "customer-master"],
        "core_roles": {"lead-engineer": 0.11, "delivery-engineer": 0.44, "data-engineer": 0.31,
                       "vendor-liaison": 0.14},
        "optional_roles": {},
        "dependencies": ["vendor-contract"],
        "problem": "replace core finance and billing platforms in a coordinated cutover",
        "names": [
            "Billing Profile Master", "Finance Close Automation", "Procurement Platform Upgrade",
            "Order-to-Cash Consolidation", "Revenue Recognition Integration",
        ],
    },
)

FIRST_NAMES = ("Avery", "Jordan", "Priya", "Marcus", "Elena", "Tomas", "Aisha", "Kenji", "Sofia",
               "Daniel", "Mei", "Omar", "Nadia", "Luca", "Imani", "Rafael", "Hana", "Victor",
               "Leila", "Gabriel", "Yuki", "Carmen", "Idris", "Anika")
LAST_NAMES = ("Nguyen", "Okafor", "Patel", "Silva", "Kowalski", "Ibrahim", "Moreau", "Tanaka",
              "Alvarez", "Brennan", "Chaudhry", "Dubois", "Eriksen", "Fofana", "Grant",
              "Haddad", "Ivanov", "Jensen", "Kapoor", "Lindqvist", "Mensah", "Novak")

DEPENDENCY_GATES = {
    "privacy-review": ("Privacy impact assessment", "Privacy Office"),
    "consent-management": ("Consent model sign-off", "Data Governance"),
    "crm-mdm-integration": ("CRM/MDM integration design", "Architecture Board"),
    "identity-provider": ("Identity provider onboarding", "Security Engineering"),
    "vendor-contract": ("Vendor contract execution", "Procurement"),
    "private-network": ("Private network approval", "Cloud Platform"),
}
RISK_TEXT = {
    "privacy-review": "privacy review lead time",
    "consent-management": "consent model ambiguity",
    "crm-mdm-integration": "CRM/MDM match-rule changes",
    "identity-provider": "identity provider onboarding delays",
    "vendor-contract": "vendor delivery commitments",
    "private-network": "network and firewall approvals",
}
COMPLEXITY_INDEX = {"low": 0, "medium": 1, "high": 2}


def _round50(value: float) -> int:
    return int(round(value / 50.0)) * 50


def _lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def _person(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _lessons(project: dict[str, Any]) -> list[dict[str, str]]:
    systems = set(project["systems"])
    deps = set(project["dependencies"])
    name = project["name"]
    pattern = project["pattern"]
    if pattern == "big-bang-cutover":
        architecture = ("big_bang", f"{name} required a coordinated cutover with a rehearsed rollback window.")
    elif pattern == "iterative-greenfield":
        architecture = ("greenfield", f"{name} used a new service with independent data contracts and short release cycles.")
    else:
        architecture = ("incremental", f"{name} moved consumers in phases while existing workflows stayed available.")
    if "event-streaming" in systems:
        data = ("event_sync", f"Near-real-time events from CRM, commerce, and service systems kept customer profile data in sync for {name}, with replay and reconciliation controls.")
    elif "batch-integration" in systems:
        data = ("batch_sync", f"Scheduled batch reconciliation of customer records in {name} exposed mismatches before release.")
    else:
        data = ("single_source", f"{name} relied on an existing master record, so no additional data movement was built.")
    if "privacy-review" in deps or "consent-management" in deps:
        security = ("pii_consent", f"{name} classified PII and completed consent and retention reviews before each release.")
    else:
        security = ("standard", f"{name} passed standard access reviews because no sensitive attributes were in scope.")
    if systems & {"mdm", "identity-resolution", "profile-api"}:
        reuse = ("reuse_mdm", f"{name} reused existing match rules and profile APIs rather than building new identity logic.")
    else:
        reuse = ("build_new", f"{name} built new components because no suitable shared capability existed.")
    return [
        {"category": category, "suggested_option": option, "lesson": lesson}
        for category, (option, lesson) in zip(
            ("architecture", "data", "security", "reuse"), (architecture, data, security, reuse)
        )
    ]


def _build_project(rng: random.Random, family: dict[str, Any], name: str, index: int) -> dict[str, Any]:
    optional = family["optional_systems"]
    count = rng.randint(min(2, len(optional)), len(optional)) if optional else 0
    systems = sorted(set(family["core_systems"]) | set(rng.sample(optional, count)))
    dependencies = sorted(dep for dep in family["dependencies"] if rng.random() < 0.72) or [
        family["dependencies"][0]
    ]
    roles = dict(family["core_roles"])
    for role, share in family["optional_roles"].items():
        if role == "privacy-engineer" and not {"privacy-review", "consent-management"} & set(dependencies):
            continue
        if rng.random() < 0.7:
            roles[role] = share
    total_share = sum(roles.values())
    roles = {role: share / total_share for role, share in roles.items()}

    big_bang = family["pattern"] == "big-bang-cutover"
    score = (
        (len(systems) - 3) * 0.55
        + len(dependencies) * 0.6
        + (1.0 if big_bang else 0.0)
        + (0.4 if "privacy-engineer" in roles else 0.0)
        + rng.uniform(-0.5, 0.5)
    )
    complexity = "high" if score >= 3.3 else "medium" if score >= 1.9 else "low"
    actual_hours = _round50(
        family["base_hours"] * (0.74 + 0.13 * max(score, 0.0)) * _lognormal(rng, 1.0, 0.09)
    )
    overrun = _lognormal(rng, 1.10, 0.19) + (0.08 if big_bang else 0.0) + (0.05 if complexity == "high" else 0.0)
    overrun = min(max(overrun, 0.8), 2.0)
    p50 = _round50(actual_hours / overrun)
    spread = 1.13 + 0.05 * COMPLEXITY_INDEX[complexity] + rng.uniform(0.0, 0.05)
    p80 = max(_round50(p50 * spread), p50 + 50)
    contingency = int(min(max(round(6 + 3 * max(score, 0) + (4 if big_bang else 0) + rng.uniform(-2, 2)), 6), 24))
    team = family["team"] * (0.8 + 0.1 * max(score, 0.0)) * _lognormal(rng, 1.0, 0.08)
    planned_weeks = int(min(max(round(p50 / (team * 40 * 0.9)), 8), 52))
    actual_weeks = int(min(max(round(planned_weeks * overrun ** 0.8 * _lognormal(rng, 1.0, 0.06)), 6), 70))
    confidence = round(
        min(max(0.93 - 0.10 * COMPLEXITY_INDEX[complexity] - 0.30 * max(overrun - 1.0, 0.0)
                + rng.uniform(-0.04, 0.04), 0.45), 0.93), 2
    )
    avg_fte = actual_hours / (actual_weeks * 40)
    submitted = date(2022, 1, 10) + timedelta(days=int(index * 1000 / 50) + rng.randint(0, 12))
    return {
        "id": f"HIST-{index + 1:03d}", "name": name, "family": family["key"],
        "structure": family["structure"], "pattern": family["pattern"], "lob": family["lob"],
        "problem": family["problem"], "systems": systems, "dependencies": dependencies,
        "roles": roles, "complexity": complexity, "score": score, "actual_hours": actual_hours,
        "p50": p50, "p80": p80, "contingency": contingency, "planned_weeks": planned_weeks,
        "actual_weeks": actual_weeks, "confidence": confidence, "avg_fte": avg_fte,
        "overrun": overrun, "submitted": submitted,
    }


def _size_label(hours: int) -> str:
    return "S" if hours < 2500 else "M" if hours < 4000 else "L" if hours < 6000 else "XL"


def _outcome(project: dict[str, Any]) -> str:
    delay = project["actual_weeks"] - project["planned_weeks"]
    if delay <= 0:
        return "Delivered on schedule"
    pct = delay / project["planned_weeks"]
    if pct <= 0.15:
        return f"Delivered {delay} weeks late with a minor schedule slip"
    return f"Delivered {delay} weeks late after replanning"


def generate_dataset(seed: int = SEED) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    projects: list[dict[str, Any]] = []
    for family in FAMILIES:
        for name in family["names"]:
            projects.append(_build_project(rng, family, name, len(projects)))

    tables: dict[str, list[dict[str, Any]]] = {table.dataset: [] for table in TABLES.values()}
    workitem_number = 1000
    for project in projects:
        pid = project["id"]
        systems = ",".join(project["systems"])
        deps = ",".join(project["dependencies"])
        risks = ", ".join(RISK_TEXT[dep] for dep in project["dependencies"])
        tables["project_intakes"].append({
            "project_id": pid, "project_name": project["name"],
            "problem_statement_and_desired_outcome": (
                f"The business needs to {project['problem']}. Success means fewer manual "
                f"hand-offs and a single trusted view for {project['lob'].lower()} teams."),
            "overall_scope": (
                f"Deliver {project['name']} across {len(project['systems'])} systems "
                f"({systems.replace(',', ', ')}) using a {project['pattern'].replace('-', ' ')} approach."),
            "affected_systems": systems,
            "business_strategy": f"Improve {project['lob'].lower()} outcomes while reducing operating risk.",
            "estimated_complexity": project["complexity"],
            "request_category": project["structure"],
            "line_of_business": project["lob"],
            "known_business_risks": risks.capitalize(),
            "known_business_dependencies": deps,
            "delivery_pattern": project["pattern"],
            "submitted_date": project["submitted"].isoformat(),
        })
        tables["project_estimate_summary"].append({
            "project_id": pid, "project_name": project["name"],
            "project_size": _size_label(project["actual_hours"]),
            "p50_hours": project["p50"], "p80_hours": project["p80"],
            "actual_hours": project["actual_hours"],
            "duration_weeks": project["planned_weeks"],
            "actual_duration_weeks": project["actual_weeks"],
            "confidence_score": project["confidence"],
            "contingency_percent": project["contingency"],
            "estimate_basis": (
                f"Analog estimate from comparable {project['structure'].replace('-', ' ')} projects "
                f"with {project['contingency']}% integration contingency."),
            "assumptions": (
                f"Core team of {max(2, round(project['avg_fte']))} engineers; dependencies "
                f"({deps.replace(',', ', ')}) are available when planned."),
            "primary_risks": risks.capitalize(),
            "outcome": _outcome(project),
        })
        people: list[tuple[str, str]] = []
        for role, share in sorted(project["roles"].items()):
            fte = round(project["avg_fte"] * share, 1)
            seats = max(1, round(fte))
            tables["project_role_estimates"].append({
                "project_id": pid, "role": role,
                "p50_hours": _round50(project["p50"] * share),
                "p80_hours": _round50(project["p80"] * share),
                "actual_hours": _round50(project["actual_hours"] * share),
                "average_fte": fte, "skill_level": ROLE_SKILL[role],
                "allocation_note": f"{ROLE_TITLE[role]} carries {round(share * 100)}% of planned effort.",
            })
            for _seat in range(seats):
                person = _person(rng)
                people.append((person, role))
                allocation = min(100, max(20, round(100 * fte / seats / 5) * 5))
                conflict = rng.random() < 0.12
                tables["project_resource_assignments"].append({
                    "project_id": pid, "resource_name": person, "role": role,
                    "planned_hours": _round50(project["p50"] * share / seats),
                    "allocation_percent": allocation,
                    "availability_at_start": "partial" if conflict else "available",
                    "capacity_conflict": "yes" if conflict else "no",
                    "constraint_note": "Shared with a concurrent release" if conflict else "",
                })
        for position, dep in enumerate(project["dependencies"]):
            gate, owner = DEPENDENCY_GATES[dep]
            tables["project_approval_dependencies"].append({
                "project_id": pid, "gate": gate, "dependency": dep, "owner": owner,
                "status": "approved", "blocking": "yes" if position == 0 else "no",
                "approved_date": (project["submitted"] + timedelta(days=10 + 6 * position)).isoformat(),
            })
        workitem_number += 1
        tables["devops_workitems_details"].append({
            "workitem_id": f"WI-{workitem_number}", "prjnumber": pid, "title": project["name"],
            "workitemtype": "Epic", "state": "Closed", "scheduling_effort": 0,
            "tags": project["family"],
        })
        for feature in range(rng.randint(3, 5)):
            workitem_number += 1
            tables["devops_workitems_details"].append({
                "workitem_id": f"WI-{workitem_number}", "prjnumber": pid,
                "title": f"{project['name']} - capability {feature + 1}",
                "workitemtype": "Feature", "state": "Closed", "scheduling_effort": 0,
                "tags": project["family"],
            })
            for story in range(rng.randint(2, 3)):
                workitem_number += 1
                tables["devops_workitems_details"].append({
                    "workitem_id": f"WI-{workitem_number}", "prjnumber": pid,
                    "title": f"{project['name']} - story {feature + 1}.{story + 1}",
                    "workitemtype": "User Story", "state": "Closed",
                    "scheduling_effort": rng.choice((2, 3, 5, 8)),
                    "tags": ";".join([project["family"], rng.choice(project["systems"])]),
                })
        for lesson in _lessons(project):
            tables["project_lessons"].append({"project_id": pid, **lesson})
        project["people"] = people
    tables["__transcripts__"] = [
        {"project_id": project["id"], "name": project["name"], "text": _transcript(project)}
        for project in projects
    ]
    return tables


def _transcript(project: dict[str, Any]) -> str:
    people = project["people"] or [("Avery Nguyen", "lead-engineer")]
    lead = next((p for p in people if p[1] == "lead-engineer"), people[0])
    others = [p for p in people if p != lead][:3]
    meeting = project["submitted"] + timedelta(days=7)
    deps = ", ".join(project["dependencies"]).replace("-", " ")
    lines = [
        "# Resource Allocation Meeting (RAM) - New Project Intake",
        f"**Project:** {project['name']}",
        f"**Project ID:** {project['id']}",
        f"**Meeting date:** {meeting.isoformat()}",
        f"**Decision:** Approved to proceed at P50 {project['p50']:,} hours and P80 {project['p80']:,} hours "
        f"with {project['contingency']}% contingency.",
        "",
        "## Participants",
        f"- {lead[0]} - Engineering Lead",
        *[f"- {name} - {ROLE_TITLE[role]}" for name, role in others],
        "- Resource Manager - Capacity Planning",
        "",
        "## 00:00 - Scope Review",
        f"**{lead[0]}:** The request is to {project['problem']}. Systems in scope are "
        f"{', '.join(project['systems']).replace('-', ' ')}.",
        f"**Resource Manager:** We will staff roughly {max(2, round(project['avg_fte']))} engineers "
        f"over {project['planned_weeks']} weeks.",
        "",
        "## 00:09 - Estimate Comparison",
        f"**{lead[0]}:** Comparable projects ran {project['complexity']} complexity. We estimate "
        f"{project['p50']:,} hours at P50 and {project['p80']:,} at P80.",
        f"**Resource Manager:** Contingency of {project['contingency']}% covers integration risk.",
        "",
        "## 00:18 - Risks and Dependencies",
        f"**{lead[0]}:** Gating items are {deps}. A slip in any of them moves the target date.",
        "",
        "## 00:26 - Decision and Action Items",
        f"**Resource Manager:** Approved. Actions: confirm {deps} owners this week, "
        f"lock allocations, and review progress at the first sprint boundary.",
        "",
    ]
    return "\n".join(lines)


def write_dataset(directory: Path | str = DATASET_DIR, seed: int = SEED) -> dict[str, int]:
    root = Path(directory)
    transcripts = root / TRANSCRIPT_DIRNAME
    transcripts.mkdir(parents=True, exist_ok=True)
    tables = generate_dataset(seed)
    counts: dict[str, int] = {}
    for name, rows in tables.items():
        if name == "__transcripts__":
            for row in rows:
                slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in row["name"]).strip("_")
                (transcripts / f"{row['project_id']}_{slug}.md").write_text(
                    row["text"], encoding="utf-8", newline="\n"
                )
            counts["ram_transcripts"] = len(rows)
            continue
        columns = [column.name for column in TABLES[TABLE_PREFIX + name].columns]
        with (root / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        counts[name] = len(rows)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the deterministic delivery_intel-shaped CSV dataset.")
    parser.add_argument("--out", default=None, help=f"Output directory (default: {DATASET_DIR})")
    args = parser.parse_args(argv)
    print(json.dumps(write_dataset(args.out or DATASET_DIR), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
