"""Single source of truth for the portfolio tables, rendered per SQL dialect."""
from __future__ import annotations

import re
from dataclasses import dataclass

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

TYPE_MAP = {
    "sqlite": {"string": "TEXT", "int": "INTEGER", "double": "REAL", "date": "TEXT"},
    "duckdb": {"string": "VARCHAR", "int": "BIGINT", "double": "DOUBLE", "date": "VARCHAR"},
    "databricks": {"string": "STRING", "int": "BIGINT", "double": "DOUBLE", "date": "DATE"},
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    comment: str = ""


def _cols(spec: str) -> tuple[Column, ...]:
    return tuple(Column(*item.split(":")) for item in spec.split())


TABLES: dict[str, Table] = {
    table.name: table
    for table in (
        Table(
            "project_intakes",
            _cols(
                "project_id:string project_name:string problem_statement_and_desired_outcome:string "
                "overall_scope:string affected_systems:string business_strategy:string "
                "estimated_complexity:string request_category:string line_of_business:string "
                "known_business_risks:string known_business_dependencies:string "
                "delivery_pattern:string submitted_date:date"
            ),
            ("project_id",),
            "Intake form fields; delivery_pattern is an extension used for 6D matching.",
        ),
        Table(
            "project_estimate_summary",
            _cols(
                "project_id:string project_name:string project_size:string p50_hours:int "
                "p80_hours:int actual_hours:int duration_weeks:int actual_duration_weeks:int "
                "confidence_score:double contingency_percent:int estimate_basis:string "
                "assumptions:string primary_risks:string outcome:string"
            ),
            ("project_id",),
            "P50/P80 estimate (including contingency) versus actuals; actual_duration_weeks "
            "and outcome are extensions.",
        ),
        Table(
            "project_role_estimates",
            _cols(
                "project_id:string role:string p50_hours:int p80_hours:int actual_hours:int "
                "average_fte:double skill_level:string allocation_note:string"
            ),
            ("project_id", "role"),
            "Per-role effort breakdown.",
        ),
        Table(
            "project_resource_assignments",
            _cols(
                "project_id:string resource_name:string role:string planned_hours:int "
                "allocation_percent:int availability_at_start:string capacity_conflict:string "
                "constraint_note:string"
            ),
            ("project_id", "resource_name", "role"),
            "Named resource allocations.",
        ),
        Table(
            "project_approval_dependencies",
            _cols(
                "project_id:string gate:string dependency:string owner:string status:string "
                "blocking:string approved_date:date"
            ),
            ("project_id", "gate"),
            "Approval gates; dependency is an extension that carries the 6D vocabulary term.",
        ),
        Table(
            "devops_workitems_details",
            _cols(
                "workitem_id:string prjnumber:string title:string workitemtype:string "
                "state:string scheduling_effort:int tags:string"
            ),
            ("workitem_id",),
            "Epics, features, and stories joined on prjnumber.",
        ),
        Table(
            "project_lessons",
            _cols("project_id:string category:string lesson:string suggested_option:string"),
            ("project_id", "category"),
            "Extension: lesson per clarification category and the option it supports.",
        ),
    )
}


def check_identifier(name: str) -> str:
    if not IDENTIFIER.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def render_create_table(table: Table, dialect: str, qualified_name: str | None = None) -> str:
    types = TYPE_MAP[dialect]
    target = qualified_name or table.name
    key_columns = set(table.key)
    lines = []
    for column in table.columns:
        not_null = " NOT NULL" if column.name in key_columns else ""
        lines.append(f"  {check_identifier(column.name)} {types[column.type]}{not_null}")
    lines.append(f"  PRIMARY KEY ({', '.join(table.key)})")
    suffix = ""
    if dialect == "databricks":
        suffix = f"\nUSING DELTA\nCOMMENT '{table.comment}'" if table.comment else "\nUSING DELTA"
    return f"CREATE TABLE IF NOT EXISTS {target} (\n" + ",\n".join(lines) + f"\n){suffix}"


def render_ddl(dialect: str, catalog: str | None = None, schema: str | None = None) -> str:
    prefix = ".".join(check_identifier(part) for part in (catalog, schema) if part)
    statements = []
    for table in TABLES.values():
        target = f"{prefix}.{table.name}" if prefix else table.name
        statements.append(render_create_table(table, dialect, target) + ";")
    return "\n\n".join(statements) + "\n"
