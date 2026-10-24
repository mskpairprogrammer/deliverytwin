"""Who approved what: the approver of each COMMIT baseline and applied INTERVENE plan.

On Azure App Service, Microsoft Entra ID sign-in (App Service Authentication) puts the signed-in user in the
``X-MS-CLIENT-PRINCIPAL-NAME`` request header, and App Service drops that header from incoming requests, so the
app can trust it there. Anywhere else the header could be sent by anyone, so it is ignored and approvals are
recorded as the local user.
"""
from __future__ import annotations

from typing import Any, Mapping

from .integrations import azure_app_service
from .storage import ApprovalRepository
from .storage.connection import Connection

PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"
LOCAL_APPROVER = {"name": "Local user (no sign-in)", "source": "local"}
MAX_NAME_LENGTH = 256


def approver_from_headers(headers: Mapping[str, str] | None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The signed-in Entra ID user on App Service, else the local user."""
    name = (headers.get(PRINCIPAL_HEADER) or "").strip() if headers is not None else ""
    if azure_app_service(env) and name:
        return {"name": name[:MAX_NAME_LENGTH], "source": "entra_id"}
    return dict(LOCAL_APPROVER)


def record_approval(connection: Connection, stage: str, record_id: object, approver: dict[str, str] | None,
                    summary: dict[str, Any]) -> dict[str, Any]:
    return ApprovalRepository(connection.app_database()).record(stage, record_id, approver or LOCAL_APPROVER, summary)


def with_approvers(connection: Connection, stage: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``approved_by`` (name and source, or None when recorded before approvals were logged) to each record."""
    approvals = ApprovalRepository(connection.app_database()).by_record(stage)
    result = []
    for record in records:
        approval = approvals.get(str(record["id"]))
        approved_by = {"name": approval["approver"], "source": approval["approver_source"]} if approval else None
        result.append({**record, "approved_by": approved_by})
    return result
