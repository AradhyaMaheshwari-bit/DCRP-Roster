"""
tests.test_role_request_state_machine
-------------------------------------
Offline tests for the approval state machine.

These tests use an in-memory fake of the `sheets.repository` API so they
can run without Google credentials. The fakes simulate the sheet's
header row + a list of row dicts, and the production code goes through
the `sheets` module's public functions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure env vars are set before any config import.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("HR_ROLE_ID", "1")
os.environ.setdefault("GOOGLE_CREDENTIALS_PATH", "./secrets/x.json")
os.environ.setdefault("TARGET_SPREADSHEET_ID", "target")
os.environ.setdefault("NOVA_SPREADSHEET_ID", "nova")

import pytest

# Make sure the project root is importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fake sheet backend
# ---------------------------------------------------------------------------

class FakeWorksheet:
    def __init__(self, title: str, headers: list[str]):
        self.title = title
        self._headers = list(headers)
        self._rows: list[list[str]] = []  # data rows only (row 2+)
        self.id = hash(title) & 0xFFFF

    # Read API used by the repository layer
    def row_values(self, row_index: int, value_render_option=None):
        # 1-based; row 1 = headers
        if row_index == 1:
            return list(self._headers)
        idx = row_index - 2
        if idx < 0 or idx >= len(self._rows):
            return []
        return list(self._rows[idx])

    def get(self, values_range=None, value_render_option=None):
        # We assume a single rectangular range starting at A2.
        return [list(r) for r in self._rows]

    # Write API
    def update(self, values, range_name=None, value_input_option=None):
        # Only support "A1:<col>1" (header) and "A<n>:<col><n>" (full row) for our tests.
        if range_name and range_name.endswith("1") and ":" in range_name:
            self._headers = list(values[0])
            return {"updatedRange": range_name}
        # full row write
        if range_name and ":" in range_name:
            a, b = range_name.split(":")
            row_num = int("".join(ch for ch in a if ch.isdigit()))
            idx = row_num - 2
            if idx < 0:
                # insert blank rows up to idx
                self._rows.extend([[]] * (-idx))
                idx = 0
            self._rows[idx] = list(values[0])
            return {"updatedRange": range_name}
        return {}

    def append_row(self, values, value_input_option=None, insert_data_option=None):
        self._rows.append(list(values))
        return len(self._rows) + 1

    def append_rows(self, values, value_input_option=None, insert_data_option=None):
        for v in values:
            self._rows.append(list(v))
        return len(self._rows) + 1


class FakeSpreadsheet:
    def __init__(self):
        self.worksheets: dict[str, FakeWorksheet] = {}
        self.batch_updates: list[dict[str, Any]] = []

    def worksheet(self, title: str) -> FakeWorksheet:
        if title not in self.worksheets:
            # Use the gspread-style exception so ensure_tab() can catch it.
            import gspread.exceptions
            raise gspread.exceptions.WorksheetNotFound(f"Worksheet {title!r} not found")
        return self.worksheets[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        ws = FakeWorksheet(title, headers=[])
        self.worksheets[title] = ws
        return ws

    def batch_update(self, body):
        self.batch_updates.append(body)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_spreadsheet(monkeypatch):
    """Install a fake `open_target_spreadsheet()` and patch repository helpers."""
    import services.approval_service as svc

    fs = FakeSpreadsheet()
    fs.worksheets["PENDING REQUESTS"] = FakeWorksheet("PENDING REQUESTS", list(svc.PENDING_HEADERS))

    monkeypatch.setattr(
        "sheets.client.open_target_spreadsheet",
        lambda: fs,
    )
    return fs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_request_appends_row(fake_spreadsheet):
    from services.approval_service import create_request, get_request

    rec = create_request(
        requester_id=100,
        target_discord_id=200,
        target_username="TestUser",
        department="SASP",
        rank="Cadet",
        approved_by_discord_id=999,
    )
    assert rec.status == "PENDING"
    assert rec.request_id
    assert rec.target_discord_id == 200

    # Round-trip
    fetched = get_request(rec.request_id)
    assert fetched is not None
    assert fetched.department == "SASP"
    assert fetched.rank == "Cadet"
    assert fetched.status == "PENDING"


def test_happy_path_transitions(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED, FAILED,
        create_request, get_request,
        mark_processing, mark_completed, transition,
    )

    rec = create_request(
        requester_id=100, target_discord_id=200, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=999,
    )
    # PENDING -> APPROVED
    rec = transition(rec.request_id, from_states={PENDING}, to_state=APPROVED, updates={"approver_id": 999})
    assert rec.status == APPROVED
    # APPROVED -> PROCESSING
    rec = mark_processing(rec.request_id, approver_id=999)
    assert rec.status == PROCESSING
    # PROCESSING -> COMPLETED
    rec = mark_completed(rec.request_id, completion_message_id="12345")
    assert rec.status == COMPLETED
    assert rec.completion_message_id == "12345"


def test_terminal_state_cannot_be_re_transitioned(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    transition(rec.request_id, from_states={PROCESSING}, to_state=COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING, APPROVED}, to_state=APPROVED)


def test_illegal_from_state_rejected(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    # Can't go PENDING -> COMPLETED (must go via APPROVED -> PROCESSING)
    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING}, to_state=COMPLETED)
    # And the state machine only allows APPROVED -> PROCESSING, not PENDING -> PROCESSING.
    # A PENDING -> PROCESSING call has PENDING NOT IN {APPROVED} so it must raise.
    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)


def test_idempotent_re_approval_returns_existing_result(fake_spreadsheet):
    """A second COMPLETED transition attempt must raise, not silently re-execute."""
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition, get_request, is_terminal,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    transition(rec.request_id, from_states={PROCESSING}, to_state=COMPLETED)

    # Second approval attempt
    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING, APPROVED}, to_state=APPROVED)
    fetched = get_request(rec.request_id)
    assert fetched.status == COMPLETED
    assert is_terminal(COMPLETED)


def test_unknown_request_id_raises(fake_spreadsheet):
    from services.approval_service import transition, RequestNotFoundError, PENDING, APPROVED

    with pytest.raises(RequestNotFoundError):
        transition("nonexistent-id", from_states={PENDING}, to_state=APPROVED)


def test_reject_path(fake_spreadsheet):
    from services.approval_service import (
        PENDING, REJECTED,
        create_request, mark_rejected, get_request,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    rec = mark_rejected(rec.request_id, approver_id=3, completion_message_id="999")
    assert rec.status == REJECTED
    fetched = get_request(rec.request_id)
    assert fetched.status == REJECTED


def test_failed_path(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, FAILED,
        create_request, transition, mark_failed, mark_processing,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    mark_processing(rec.request_id)
    # Can fail from PROCESSING
    rec = mark_failed(rec.request_id, result_metadata={"reason": "test"})
    assert rec.status == FAILED
    assert rec.result_metadata.get("reason") == "test"


def test_unknown_state_raises_value_error(fake_spreadsheet):
    from services.approval_service import create_request, transition, PENDING
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="U",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    with pytest.raises(ValueError):
        transition(rec.request_id, from_states={PENDING}, to_state="WHATEVER")
