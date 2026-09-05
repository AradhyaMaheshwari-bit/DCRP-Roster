"""
services.personnel_service
--------------------------
Personnel lookup and upsert.

Primary identity: Discord User ID (never username).

Field-ownership rule: an upsert only writes the columns the caller
supplies. Every other column is preserved verbatim. This is the
mechanism that prevents accidental overwrites of e.g. `Strike`,
`Last Promotion Date`, `LOA Start Date` when only rank+department
are being changed.

Conflict rule: a Discord ID that appears in >1 roster tab = FAIL
SAFELY. The service returns CONFLICT_MULTIPLE; the caller decides
what to do (probably notify staff and not write anything).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import yaml

from config import get_settings
from sheets import client as sheets_client
from sheets import repository
from sheets.exceptions import SheetColumnMissingError, SheetTabMissingError, SheetsError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Tabs that hold roster rows keyed by Discord ID. This list is the set of
# tabs the personnel service searches for find/upsert. The HOME PAGE,
# Armoury Authorization, PENDING REQUESTS, AUDIT_LOG and other
# non-roster tabs are excluded by construction (no Discord ID column).
DEPARTMENT_ROSTER_TABS: list[str] = [
    "SASP ROSTER",
    "LSPD ROSTER",
    "BCSO ROSTER",
    "PSC ROSTER",
    "SASPR ROSTER",
    "SAHP ROSTER",
    "DOC ROSTER",
    "SWAT ROSTER",
    "CID ROSTER",
    "SUB DEPARTMENT ROSTER",
    "DOJ",
]


@dataclass
class PersonnelRecord:
    """One personnel row, keyed by its source tab + 1-based row index."""
    tab_name: str
    row_index: int
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default) or default

    @property
    def discord_id(self) -> str:
        return self.get("Discord ID")


@dataclass
class UpsertOutcome:
    """Outcome of a personnel upsert."""
    outcome: str  # "CREATED" | "UPDATED" | "CONFLICT_MULTIPLE" | "NOT_FOUND_TAB"
    record: Optional[PersonnelRecord] = None
    conflicting_tabs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Department → tab mapping
# ---------------------------------------------------------------------------

DEPARTMENT_TO_TAB: dict[str, str] = {
    "SASP": "SASP ROSTER",
    "LSPD": "LSPD ROSTER",
    "BCSO": "BCSO ROSTER",
    "PSC": "PSC ROSTER",
    "SASPR": "SASPR ROSTER",
    "SAHP": "SAHP ROSTER",
    "DOC": "DOC ROSTER",
    "SWAT": "SWAT ROSTER",
    "CID": "CID ROSTER",
    "DOJ": "DOJ",
}

# Sub-departments (HR, MPU, PIU, AIR, K9, GIU, FTD, SEU, WING, MBU) share
# the SUB DEPARTMENT ROSTER tab.
SUBDIVISION_NAMES: set[str] = {
    "HR", "MPU", "PIU", "AIR", "K9", "GIU", "FTD", "SEU", "WING", "MBU", "ACADEMY",
}

CID_RANKS: set[str] = {
    "Detective", "Senior Detective", "SSA", "SAC",
    "Deputy Director", "Asst. Director", "Director", "OverWatch",
}


def tab_for(department: str) -> str:
    """Return the roster tab name for a (sub-)department."""
    if department in SUBDIVISION_NAMES:
        return "SUB DEPARTMENT ROSTER"
    if department in DEPARTMENT_TO_TAB:
        return DEPARTMENT_TO_TAB[department]
    raise ValueError(f"Unknown department: {department!r}")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_by_discord_id(discord_id: str) -> Optional[PersonnelRecord]:
    """Return the FIRST roster row matching `discord_id`, or None.

    Linear scan across every roster tab. Returns the first hit
    (lowest row index in tab order). Use `find_all_by_discord_id` for
    conflict detection.
    """
    for rec in find_all_by_discord_id(discord_id):
        return rec
    return None


def find_all_by_discord_id(discord_id: str) -> list[PersonnelRecord]:
    """Return ALL roster rows matching `discord_id` across all tabs."""
    target = (str(discord_id) or "").strip()
    if not target:
        return []

    try:
        spreadsheet = sheets_client.open_target_spreadsheet()
    except SheetsError as exc:
        logger.error("find_all_by_discord_id: cannot open Target sheet: %s", exc)
        return []

    results: list[PersonnelRecord] = []
    for tab_name in DEPARTMENT_ROSTER_TABS:
        try:
            ws = spreadsheet.worksheet(tab_name)
        except Exception:  # noqa: BLE001 — tab not yet bootstrapped
            continue
        try:
            locs = repository.find_all_rows_by_column(ws, "Discord ID", target)
        except SheetColumnMissingError:
            continue
        for loc in locs:
            results.append(
                PersonnelRecord(
                    tab_name=tab_name,
                    row_index=loc.row_index,
                    fields=_row_to_dict(ws, loc.row_index),
                )
            )
    return results


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert(
    discord_id: str,
    target_discord_username: str,
    department: str,
    rank: str,
    *,
    hire_date: Optional[str] = None,
    last_promotion_date: Optional[str] = None,
    activity_status: Optional[str] = None,
) -> UpsertOutcome:
    """Upsert a roster row keyed by Discord ID.

    Behavior:
      * 0 matches anywhere → CREATE a row in the tab for `department`.
      * 1 match in any tab → UPDATE that row in place.
      * 2+ matches in any tab → CONFLICT_MULTIPLE; do NOT modify anything.

    Field ownership: this function only writes the fields it explicitly
    receives as kwargs. Every other column is preserved from the
    existing row (or left blank on CREATE).
    """
    target = (str(discord_id) or "").strip()
    if not target:
        return UpsertOutcome(outcome="NOT_FOUND_TAB")

    existing_records = find_all_by_discord_id(target)
    if len(existing_records) > 1:
        return UpsertOutcome(
            outcome="CONFLICT_MULTIPLE",
            conflicting_tabs=[r.tab_name for r in existing_records],
        )

    tab_name = tab_for(department)

    try:
        spreadsheet = sheets_client.open_target_spreadsheet()
        ws = repository.ensure_tab(spreadsheet, tab_name)
    except SheetsError as exc:
        logger.error("upsert: cannot open Target sheet: %s", exc)
        return UpsertOutcome(outcome="NOT_FOUND_TAB")

    headers = repository.read_header_row(ws)

    def _build(existing: Optional[dict[str, str]]) -> list[str]:
        # Start with the existing row, then overlay only the fields we
        # were told to write. This is the field-ownership rule.
        row: dict[str, str] = {}
        if existing:
            row.update(existing)
        else:
            # On CREATE, seed every header with an empty string.
            for h in headers:
                row[h] = ""

        # Always overwrite identity fields.
        row["Discord ID"] = target
        row["Discord username"] = target_discord_username
        # Department goes into the Department column (if present) or
        # Sub department column for sub-departments.
        if department in SUBDIVISION_NAMES:
            row["Sub department"] = department
        else:
            if "Department" in row and row["Department"]:
                # Only set Department on CREATE; otherwise preserve it.
                pass
            row["Department"] = department
        row["Rank"] = rank

        # Conditional writes — only on the fields the caller supplied.
        if hire_date is not None:
            row["Hire Date"] = hire_date
        if last_promotion_date is not None:
            row["Last Promotion Date"] = last_promotion_date
        if activity_status is not None:
            row["Activity Status"] = activity_status

        # New users default to ACTIVE if the column is empty.
        if not row.get("Activity Status"):
            row["Activity Status"] = "ACTIVE"

        # Emit in header order.
        return [str(row.get(h, "")) for h in headers]

    if not existing_records:
        new_row = _build(None)
        new_index = repository.append_row(ws, new_row)
        return UpsertOutcome(
            outcome="CREATED",
            record=PersonnelRecord(
                tab_name=tab_name,
                row_index=new_index,
                fields=dict(zip(headers, new_row)),
            ),
        )

    # Exactly one existing row — update it in place.
    rec = existing_records[0]
    new_row = _build(rec.fields)
    repository.update_row(ws, rec.row_index, new_row)
    return UpsertOutcome(
        outcome="UPDATED",
        record=PersonnelRecord(
            tab_name=rec.tab_name,
            row_index=rec.row_index,
            fields=dict(zip(headers, new_row)),
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(ws, row_index: int) -> dict[str, str]:
    """Read a single row and return it keyed by header (delegates to repo)."""
    headers = repository.read_header_row(ws)
    raw = ws.row_values(row_index)
    out: dict[str, str] = {}
    for i, h in enumerate(headers):
        cell = raw[i] if i < len(raw) else ""
        out[h] = str(cell) if cell is not None else ""
    return out


def today_iso(timezone_name: str) -> str:
    """Return today's date as YYYY-MM-DD in the given IANA timezone."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        return _dt.now(ZoneInfo(timezone_name)).date().isoformat()
    except Exception:  # noqa: BLE001 — fall back to UTC
        return datetime.utcnow().date().isoformat()
