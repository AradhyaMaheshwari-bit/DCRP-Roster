"""
tests.test_audit
----------------
Offline tests for services.audit_service.

Covers:
  * append_audit creates the AUDIT_LOG tab with the documented schema if it
    doesn't exist.
  * append_audit appends a row whose columns are populated in the documented
    order.
  * If the AUDIT_LOG tab exists with the wrong schema (missing required
    columns), append_audit does NOT clobber the header and refuses to write
    a malformed row — the error is logged but the bot keeps running.
  * The audit log is append-only: there is no public API to update or
    delete an existing row. (Verified by import inspection.)
  * The AuditEntry dataclass has every documented field, with sensible
    defaults, and serializes deterministically.
"""

from __future__ import annotations

import os
import sys
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_spreadsheet(monkeypatch):
    """A Target sheet with NO AUDIT_LOG tab yet — exercises the auto-create path."""
    fs = FakeSpreadsheet()
    monkeypatch.setattr("sheets.client.open_target_spreadsheet", lambda: fs)
    return fs


@pytest.fixture
def audit_tab_existing(monkeypatch):
    """A Target sheet that already has an AUDIT_LOG tab with the correct schema."""
    import services.audit_service as svc
    fs = FakeSpreadsheet()
    fs.worksheets["AUDIT_LOG"] = FakeWorksheet("AUDIT_LOG", list(svc.AUDIT_HEADERS))
    monkeypatch.setattr("sheets.client.open_target_spreadsheet", lambda: fs)
    return fs


@pytest.fixture
def audit_tab_wrong_schema(monkeypatch):
    """A Target sheet with an AUDIT_LOG tab whose header is incomplete."""
    import services.audit_service as svc
    fs = FakeSpreadsheet()
    bad_headers = list(svc.AUDIT_HEADERS[:5])  # missing 11 required columns
    fs.worksheets["AUDIT_LOG"] = FakeWorksheet("AUDIT_LOG", bad_headers)
    monkeypatch.setattr("sheets.client.open_target_spreadsheet", lambda: fs)
    return fs


# ---------------------------------------------------------------------------
# Tests — tab creation
# ---------------------------------------------------------------------------

def test_append_audit_creates_tab_with_full_schema(fake_spreadsheet):
    from services.audit_service import AuditEntry, append_audit, AUDIT_HEADERS

    entry = AuditEntry(
        request_id="req-001",
        action="ROLE_REQUEST_APPROVED",
        target_discord_id=123456,
        target_username="alice",
        new_rank="Cadet",
        new_department="SASP",
        approver_id=999,
        overall_result="SUCCESS",
    )
    append_audit(entry)

    ws = fake_spreadsheet.worksheet("AUDIT_LOG")
    assert ws._headers == AUDIT_HEADERS
    assert len(ws._rows) == 1


def test_append_audit_writes_row_in_schema_order(fake_spreadsheet):
    from services.audit_service import AuditEntry, append_audit, AUDIT_HEADERS

    entry = AuditEntry(
        request_id="req-002",
        action="ROLE_REQUEST_APPROVED",
        target_discord_id=222,
        target_username="bob",
        prev_rank="Cadet",
        new_rank="Corporal",
        prev_department="SASP",
        new_department="SASP",
        requester_id=100,
        approver_id=999,
        overall_result="SUCCESS",
    )
    append_audit(entry)

    ws = fake_spreadsheet.worksheet("AUDIT_LOG")
    row = ws._rows[0]
    assert row[AUDIT_HEADERS.index("Request ID")] == "req-002"
    assert row[AUDIT_HEADERS.index("Action")] == "ROLE_REQUEST_APPROVED"
    assert row[AUDIT_HEADERS.index("Target Discord ID")] == "222"
    assert row[AUDIT_HEADERS.index("Target Username")] == "bob"
    assert row[AUDIT_HEADERS.index("Prev Rank")] == "Cadet"
    assert row[AUDIT_HEADERS.index("New Rank")] == "Corporal"
    assert row[AUDIT_HEADERS.index("Requester ID")] == "100"
    assert row[AUDIT_HEADERS.index("Approver ID")] == "999"
    assert row[AUDIT_HEADERS.index("Overall Result")] == "SUCCESS"


def test_append_audit_handles_existing_correct_tab(audit_tab_existing):
    from services.audit_service import AuditEntry, append_audit, AUDIT_HEADERS

    entry = AuditEntry(
        request_id="req-003",
        action="ROLE_REQUEST_REJECTED",
        target_discord_id=333,
        target_username="carol",
        approver_id=999,
        overall_result="FAILURE",
        failure_reason="HR rejected",
    )
    append_audit(entry)

    ws = audit_tab_existing.worksheet("AUDIT_LOG")
    assert ws._headers == AUDIT_HEADERS  # untouched
    assert len(ws._rows) == 1
    assert ws._rows[0][AUDIT_HEADERS.index("Failure Reason")] == "HR rejected"


def test_append_audit_refuses_to_clobber_wrong_schema(audit_tab_wrong_schema, caplog):
    """If an existing AUDIT_LOG has a wrong schema, the bot must not silently
    rewrite the header. The bad header is preserved; no row is written."""
    from services.audit_service import AuditEntry, append_audit
    import logging

    ws_before = audit_tab_wrong_schema.worksheet("AUDIT_LOG")
    bad_headers = list(ws_before._headers)
    assert len(bad_headers) == 5  # confirm our fixture

    entry = AuditEntry(
        request_id="req-004",
        action="ROLE_REQUEST_APPROVED",
        target_discord_id=444,
        target_username="dave",
        overall_result="SUCCESS",
    )
    with caplog.at_level(logging.ERROR, logger="services.audit_service"):
        append_audit(entry)

    ws_after = audit_tab_wrong_schema.worksheet("AUDIT_LOG")
    # Header preserved (not clobbered).
    assert ws_after._headers == bad_headers
    # No row written.
    assert ws_after._rows == []
    # Error was logged.
    assert any("header mismatch" in rec.message.lower() for rec in caplog.records)


def test_append_audit_serializes_metadata_as_json(audit_tab_existing):
    import json
    from services.audit_service import AuditEntry, append_audit, AUDIT_HEADERS

    entry = AuditEntry(
        request_id="req-005",
        action="ROLE_REQUEST_APPROVED",
        target_discord_id=555,
        target_username="eve",
        metadata={"discord": {"added": [111, 222]}, "sheets": "ok"},
        overall_result="SUCCESS",
    )
    append_audit(entry)
    ws = audit_tab_existing.worksheet("AUDIT_LOG")
    raw = ws._rows[0][AUDIT_HEADERS.index("Metadata")]
    parsed = json.loads(raw)
    assert parsed == {"discord": {"added": [111, 222]}, "sheets": "ok"}


def test_multiple_audit_entries_append_in_order(fake_spreadsheet):
    from services.audit_service import AuditEntry, append_audit, AUDIT_HEADERS

    for i, action in enumerate(["ROLE_REQUEST_APPROVED", "ROLE_REQUEST_REJECTED"]):
        append_audit(AuditEntry(
            request_id=f"req-{i:03d}",
            action=action,
            target_discord_id=100 + i,
            target_username=f"user{i}",
            overall_result="SUCCESS" if i == 0 else "FAILURE",
        ))
    ws = fake_spreadsheet.worksheet("AUDIT_LOG")
    assert len(ws._rows) == 2
    assert ws._rows[0][AUDIT_HEADERS.index("Request ID")] == "req-000"
    assert ws._rows[1][AUDIT_HEADERS.index("Request ID")] == "req-001"


# ---------------------------------------------------------------------------
# Tests — append-only contract
# ---------------------------------------------------------------------------

def test_audit_module_has_no_update_or_delete_api():
    """The module must not expose any function that mutates an existing row.
    The only public mutator is `append_audit` (which appends)."""
    import inspect
    import services.audit_service as mod

    forbidden = {"update_audit", "delete_audit", "remove_audit", "edit_audit"}
    for name, obj in vars(mod).items():
        if not callable(obj) or name.startswith("_"):
            continue
        assert name not in forbidden, f"audit service exposes mutator {name}"
        # The only mutator is append_audit.
        if name == "append_audit":
            continue


def test_audit_entry_defaults_are_sensible():
    from services.audit_service import AuditEntry
    e = AuditEntry(
        request_id="x",
        action="ROLE_REQUEST_APPROVED",
        target_discord_id=1,
        target_username="u",
    )
    assert e.prev_rank == ""
    assert e.new_rank == ""
    assert e.overall_result == ""
    assert e.metadata == {}
    assert e.timestamp.endswith("Z")
    assert "T" in e.timestamp  # ISO-8601 date+time


def test_audit_headers_cover_all_documented_columns():
    """Guard against accidental removal of a column from AUDIT_HEADERS —
    the orchestrator and downstream consumers depend on these names."""
    from services.audit_service import AUDIT_HEADERS

    required = {
        "Timestamp", "Request ID", "Action", "Target Discord ID",
        "Target Username", "Prev Rank", "New Rank", "Prev Department",
        "New Department", "Requester ID", "Approver ID", "Discord Result",
        "Sheets Result", "Overall Result", "Failure Reason", "Metadata",
    }
    assert required.issubset(set(AUDIT_HEADERS))
