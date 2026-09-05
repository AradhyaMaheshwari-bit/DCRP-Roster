"""
services.approval_service
-------------------------
State machine for `role_request` approvals.

State diagram:
    PENDING ──> APPROVED ──> PROCESSING ──> COMPLETED  (success)
       │           │              │
       │           │              └──> FAILED       (any step errored)
       │           └──> REJECTED                     (HR said no)
       │           └──> FAILED                       (config error, etc.)
       └──> REJECTED                                 (HR rejected before approve)
       └──> FAILED                                   (config missing, etc.)

Idempotency: every transition is gated by a precondition on the current
state (in the spreadsheet). Two simultaneous approvals of the same
request_id can't both win; the second one will see the new state and
short-circuit. An in-process threading.Lock per request_id provides
best-effort single-process serialization on top of the sheet precondition.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from sheets import client as sheets_client
from sheets import repository
from sheets.exceptions import SheetColumnMissingError, SheetTabMissingError, SheetsError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

PENDING = "PENDING"
APPROVED = "APPROVED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
FAILED = "FAILED"

ALL_STATES: tuple[str, ...] = (PENDING, APPROVED, PROCESSING, COMPLETED, REJECTED, FAILED)
TERMINAL_STATES: frozenset[str] = frozenset({COMPLETED, REJECTED, FAILED})

# Static transition table — the state machine enforces this DAG.
# A transition is legal iff (current, to) is a key in this map.
# The "from_states" parameter on transition() is also checked, so callers
# can be defensive about which states they accept (e.g. reject may
# proceed from PENDING or APPROVED, but not from PROCESSING).
LEGAL_NEXT: dict[str, frozenset[str]] = {
    PENDING:    frozenset({APPROVED, REJECTED, FAILED}),
    APPROVED:   frozenset({PROCESSING, REJECTED, FAILED}),
    PROCESSING: frozenset({COMPLETED, FAILED}),
    COMPLETED:  frozenset(),  # terminal
    REJECTED:   frozenset(),  # terminal
    FAILED:     frozenset(),  # terminal
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ApprovalError(Exception):
    """Base class for approval-state errors."""


class RequestNotFoundError(ApprovalError):
    pass


class InvalidStateTransitionError(ApprovalError):
    """Raised when a transition is illegal (wrong from-state or terminal)."""


# ---------------------------------------------------------------------------
# Record / tab schema
# ---------------------------------------------------------------------------

PENDING_TAB = "PENDING REQUESTS"

PENDING_HEADERS: list[str] = [
    "Request ID",
    "Requester",
    "Target ID",
    "Target Username",
    "Department",
    "Rank",
    "Approved By",
    "Status",
    "Created At",
    "Updated At",
    "Approver",
    "Completion Message ID",
    "Result Metadata",
]


@dataclass
class RoleRequestRecord:
    request_id: str
    requester_id: int
    target_discord_id: int
    target_username: str
    department: str
    rank: str
    approved_by_discord_id: int
    status: str = PENDING
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    approver_id: int = 0
    completion_message_id: str = ""
    result_metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> list[str]:
        return [
            self.request_id,
            str(self.requester_id),
            str(self.target_discord_id),
            self.target_username,
            self.department,
            self.rank,
            str(self.approved_by_discord_id),
            self.status,
            self.created_at,
            self.updated_at,
            str(self.approver_id or ""),
            self.completion_message_id,
            json.dumps(self.result_metadata, ensure_ascii=False, sort_keys=True),
        ]

    @classmethod
    def from_row(cls, row_index: int, row: list[str]) -> "RoleRequestRecord":
        def _cell(i: int) -> str:
            return str(row[i]) if i < len(row) and row[i] is not None else ""

        meta_raw = _cell(12)
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except json.JSONDecodeError:
            meta = {"_raw_metadata": meta_raw}
        return cls(
            request_id=_cell(0),
            requester_id=int(_cell(1) or 0),
            target_discord_id=int(_cell(2) or 0),
            target_username=_cell(3),
            department=_cell(4),
            rank=_cell(5),
            approved_by_discord_id=int(_cell(6) or 0),
            status=_cell(7) or PENDING,
            created_at=_cell(8),
            updated_at=_cell(9),
            approver_id=int(_cell(10) or 0),
            completion_message_id=_cell(11),
            result_metadata=meta,
        )


# ---------------------------------------------------------------------------
# Lock registry (in-process best-effort concurrency)
# ---------------------------------------------------------------------------

_Locks: dict[str, threading.Lock] = {}
_Locks_Meta = threading.Lock()


def _lock_for(request_id: str) -> threading.Lock:
    with _Locks_Meta:
        lock = _Locks.get(request_id)
        if lock is None:
            lock = threading.Lock()
            _Locks[request_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Tab helpers
# ---------------------------------------------------------------------------

def _open_pending_ws():
    spreadsheet = sheets_client.open_target_spreadsheet()
    ws = repository.ensure_tab(spreadsheet, PENDING_TAB)
    headers = repository.read_header_row(ws)
    if not headers:
        repository.write_headers(ws, PENDING_HEADERS)
    elif headers != PENDING_HEADERS:
        try:
            _validate_headers(headers)
        except SheetColumnMissingError as exc:
            logger.error("PENDING REQUESTS header mismatch: %s", exc)
            raise
    return ws


def _validate_headers(headers: list[str]) -> None:
    norm = {h.strip().casefold() for h in headers}
    for required in PENDING_HEADERS:
        if required.casefold() not in norm:
            raise SheetColumnMissingError(PENDING_TAB, required)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_request(
    *,
    requester_id: int,
    target_discord_id: int,
    target_username: str,
    department: str,
    rank: str,
    approved_by_discord_id: int,
) -> RoleRequestRecord:
    """Create a new PENDING request row. Returns the new record (with id)."""
    record = RoleRequestRecord(
        request_id=uuid.uuid4().hex[:12],
        requester_id=requester_id,
        target_discord_id=target_discord_id,
        target_username=target_username,
        department=department,
        rank=rank,
        approved_by_discord_id=approved_by_discord_id,
    )
    ws = _open_pending_ws()
    repository.append_row(ws, record.to_row())
    logger.info(
        "approval: created request_id=%s target=%s dept=%s rank=%s",
        record.request_id, target_discord_id, department, rank,
    )
    return record


def get_request(request_id: str) -> Optional[RoleRequestRecord]:
    """Fetch a request by id. Returns None if not found."""
    ws = _open_pending_ws()
    loc = repository.find_row_by_column(ws, "Request ID", request_id)
    if not loc:
        return None
    return RoleRequestRecord.from_row(loc.row_index, loc.row_values)


def transition(
    request_id: str,
    *,
    from_states: set[str] | list[str] | tuple[str, ...],
    to_state: str,
    updates: Optional[dict[str, Any]] = None,
) -> RoleRequestRecord:
    """Atomically move a request from any of `from_states` to `to_state`.

    Steps:
      1. Acquire in-process lock on request_id.
      2. Read current row by Request ID.
      3. If current status not in from_states → raise InvalidStateTransitionError.
      4. If current status is terminal → raise InvalidStateTransitionError.
      5. Apply updates, set Status = to_state, set Updated At = now.
      6. Write with a precondition on the current status (defends against
         the in-process lock being bypassed by another worker).
      7. Return the new record.

    `updates` keys can be any of:
      - approver_id (int)
      - completion_message_id (str)
      - result_metadata (dict — merged into existing metadata)
    """
    allowed = set(from_states)
    if to_state not in ALL_STATES:
        raise ValueError(f"Unknown target state: {to_state!r}")

    lock = _lock_for(request_id)
    with lock:
        ws = _open_pending_ws()
        loc = repository.find_row_by_column(ws, "Request ID", request_id)
        if not loc:
            raise RequestNotFoundError(f"Request {request_id} not found")

        current = RoleRequestRecord.from_row(loc.row_index, loc.row_values)
        if current.status in TERMINAL_STATES:
            raise InvalidStateTransitionError(
                f"Request {request_id} already terminal ({current.status}); "
                f"cannot transition to {to_state}"
            )
        if current.status not in allowed:
            raise InvalidStateTransitionError(
                f"Request {request_id} is in state {current.status!r}; "
                f"expected one of {sorted(allowed)}"
            )
        if to_state not in LEGAL_NEXT.get(current.status, frozenset()):
            raise InvalidStateTransitionError(
                f"Illegal transition for {request_id}: "
                f"{current.status!r} -> {to_state!r}. "
                f"Allowed from {current.status!r}: "
                f"{sorted(LEGAL_NEXT.get(current.status, frozenset()))}"
            )

        # Build the new row.
        new_record = RoleRequestRecord(
            request_id=current.request_id,
            requester_id=current.requester_id,
            target_discord_id=current.target_discord_id,
            target_username=current.target_username,
            department=current.department,
            rank=current.rank,
            approved_by_discord_id=current.approved_by_discord_id,
            status=to_state,
            created_at=current.created_at,
            updated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            approver_id=current.approver_id,
            completion_message_id=current.completion_message_id,
            result_metadata=dict(current.result_metadata),
        )
        if updates:
            if "approver_id" in updates:
                new_record.approver_id = int(updates["approver_id"])
            if "completion_message_id" in updates:
                new_record.completion_message_id = str(updates["completion_message_id"])
            if "result_metadata" in updates and isinstance(updates["result_metadata"], dict):
                new_record.result_metadata.update(updates["result_metadata"])

        # Write with precondition on the old status.
        expected_current = current.to_row()
        try:
            repository.update_row(
                ws,
                loc.row_index,
                new_record.to_row(),
                expected_current=expected_current,
            )
        except Exception as exc:
            logger.error("transition: write failed for %s: %s", request_id, exc)
            raise

        logger.info(
            "approval: request_id=%s %s -> %s",
            request_id, current.status, to_state,
        )
        return new_record


# Convenience wrappers ----------------------------------------------------

def mark_processing(request_id: str, **kw) -> RoleRequestRecord:
    return transition(request_id, from_states={APPROVED}, to_state=PROCESSING, updates=kw)


def mark_completed(request_id: str, **kw) -> RoleRequestRecord:
    return transition(request_id, from_states={PROCESSING}, to_state=COMPLETED, updates=kw)


def mark_rejected(request_id: str, **kw) -> RoleRequestRecord:
    return transition(request_id, from_states={PENDING, APPROVED}, to_state=REJECTED, updates=kw)


def mark_failed(request_id: str, **kw) -> RoleRequestRecord:
    return transition(
        request_id,
        from_states={PENDING, APPROVED, PROCESSING},
        to_state=FAILED,
        updates=kw,
    )
