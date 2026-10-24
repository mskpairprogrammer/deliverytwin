"""Turn exports a person uploads (CSV or XLSX) into the TRACK signals; nothing connects to a source system.

Teams export a view from Azure DevOps, ServiceNow or UKG and upload it. Each file's kind is detected from
its column headers, every row is accepted or rejected with a reason, and the result is a validation
report plus the same five signals the synthetic scenarios produce.
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

from .storage.connection import Connection

MAX_FILES = 5
MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 20_000
MAX_REASONS = 5
SPRINT_DAYS = 14
OPEN_CHANGE_WORDS = ("approval", "cab", "assess", "authorize", "authorise", "review", "pending", "scheduled")
CLOSED_WORDS = ("closed", "resolved", "cancel", "complete", "done", "implemented")

# Header aliases per field, after normalisation (lower case, separators collapsed to single spaces).
FIELDS: dict[str, tuple[str, ...]] = {
    "work_item_type": ("work item type", "workitemtype", "type"),
    "state": ("state", "incident state", "change state", "status", "request status"),
    "points": ("story points", "microsoft.vsts.scheduling.storypoints", "effort", "points"),
    "tags": ("tags", "labels"),
    "unplanned": ("unplanned", "is unplanned", "added after sprint start", "added mid sprint"),
    "number": ("number", "id", "work item id"),
    "priority": ("priority", "severity"),
    "description": ("short description", "title", "description", "summary"),
    "opened": ("opened at", "opened", "created date", "created", "sys created on", "created at"),
    "blocking": ("blocking", "is blocking", "blocks delivery"),
    "approval": ("approval", "approval state"),
    "work_date": ("work date", "date", "punch date", "entry date"),
    "hours": ("hours", "duration hours", "total hours", "paid hours"),
    "work_type": ("work type", "pay code", "labor category", "category", "activity"),
    "person": ("employee", "person", "worker", "employee name", "resource"),
    "start": ("start date", "start", "from"),
    "end": ("end date", "end", "to"),
}

# Header weights that identify the kind of export. A kind needs at least MIN_SCORE to be accepted.
SIGNATURES: dict[str, dict[str, int]] = {
    "ado_work_items": {"work item type": 3, "story points": 3, "iteration path": 2, "unplanned": 2,
                       "added after sprint start": 2, "tags": 1, "microsoft.vsts.scheduling.storypoints": 3},
    "servicenow_incidents": {"priority": 3, "incident state": 3, "urgency": 1, "impact": 1, "assigned to": 1,
                             "number": 1, "opened at": 1, "short description": 1},
    "servicenow_changes": {"approval": 3, "risk": 2, "blocking": 2, "cab required": 2, "planned start date": 2,
                           "change state": 3, "type": 1, "number": 1, "opened at": 1, "short description": 1,
                           "state": 1},
    "ukg_time_entries": {"pay code": 3, "work type": 2, "work date": 3, "labor category": 2, "punch date": 3, "hours": 2},
    "ukg_pto": {"time off type": 3, "request status": 2, "start date": 2, "end date": 2, "status": 1},
}
MIN_SCORE = 4
KIND_LABELS = {
    "ado_work_items": "Azure DevOps work items",
    "servicenow_incidents": "ServiceNow incidents",
    "servicenow_changes": "ServiceNow change requests",
    "ukg_time_entries": "UKG time entries",
    "ukg_pto": "UKG time-off requests",
}
EXPECTED_COLUMNS = {
    "ado_work_items": "Work Item Type, State, Story Points, Tags (or an Unplanned column)",
    "servicenow_incidents": "Number, Priority, State, Opened At",
    "servicenow_changes": "Number, Short Description, State, Approval, Opened At",
    "ukg_time_entries": "Work Date, Pay Code (or Work Type), Hours",
    "ukg_pto": "Start Date, End Date, Status, Hours",
}


def _normalise(header: object) -> str:
    return re.sub(r"[\s_\-]+", " ", str(header or "").strip().lower())


def _pick(row: dict[str, Any], field: str) -> Any:
    for alias in FIELDS[field]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "x"}


def _read_rows(name: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if isinstance(item.get("text"), str) and "content_base64" not in item:
        raw = item["text"].encode("utf-8")
    elif isinstance(item.get("content_base64"), str):
        if len(item["content_base64"]) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError(f"{name} exceeds the 2 MB per-file limit.")
        try:
            raw = base64.b64decode(item["content_base64"], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError(f"{name} is not valid base64-encoded data.") from error
    else:
        raise ValueError(f"{name} must contain text or encoded file data.")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"{name} exceeds the 2 MB per-file limit.")

    if extension == "xlsx":
        from openpyxl import load_workbook

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > 40_000_000:
                    raise ValueError(f"{name} expands beyond the supported workbook limits.")
            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
            try:
                lines = list(workbook.worksheets[0].iter_rows(max_row=MAX_ROWS + 1, max_col=60, values_only=True))
            finally:
                workbook.close()
        except ValueError:
            raise
        except Exception as error:  # openpyxl and lxml raise many parser-specific types
            raise ValueError(f"{name} could not be read as an Excel workbook.") from error
    elif extension in ("csv", "txt", ""):
        try:
            text = raw.decode("utf-8-sig")
            lines = list(csv.reader(io.StringIO(text)))[: MAX_ROWS + 1]
        except UnicodeDecodeError as error:
            raise ValueError(f"{name} must be UTF-8 text.") from error
        except csv.Error as error:
            raise ValueError(f"{name} could not be read as CSV: {error}") from error
    else:
        raise ValueError(f"{name}: only CSV and XLSX exports are supported.")
    if len(lines) < 2:
        raise ValueError(f"{name} has no data rows.")
    headers = [_normalise(value) for value in lines[0]]
    return [
        {header: value for header, value in zip(headers, line) if header}
        for line in lines[1:] if any(value not in (None, "") for value in line)
    ]


def detect_kind(headers: set[str]) -> str | None:
    scores = {
        kind: sum(weight for header, weight in signature.items() if header in headers)
        for kind, signature in SIGNATURES.items()
    }
    kind, score = max(scores.items(), key=lambda pair: (pair[1], pair[0]))
    return kind if score >= MIN_SCORE else None


class _Tally:
    def __init__(self) -> None:
        self.rows = 0
        self.used = 0
        self.reasons: list[str] = []
        self.skipped = 0

    def skip(self, line: int, reason: str) -> None:
        self.skipped += 1
        if len(self.reasons) < MAX_REASONS:
            self.reasons.append(f"Row {line}: {reason}")


def _ado(rows: list[dict[str, Any]], tally: _Tally) -> dict[str, Any]:
    items = points = 0.0
    for line, row in enumerate(rows, start=2):
        state = str(_pick(row, "state") or "").lower()
        if state == "removed":
            tally.skip(line, "removed work item")
            continue
        unplanned = "unplanned" in str(_pick(row, "tags") or "").lower() or _truthy(_pick(row, "unplanned"))
        if not unplanned:
            continue
        value = _number(_pick(row, "points"))
        items += 1
        points += value or 0
        tally.used += 1
    return {"unplanned_work_item_count": int(items), "unplanned_story_points": points}


def _incidents(rows: list[dict[str, Any]], tally: _Tally) -> dict[str, Any]:
    count = 0
    for line, row in enumerate(rows, start=2):
        priority = str(_pick(row, "priority") or "").strip().lower()
        if not priority:
            tally.skip(line, "missing priority")
            continue
        if not (priority[:1] == "1" or "critical" in priority or priority in ("p1", "sev1")):
            continue
        state = str(_pick(row, "state") or "").lower()
        if any(word in state for word in CLOSED_WORDS):
            continue
        count += 1
        tally.used += 1
    return {"active_priority_1_incidents": count}


def _changes(rows: list[dict[str, Any]], tally: _Tally, as_of: date) -> dict[str, Any]:
    blocking = []
    for line, row in enumerate(rows, start=2):
        state = str(_pick(row, "state") or "").lower()
        if any(word in state for word in CLOSED_WORDS):
            continue
        flag = _pick(row, "blocking")
        text = f"{state} {str(_pick(row, 'approval') or '').lower()}"
        is_blocking = _truthy(flag) if flag is not None else any(word in text for word in OPEN_CHANGE_WORDS)
        if not is_blocking:
            continue
        opened = _date(_pick(row, "opened"))
        if opened is None:
            tally.skip(line, "missing or unreadable opened date")
            continue
        blocking.append({
            "external_id": str(_pick(row, "number") or f"row-{line}"),
            "description": str(_pick(row, "description") or "Change request")[:160],
            "age_days": max((as_of - opened).days, 0),
        })
        tally.used += 1
    return {"blocking_change_requests": blocking,
            "oldest_blocking_change_age_days": max((item["age_days"] for item in blocking), default=0)}


def _is_lead(row: dict[str, Any]) -> bool:
    """The signals describe the lead engineer; when a person column exists, other people's rows are left out."""
    person = _pick(row, "person")
    return person is None or "lead" in str(person).lower()


def _approved(value: Any) -> bool:
    state = str(value or "").lower()
    return bool(re.search(r"\bapproved\b", state)) and not re.search(r"\b(not|un)\s*approved\b", state)


def _time_entries(rows: list[dict[str, Any]], tally: _Tally, as_of: date) -> dict[str, Any]:
    total = 0.0
    for line, row in enumerate(rows, start=2):
        kind = str(_pick(row, "work_type") or "").lower()
        if "support" not in kind or not _is_lead(row):
            continue
        worked, hours = _date(_pick(row, "work_date")), _number(_pick(row, "hours"))
        if worked is None or hours is None:
            tally.skip(line, "missing or unreadable date or hours")
            continue
        if as_of - timedelta(days=6) <= worked <= as_of:
            total += hours
            tally.used += 1
    return {"lead_production_support_hours_per_week": total}


def _pto(rows: list[dict[str, Any]], tally: _Tally, as_of: date) -> dict[str, Any]:
    total = 0.0
    window_start = as_of - timedelta(days=SPRINT_DAYS - 1)
    for line, row in enumerate(rows, start=2):
        if not _approved(_pick(row, "state")) or not _is_lead(row):
            continue
        start, end = _date(_pick(row, "start")), _date(_pick(row, "end"))
        if start is None or end is None or end < start:
            tally.skip(line, "missing or unreadable start or end date")
            continue
        if start > as_of or end < window_start:
            continue
        hours = _number(_pick(row, "hours"))
        total += hours if hours is not None else 8 * ((end - start).days + 1)
        tally.used += 1
    return {"lead_approved_pto_hours": total}


def parse_exports(files: object, as_of: date | None = None) -> dict[str, Any]:
    """Validate uploaded exports and compute the TRACK signals; raises ValueError for unusable input."""
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError(f"Upload between 1 and {MAX_FILES} export files.")
    as_of = as_of or date.today()
    signals: dict[str, Any] = {
        "unplanned_work_item_count": 0, "unplanned_story_points": 0,
        "lead_production_support_hours_per_week": 0, "lead_approved_pto_hours": 0,
        "active_priority_1_incidents": 0, "blocking_change_requests": [], "oldest_blocking_change_age_days": 0,
    }
    report: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Each file needs a name.")
        name = item["name"][:120]
        try:
            rows = _read_rows(name, item)
        except ValueError as error:
            report.append({"name": name, "kind": None, "accepted": False, "message": str(error)})
            continue
        kind = detect_kind(set().union(*(row.keys() for row in rows)))
        if kind is None:
            report.append({
                "name": name, "kind": None, "accepted": False, "rows": len(rows),
                "message": "The columns were not recognised. Supported exports: "
                           + "; ".join(f"{KIND_LABELS[k]} ({v})" for k, v in EXPECTED_COLUMNS.items()) + ".",
            })
            continue
        if kind in seen:
            report.append({"name": name, "kind": kind, "accepted": False, "rows": len(rows),
                           "message": f"A {KIND_LABELS[kind]} file was already provided; only one per kind is used."})
            continue
        seen.add(kind)
        tally = _Tally()
        tally.rows = len(rows)
        parsed = {
            "ado_work_items": lambda: _ado(rows, tally),
            "servicenow_incidents": lambda: _incidents(rows, tally),
            "servicenow_changes": lambda: _changes(rows, tally, as_of),
            "ukg_time_entries": lambda: _time_entries(rows, tally, as_of),
            "ukg_pto": lambda: _pto(rows, tally, as_of),
        }[kind]()
        signals.update(parsed)
        report.append({
            "name": name, "kind": kind, "kind_label": KIND_LABELS[kind], "accepted": True,
            "rows": tally.rows, "rows_used": tally.used, "rows_skipped": tally.skipped, "skipped_reasons": tally.reasons,
        })
    if not seen:
        raise ValueError(
            "No file was recognised. " + (report[0]["message"] if report else "")
        )
    missing = [KIND_LABELS[k] for k in SIGNATURES if k not in seen]
    return {
        "as_of": as_of.isoformat(), "files": report, "signals": signals,
        "not_provided": missing,
        "note": "Signals not covered by an uploaded file are counted as zero.",
    }


def save_import(connection: Connection, parsed: dict[str, Any]) -> None:
    with connection:
        connection.upsert_rows(
            "dt_simulation_settings", ("key", "value"), ("key",),
            [("import_signals", json.dumps(parsed))], ("value",),
        )


def load_import(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute("SELECT value FROM dt_simulation_settings WHERE key = 'import_signals'").fetchone()
    return json.loads(row["value"]) if row else None
