"""
services.audit_service
----------------------
Append-only audit log of every personnel change.

Writes to the `AUDIT_LOG` tab in the Target sheet. Lazy-creates the tab
with the documented schema if it doesn't exist. Never updates or deletes
rows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from sheets import client as sheets_client
from sheets import repository
from sheets.exceptions import SheetColumnMissingError, SheetsError

logger = logging.getLogger(__name__)

AUDIT_TAB = "AUDIT_LOG"
AUDIT_HEADERS: list[str] = [
    "Timestamp",
    "Request ID",
    "Action",
    "Target Discord ID",
    "Target Username",
    "Prev Rank",
    "New Rank",
    "Prev Department",
    "New Department",
    "Requester ID",
    "Approver ID",
    "Discord Result",
    "Sheets Result",
    "Overall Result",
    "Failure Reason",
    "Metadata",
]


@dataclass
class AuditEntry:
    request_id: str
    action: str  # e.g. "ROLE_REQUEST_APPROVED", "ROLE_REQUEST_REJECTED", "ROLE_REQUEST_FAILED"
    target_discord_id: int
    target_username: str
    prev_rank: str = ""
    new_rank: str = ""
    prev_department: str = ""
    new_department: str = ""
    requester_id: int = 0
    approver_id: int = 0
    discord_result: str = ""
    sheets_result: str = ""
    overall_result: str = ""  # "SUCCESS" | "PARTIAL" | "FAILURE"
    failure_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")


def append_audit(entry: AuditEntry) -> None:
    """Append one row to AUDIT_LOG. Creates the tab with the right schema if missing."""
    try:
        spreadsheet = sheets_client.open_target_spreadsheet()
    except SheetsError as exc:
        logger.error("Audit log: cannot open Target sheet (%s). Entry dropped: %s", exc, entry)
        return

    ws = repository.ensure_tab(spreadsheet, AUDIT_TAB)
    try:
        headers = repository.read_header_row(ws)
    except Exception:  # noqa: BLE001
        headers = []
    if not headers or headers != AUDIT_HEADERS:
        # If the tab is empty (no headers yet), seed it. If it has different
        # headers, refuse to clobber them — surface an error so the operator
        # can investigate.
        if not headers:
            repository.write_headers(ws, AUDIT_HEADERS)
        else:
            try:
                _validate_audit_headers(headers)
            except SheetColumnMissingError as exc:
                logger.error("Audit log header mismatch: %s", exc)
                return

    row = [
        entry.timestamp,
        entry.request_id,
        entry.action,
        str(entry.target_discord_id),
        entry.target_username,
        entry.prev_rank,
        entry.new_rank,
        entry.prev_department,
        entry.new_department,
        str(entry.requester_id or ""),
        str(entry.approver_id or ""),
        entry.discord_result,
        entry.sheets_result,
        entry.overall_result,
        entry.failure_reason,
        json.dumps(entry.metadata, ensure_ascii=False, sort_keys=True),
    ]
    try:
        repository.append_row(ws, row)
        logger.info(
            "audit: action=%s target=%s overall=%s",
            entry.action, entry.target_discord_id, entry.overall_result,
        )
    except SheetsError as exc:
        logger.error("Audit log write failed (%s). Entry dropped: %s", exc, entry)


def _validate_audit_headers(headers: list[str]) -> None:
    """Raise if the tab's headers don't cover all required columns."""
    norm = {h.strip().casefold() for h in headers}
    for required in AUDIT_HEADERS:
        if required.casefold() not in norm:
            raise SheetColumnMissingError(AUDIT_TAB, required)
