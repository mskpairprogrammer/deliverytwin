"""Single source of truth for the portfolio tables, rendered per SQL dialect."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

TYPE_MAP = {
    "duckdb": {"string": "VARCHAR", "int": "BIGINT", "double": "DOUBLE", "date": "VARCHAR", "timestamp": "VARCHAR"},
    "databricks": {"string": "STRING", "int": "BIGINT", "double": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP"},
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str


# Every Delivery Twin table name starts with dt_, so the tables group together and never collide with
# other tables in a shared catalog or schema. Column names are not prefixed.
TABLE_PREFIX = "dt_"


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    comment: str = ""

    @property
    def dataset(self) -> str:
        """Name of the checked-in CSV (and delivery_intel source table) the rows load from: the unprefixed name."""
        return self.name.removeprefix(TABLE_PREFIX)


def _cols(spec: str) -> tuple[Column, ...]:
    return tuple(Column(*item.split(":")) for item in spec.split())


TABLES: dict[str, Table] = {
    table.name: table
    for table in (
        Table(
            "dt_project_intakes",
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
            "dt_project_estimate_summary",
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
            "dt_project_role_estimates",
            _cols(
                "project_id:string role:string p50_hours:int p80_hours:int actual_hours:int "
                "average_fte:double skill_level:string allocation_note:string"
            ),
            ("project_id", "role"),
            "Per-role effort breakdown.",
        ),
        Table(
            "dt_project_resource_assignments",
            _cols(
                "project_id:string resource_name:string role:string planned_hours:int "
                "allocation_percent:int availability_at_start:string capacity_conflict:string "
                "constraint_note:string"
            ),
            ("project_id", "resource_name", "role"),
            "Named resource allocations.",
        ),
        Table(
            "dt_project_approval_dependencies",
            _cols(
                "project_id:string gate:string dependency:string owner:string status:string "
                "blocking:string approved_date:date"
            ),
            ("project_id", "gate"),
            "Approval gates; dependency is an extension that carries the 6D vocabulary term.",
        ),
        Table(
            "dt_devops_workitems_details",
            _cols(
                "workitem_id:string prjnumber:string title:string workitemtype:string "
                "state:string scheduling_effort:int tags:string"
            ),
            ("workitem_id",),
            "Epics, features, and stories joined on prjnumber.",
        ),
        Table(
            "dt_project_lessons",
            _cols("project_id:string category:string lesson:string suggested_option:string"),
            ("project_id", "category"),
            "Extension: lesson per clarification category and the option it supports.",
        ),
    )
}


# Tables the application itself writes (not loaded from the CSV dataset). All four go through the portable
# repositories in app_repository.py, so they run unchanged on DuckDB and Databricks (Delta).
APP_TABLES: dict[str, Table] = {
    table.name: table
    for table in (
        Table(
            "dt_commitment_baselines",
            _cols(
                "baseline_id:string request_text:string answers_json:string fingerprint_json:string "
                "p50_hours:double p80_hours:double integration_buffer_pct:double "
                "allocated_fte:double capacity_fte:double target_date:date "
                "confidence_pct:double approved_at:timestamp"
            ),
            ("baseline_id",),
            "Human-approved COMMIT baselines.",
        ),
        Table(
            "dt_intake_sessions",
            _cols("session_id:string started_at:timestamp first_rom_at:timestamp approved_at:timestamp"),
            ("session_id",),
            "Anonymous intake timing; no request text or document content.",
        ),
        Table(
            "dt_recovery_scenarios",
            _cols(
                "scenario_id:int initiative_id:string recommended_option:string allocated_fte:double "
                "scope_defer_pct:int target_date:date incremental_cost_k:double "
                "confidence_pct:int saved_at:timestamp"
            ),
            ("scenario_id",),
            "INTERVENE scenarios saved after a what-if preview.",
        ),
        Table(
            "dt_recovery_target_updates",
            _cols("scenario_id:int source_key:string payload_json:string"),
            ("scenario_id", "source_key"),
            "Simulated per-system update records of a saved recovery scenario.",
        ),
        Table(
            "dt_track_snapshots",
            _cols(
                "snapshot_id:string initiative_id:string sprint_number:int as_of:date "
                "on_time_pct:double projected_slip_days:int unplanned_story_points:int "
                "support_hours_per_week:double captured_at:timestamp"
            ),
            ("snapshot_id",),
            "One TRACK outlook per sprint, kept for trend history.",
        ),
    )
}
ALL_TABLES: dict[str, Table] = {**TABLES, **APP_TABLES}


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
    for table in ALL_TABLES.values():
        target = f"{prefix}.{table.name}" if prefix else table.name
        statements.append(render_create_table(table, dialect, target) + ";")
    if dialect == "databricks":
        source = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
        statements.extend(
            f"{statement};" for statement in translate_schema(source, dialect, catalog=catalog, schema=schema)
        )
    return "\n\n".join(statements) + "\n"


_AUTOINCREMENT = re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT", re.I)
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)", re.I)
_REAL = re.compile(r"\bREAL\b", re.I)
_INTEGER = re.compile(r"\bINTEGER\b", re.I)
_TEXT = re.compile(r"\bTEXT\b", re.I)


def _remove_parenthesized(sql: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    for index in range(start, len(sql)):
        char = sql[index]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(sql)


def _strip_databricks_constraints(statement: str) -> str:
    for match in list(re.finditer(r"\bCHECK\s*\(", statement, re.I))[::-1]:
        start = statement.find("(", match.start())
        statement = statement[:match.start()] + statement[_remove_parenthesized(statement, start):]
    statement = re.sub(r"\s+REFERENCES\s+\w+\s*\([^)]*\)", "", statement, flags=re.I)
    statement = re.sub(r"\s+UNIQUE\b", "", statement, flags=re.I)
    statement = re.sub(r"[ \t]+(?=[,)])", "", statement)
    return re.sub(r"[ \t]+\n", "\n", statement)


def translate_schema(
    script: str, dialect: str, *, catalog: str | None = None, schema: str | None = None,
) -> list[str]:
    """Split the app schema and translate its SQLite types/constraints for the target engine."""
    statements: list[str] = []
    prefix = ".".join(check_identifier(part) for part in (catalog, schema) if part)
    for raw in script.split(";"):
        statement = raw.strip()
        if not statement:
            continue
        upper = statement.upper()
        if upper.startswith("PRAGMA") or (dialect == "databricks" and upper.startswith("CREATE INDEX")):
            continue
        if dialect == "duckdb":
            created = _CREATE_TABLE.search(statement)
            if created and _AUTOINCREMENT.search(statement):
                name = created.group(1)
                statements.append(f"CREATE SEQUENCE IF NOT EXISTS {name}_seq START 1")
                statement = _AUTOINCREMENT.sub(f"BIGINT PRIMARY KEY DEFAULT nextval('{name}_seq')", statement)
            statement = _REAL.sub("DOUBLE", statement)
        elif dialect == "databricks":
            statement = _AUTOINCREMENT.sub("BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", statement)
            statement = _strip_databricks_constraints(statement)
            statement = _REAL.sub("DOUBLE", statement)
            statement = _INTEGER.sub("BIGINT", statement)
            statement = _TEXT.sub("STRING", statement)
            created = _CREATE_TABLE.search(statement)
            if created and prefix:
                statement = (
                    statement[:created.start(1)] + f"{prefix}.{created.group(1)}"
                    + statement[created.end(1):]
                )
            if _CREATE_TABLE.search(statement):
                statement += "\nUSING DELTA"
                if re.search(r"\bDEFAULT\b", statement, re.I):
                    statement += "\nTBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')"
        else:
            raise ValueError(f"Unsupported schema dialect: {dialect!r}")
        statements.append(statement)
    return statements
