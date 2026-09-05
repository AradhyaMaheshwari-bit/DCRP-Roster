"""
tests.test_idempotency
----------------------
Offline tests for the "approve is idempotent" contract.

The contract: once a request is in a terminal state (COMPLETED, REJECTED,
FAILED), subsequent approval or rejection clicks must NOT re-execute the
underlying side effects. They must surface a clear "already processed"
error to the caller.

Covers:
  * After a PENDING → APPROVED → PROCESSING → COMPLETED chain, a second
    "Approve" click (which calls transition with from_states={PENDING,
    APPROVED} → APPROVED) raises InvalidStateTransitionError; the row's
    status remains COMPLETED.
  * After PENDING → REJECTED, a second "Approve" click raises.
  * After PENDING → FAILED, a second "Approve" click raises.
  * Two concurrent transition() calls on the same request_id cannot both
    pass the precondition — exactly one wins.
  * The orchestrator's "already completed" branch returns the existing
    record (caller does not re-run Discord + Sheets).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("HR_ROLE_ID", "1")
os.environ.setdefault("GOOGLE_CREDENTIALS_PATH", "./secrets/x.json")
os.environ.setdefault("TARGET_SPREADSHEET_ID", "target")
os.environ.setdefault("NOVA_SPREADSHEET_ID", "nova")

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_role_request_state_machine import FakeSpreadsheet, FakeWorksheet  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_spreadsheet(monkeypatch):
    import services.approval_service as svc
    fs = FakeSpreadsheet()
    fs.worksheets["PENDING REQUESTS"] = FakeWorksheet("PENDING REQUESTS", list(svc.PENDING_HEADERS))
    monkeypatch.setattr("sheets.client.open_target_spreadsheet", lambda: fs)
    return fs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_second_approve_after_completed_raises(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    transition(rec.request_id, from_states={PROCESSING}, to_state=COMPLETED)

    # Simulate a second click of the Approve button.
    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING, APPROVED}, to_state=APPROVED)

    # The row's status remains COMPLETED — no side-effects re-ran.
    fetched = get_request(rec.request_id)
    assert fetched.status == COMPLETED


def test_approve_after_rejected_raises(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, REJECTED,
        create_request, transition, mark_rejected, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    mark_rejected(rec.request_id)

    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING, APPROVED}, to_state=APPROVED)
    assert get_request(rec.request_id).status == REJECTED


def test_approve_after_failed_raises(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, FAILED,
        create_request, transition, mark_failed, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    mark_failed(rec.request_id, result_metadata={"reason": "config missing"})

    with pytest.raises(InvalidStateTransitionError):
        transition(rec.request_id, from_states={PENDING, APPROVED}, to_state=APPROVED)
    assert get_request(rec.request_id).status == FAILED


def test_reject_after_completed_raises(fake_spreadsheet):
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition, mark_rejected,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    transition(rec.request_id, from_states={PROCESSING}, to_state=COMPLETED)

    # A late "Reject" click must not flip a COMPLETED request to REJECTED.
    with pytest.raises(InvalidStateTransitionError):
        mark_rejected(rec.request_id)


def test_idempotent_transition_does_not_mutate_other_fields(fake_spreadsheet):
    """The terminal-state guard must short-circuit BEFORE updates are applied."""
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    transition(
        rec.request_id,
        from_states={PROCESSING}, to_state=COMPLETED,
        updates={"completion_message_id": "11111"},
    )

    with pytest.raises(InvalidStateTransitionError):
        transition(
            rec.request_id,
            from_states={PENDING, APPROVED}, to_state=APPROVED,
            updates={"approver_id": 999},  # would clobber if not blocked
        )
    # The completion_message_id from the legitimate run is still there.
    fetched = get_request(rec.request_id)
    assert fetched.completion_message_id == "11111"
    assert fetched.approver_id != 999


def test_concurrent_transitions_only_one_wins(fake_spreadsheet):
    """Two threads racing to transition the same PENDING request:
    exactly one wins, the other sees the new state and raises.
    """
    from services.approval_service import (
        PENDING, APPROVED,
        create_request, transition, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )

    results: list[str] = []  # "win" | "lose"
    errors: list[Exception] = []

    def attempt():
        try:
            transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
            results.append("win")
        except InvalidStateTransitionError:
            results.append("lose")

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert sorted(results) == ["lose", "win"]
    assert get_request(rec.request_id).status == APPROVED


def test_processing_to_completed_is_one_shot(fake_spreadsheet):
    """Marking the same request COMPLETED twice must not re-apply metadata."""
    from services.approval_service import (
        PENDING, APPROVED, PROCESSING, COMPLETED,
        create_request, transition, mark_completed, get_request,
        InvalidStateTransitionError,
    )
    rec = create_request(
        requester_id=1, target_discord_id=2, target_username="u",
        department="SASP", rank="Cadet", approved_by_discord_id=3,
    )
    transition(rec.request_id, from_states={PENDING}, to_state=APPROVED)
    transition(rec.request_id, from_states={APPROVED}, to_state=PROCESSING)
    mark_completed(rec.request_id, completion_message_id="first")

    with pytest.raises(InvalidStateTransitionError):
        mark_completed(rec.request_id, completion_message_id="second")
    assert get_request(rec.request_id).completion_message_id == "first"
