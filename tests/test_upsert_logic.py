"""
tests.test_upsert_logic
-----------------------
Offline tests for services.personnel_service.upsert.

Covers:
  * new user (0 matching rows) → CREATED in the right tab
  * existing user (1 matching row) → UPDATED in place
  * conflict (2+ rows in different tabs with same Discord ID) → CONFLICT_MULTIPLE
    and the existing rows are NOT modified
  * field ownership: only fields the caller supplies are written; every
    other column is preserved verbatim
  * default Activity Status = ACTIVE on a new row
  * default Hire Date is left blank unless supplied
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure env vars exist before any config import.
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

# Reuse fakes from the state-machine tests.
from tests.test_role_request_state_machine import FakeSpreadsheet, FakeWorksheet  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical header set (subset of what `org_structure.yaml` declares for
# SASP ROSTER) — these are the columns upsert() can populate.
# ---------------------------------------------------------------------------
SASP_HEADERS: list[str] = [
    "Discord ID",
    "Discord username",
    "Name",
    "Department",
    "Rank",
    "Sub department",
    "Hire Date",
    "Last Promotion Date",
    "Activity Status",
    "Strike",
    "LOA Start Date",
    "LOA End Date",
    "Notes",
]

CID_HEADERS: list[str] = [
    "Discord ID",
    "Discord username",
    "Name",
    "Department",
    "Rank",
    "Hire Date",
    "Activity Status",
    "Notes",
]

SUB_DEPT_HEADERS: list[str] = [
    "Discord ID",
    "Discord username",
    "Name",
    "Sub department",
    "Rank",
    "Hire Date",
    "Activity Status",
    "Notes",
]


# ---------------------------------------------------------------------------
# Fixture: a Target spreadsheet pre-seeded with the roster tabs we exercise.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_spreadsheet(monkeypatch):
    """Seed SASP ROSTER, CID ROSTER, SUB DEPARTMENT ROSTER; patch open_target_spreadsheet."""
    import services.personnel_service as svc

    fs = FakeSpreadsheet()
    fs.worksheets["SASP ROSTER"] = FakeWorksheet("SASP ROSTER", list(SASP_HEADERS))
    fs.worksheets["LSPD ROSTER"] = FakeWorksheet("LSPD ROSTER", list(SASP_HEADERS))
    fs.worksheets["CID ROSTER"] = FakeWorksheet("CID ROSTER", list(CID_HEADERS))
    fs.worksheets["SUB DEPARTMENT ROSTER"] = FakeWorksheet(
        "SUB DEPARTMENT ROSTER", list(SUB_DEPT_HEADERS)
    )

    monkeypatch.setattr("sheets.client.open_target_spreadsheet", lambda: fs)
    return fs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_new_user_in_department_tab(fake_spreadsheet):
    from services.personnel_service import upsert

    out = upsert(
        discord_id="111",
        target_discord_username="alice",
        department="SASP",
        rank="Cadet",
    )
    assert out.outcome == "CREATED"
    assert out.record is not None
    assert out.record.tab_name == "SASP ROSTER"
    assert out.record.discord_id == "111"
    assert out.record.get("Discord username") == "alice"
    assert out.record.get("Department") == "SASP"
    assert out.record.get("Rank") == "Cadet"
    # New user defaults
    assert out.record.get("Activity Status") == "ACTIVE"
    # Hire Date not supplied → blank
    assert out.record.get("Hire Date") == ""


def test_update_existing_user_preserves_unrelated_columns(fake_spreadsheet):
    from services.personnel_service import upsert

    # Pre-seed a SASP roster row.
    ws = fake_spreadsheet.worksheet("SASP ROSTER")
    ws.append_row([
        "222", "bob", "Bob", "SASP", "Cadet", "",
        "2025-01-15", "", "ACTIVE", "2", "", "", "veteran",
    ])

    # Promote to Corporal — only Rank + Last Promotion Date supplied.
    out = upsert(
        discord_id="222",
        target_discord_username="bob",
        department="SASP",
        rank="Corporal",
        last_promotion_date="2026-09-05",
    )
    assert out.outcome == "UPDATED"
    assert out.record.get("Rank") == "Corporal"
    assert out.record.get("Last Promotion Date") == "2026-09-05"
    # Field-ownership: untouched fields preserved exactly.
    assert out.record.get("Name") == "Bob"
    assert out.record.get("Hire Date") == "2025-01-15"
    assert out.record.get("Strike") == "2"
    assert out.record.get("Notes") == "veteran"
    # Discord username is always overwritten (identity).
    assert out.record.get("Discord username") == "bob"
    # Activity Status preserved because caller did not supply it and it's already set.
    assert out.record.get("Activity Status") == "ACTIVE"


def test_conflict_multiple_tabs_returns_no_writes(fake_spreadsheet):
    from services.personnel_service import upsert

    # Same Discord ID in SASP ROSTER and CID ROSTER — a conflict scenario.
    ws_sasp = fake_spreadsheet.worksheet("SASP ROSTER")
    ws_cid = fake_spreadsheet.worksheet("CID ROSTER")
    ws_sasp.append_row(["333", "carol", "Carol", "SASP", "Cadet", "",
                        "2025-05-01", "", "ACTIVE", "0", "", "", ""])
    ws_cid.append_row(["333", "carol", "Carol", "CID", "Detective", "",
                       "2024-11-12", "ACTIVE", ""])

    out = upsert(
        discord_id="333",
        target_discord_username="carol",
        department="SASP",
        rank="Corporal",
    )
    assert out.outcome == "CONFLICT_MULTIPLE"
    assert set(out.conflicting_tabs) == {"SASP ROSTER", "CID ROSTER"}
    # CRITICAL: no writes happened.
    assert ws_sasp._rows[0][4] == "Cadet"  # rank unchanged
    assert ws_cid._rows[0][4] == "Detective"  # rank unchanged
    assert out.record is None


def test_sub_department_routes_to_sub_department_roster(fake_spreadsheet):
    from services.personnel_service import upsert

    out = upsert(
        discord_id="444",
        target_discord_username="dave",
        department="MPU",  # sub-department
        rank="INSTRUCTOR",
    )
    assert out.outcome == "CREATED"
    assert out.record.tab_name == "SUB DEPARTMENT ROSTER"
    assert out.record.get("Sub department") == "MPU"
    assert out.record.get("Rank") == "INSTRUCTOR"


def test_unknown_department_raises_value_error(fake_spreadsheet):
    from services.personnel_service import upsert

    with pytest.raises(ValueError):
        upsert(
            discord_id="555",
            target_discord_username="eve",
            department="NOT_A_DEPARTMENT",
            rank="Cadet",
        )


def test_find_by_discord_id_returns_none_when_absent(fake_spreadsheet):
    from services.personnel_service import find_by_discord_id

    assert find_by_discord_id("999") is None


def test_find_by_discord_id_returns_first_match(fake_spreadsheet):
    from services.personnel_service import find_by_discord_id

    fake_spreadsheet.worksheet("SASP ROSTER").append_row(
        ["666", "frank", "Frank", "SASP", "Officer", "",
         "2025-02-02", "", "ACTIVE", "0", "", "", ""]
    )
    rec = find_by_discord_id("666")
    assert rec is not None
    assert rec.tab_name == "SASP ROSTER"
    assert rec.get("Name") == "Frank"


def test_caller_supplied_activity_status_is_written(fake_spreadsheet):
    from services.personnel_service import upsert

    out = upsert(
        discord_id="777",
        target_discord_username="gina",
        department="SASP",
        rank="Cadet",
        activity_status="LOA",
    )
    assert out.outcome == "CREATED"
    assert out.record.get("Activity Status") == "LOA"


def test_caller_supplied_hire_date_is_written(fake_spreadsheet):
    from services.personnel_service import upsert

    out = upsert(
        discord_id="888",
        target_discord_username="henry",
        department="LSPD",
        rank="Officer",
        hire_date="2026-08-01",
    )
    assert out.outcome == "CREATED"
    assert out.record.get("Hire Date") == "2026-08-01"
    # Default Activity Status still applied because column was empty.
    assert out.record.get("Activity Status") == "ACTIVE"
