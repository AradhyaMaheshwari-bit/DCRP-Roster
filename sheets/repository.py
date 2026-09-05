"""
sheets.repository
-----------------
Typed CRUD primitives over Google Sheets.

This module knows nothing about business logic. It only:
  * Reads / writes rows
  * Locates rows by Discord ID (or any column)
  * Upserts by Discord ID
  * Applies batch updates
  * Validates that a tab/column exists

All functions take a `gspread.Worksheet` (so callers decide whether it's
Target or Nova) and return either `list[dict[str, str]]` (rows keyed by
header) or a typed result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import gspread
from gspread.worksheet import ValueRenderOption

from .exceptions import (
    SheetColumnMissingError,
    SheetTabMissingError,
    SheetWriteError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A row is a dict keyed by column header → cell string.
Row = dict[str, str]


@dataclass(frozen=True)
class RowLocation:
    """A reference to a single row in a tab by 1-indexed position."""

    row_index: int  # 1-based, includes header
    row_values: list[str]


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of an upsert operation."""

    outcome: str  # "CREATED" | "UPDATED" | "CONFLICT_MULTIPLE" | "UNCONFIGURED"
    row_index: Optional[int] = None
    tab_name: Optional[str] = None
    conflicting_locations: Optional[list[RowLocation]] = None


# ---------------------------------------------------------------------------
# Header utilities
# ---------------------------------------------------------------------------

def _norm_header(s: str) -> str:
    """Normalize a header for case/whitespace-insensitive comparison."""
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def read_header_row(ws: gspread.Worksheet) -> list[str]:
    """Read row 1 (headers) of a worksheet as a list of strings."""
    values = ws.row_values(1, value_render_option=ValueRenderOption.formatted)
    return [str(v) for v in values]


def _build_header_index(headers: Sequence[str]) -> dict[str, int]:
    """Map normalized header → 0-based column index."""
    return {_norm_header(h): i for i, h in enumerate(headers)}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def read_all_rows(ws: gspread.Worksheet) -> list[Row]:
    """Read all data rows (row 2 onward) keyed by header.

    Each returned row is a dict of {header: cell-string}. Missing cells are
    returned as empty strings. Columns without a header at the same index
    are dropped.
    """
    headers = read_header_row(ws)
    if not headers:
        return []
    records = ws.get(
        values_range=f"A2:{_col_letter(len(headers))}",
        value_render_option=ValueRenderOption.formatted,
    )
    out: list[Row] = []
    for raw in records:
        row: Row = {}
        for i, header in enumerate(headers):
            cell = raw[i] if i < len(raw) else ""
            row[header] = str(cell) if cell is not None else ""
        out.append(row)
    return out


def find_row_by_column(
    ws: gspread.Worksheet, column: str, value: str
) -> Optional[RowLocation]:
    """Find the FIRST row whose `column` cell equals `value` (case-sensitive,
    whitespace-trimmed). Returns None if not found.

    Note: this is a linear scan. The Target sheet is small (<200 rows per
    tab) so this is acceptable; if that changes, switch to a binary search
    on a sorted range.
    """
    headers = read_header_row(ws)
    if not headers:
        return None
    col_idx = _build_header_index(headers).get(_norm_header(column))
    if col_idx is None:
        raise SheetColumnMissingError(ws.title, column)
    target = (value or "").strip()
    records = ws.get(
        values_range=f"A2:{_col_letter(len(headers))}",
        value_render_option=ValueRenderOption.formatted,
    )
    for offset, raw in enumerate(records):
        cell = raw[col_idx] if col_idx < len(raw) else ""
        if str(cell or "").strip() == target:
            return RowLocation(row_index=offset + 2, row_values=[str(v) if v is not None else "" for v in raw])
    return None


def find_all_rows_by_column(
    ws: gspread.Worksheet, column: str, value: str
) -> list[RowLocation]:
    """Like find_row_by_column, but returns ALL matches (for conflict detection)."""
    headers = read_header_row(ws)
    if not headers:
        return []
    col_idx = _build_header_index(headers).get(_norm_header(column))
    if col_idx is None:
        raise SheetColumnMissingError(ws.title, column)
    target = (value or "").strip()
    records = ws.get(
        values_range=f"A2:{_col_letter(len(headers))}",
        value_render_option=ValueRenderOption.formatted,
    )
    out: list[RowLocation] = []
    for offset, raw in enumerate(records):
        cell = raw[col_idx] if col_idx < len(raw) else ""
        if str(cell or "").strip() == target:
            out.append(
                RowLocation(
                    row_index=offset + 2,
                    row_values=[str(v) if v is not None else "" for v in raw],
                )
            )
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def append_row(ws: gspread.Worksheet, row_values: Sequence[str]) -> int:
    """Append a single row to the end of the tab. Returns the new 1-based row index."""
    try:
        return ws.append_row(
            list(row_values),
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
        )
    except gspread.exceptions.APIError as exc:
        raise SheetWriteError(ws.title, str(exc)) from exc


def update_row(
    ws: gspread.Worksheet,
    row_index: int,
    row_values: Sequence[str],
    *,
    expected_current: Optional[Sequence[str]] = None,
) -> None:
    """Update an entire row by 1-based index.

    If `expected_current` is given, the function refuses to write unless
    the current row values match exactly. This is the precondition that
    gives us optimistic-concurrency control on the PENDING REQUESTS tab
    (we never want two simultaneous approvals to both win).
    """
    ncols = len(row_values)
    if expected_current is not None:
        current = ws.row_values(
            row_index, value_render_option=ValueRenderOption.formatted
        )
        # Pad current to length of expected so we can compare.
        padded = list(current) + [""] * max(0, len(expected_current) - len(current))
        if [s.strip() for s in padded[: len(expected_current)]] != [
            s.strip() for s in expected_current
        ]:
            raise SheetWriteError(
                ws.title,
                f"precondition failed at row {row_index}: expected "
                f"{list(expected_current)!r}, got {current!r}",
            )
    body_range = f"A{row_index}:{_col_letter(ncols)}{row_index}"
    try:
        ws.update(
            [list(row_values)],
            range_name=body_range,
            value_input_option="USER_ENTERED",
        )
    except gspread.exceptions.APIError as exc:
        raise SheetWriteError(ws.title, str(exc)) from exc


def upsert_by_column(
    ws: gspread.Worksheet,
    column: str,
    key: str,
    row_builder: Callable[[Optional[Row]], list[str]],
) -> UpsertResult:
    """Upsert a row keyed by `column == key`.

    * If exactly one row matches → call `row_builder(existing_row)` and
      overwrite that row.
    * If zero rows match → call `row_builder(None)` and append.
    * If multiple rows match → return CONFLICT_MULTIPLE; the caller decides.

    `row_builder` receives the existing row dict (or None) and must return
    the full new row values. The builder is responsible for preserving any
    columns the caller doesn't want to overwrite.
    """
    matches = find_all_rows_by_column(ws, column, key)
    if len(matches) > 1:
        return UpsertResult(
            outcome="CONFLICT_MULTIPLE",
            tab_name=ws.title,
            conflicting_locations=matches,
        )
    if not matches:
        new_row = row_builder(None)
        new_index = append_row(ws, new_row)
        return UpsertResult(
            outcome="CREATED",
            row_index=new_index,
            tab_name=ws.title,
        )
    existing = _row_to_dict(ws, matches[0].row_index)
    new_row = row_builder(existing)
    update_row(ws, matches[0].row_index, new_row)
    return UpsertResult(
        outcome="UPDATED",
        row_index=matches[0].row_index,
        tab_name=ws.title,
    )


def batch_update_cells(
    ws: gspread.Worksheet,
    cell_updates: Iterable[tuple[int, int, str]],
) -> None:
    """Apply a batch of (row_1based, col_1based, value) updates in one call.

    Used to update specific cells in a row (e.g. status column + timestamp)
    without overwriting the rest of the row.
    """
    updates = list(cell_updates)
    if not updates:
        return
    payload: list[dict[str, Any]] = []
    for row, col, val in updates:
        payload.append(
            {
                "range": f"{_col_letter(col)}{row}:{_col_letter(col)}{row}",
                "values": [[val]],
            }
        )
    if not payload:
        return
    try:
        ws.batch_update(
            payload,
            value_input_option="USER_ENTERED",
        )
    except gspread.exceptions.APIError as exc:
        raise SheetWriteError(ws.title, str(exc)) from exc


# ---------------------------------------------------------------------------
# Tab management (used by bootstrap)
# ---------------------------------------------------------------------------

def ensure_tab(
    spreadsheet: gspread.Spreadsheet,
    tab_name: str,
) -> gspread.Worksheet:
    """Return the worksheet for `tab_name`, creating it if missing."""
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=100, cols=40)


def write_headers(ws: gspread.Worksheet, headers: Sequence[str]) -> None:
    """Overwrite row 1 with the given headers. Idempotent."""
    if not headers:
        return
    try:
        ws.update(
            [list(headers)],
            range_name=f"A1:{_col_letter(len(headers))}1",
            value_input_option="USER_ENTERED",
        )
    except gspread.exceptions.APIError as exc:
        raise SheetWriteError(ws.title, str(exc)) from exc


def append_static_rows(ws: gspread.Worksheet, rows: Sequence[Sequence[str]]) -> None:
    """Append non-personnel static rows after the header row. Idempotent only
    in the sense that the caller is responsible for not calling this twice
    on the same tab (bootstrap is gated by tab emptiness checks)."""
    if not rows:
        return
    try:
        ws.append_rows(
            [list(r) for r in rows],
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
        )
    except gspread.exceptions.APIError as exc:
        raise SheetWriteError(ws.title, str(exc)) from exc


def apply_column_widths(ws: gspread.Worksheet, widths: Sequence[int]) -> None:
    """Set column widths in pixels. No-op if the worksheet has no custom
    width API exposed (gspread delegates to `format` for some versions)."""
    if not widths:
        return
    requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": int(w)},
                "fields": "pixelSize",
            }
        }
        for i, w in enumerate(widths)
        if w and int(w) > 0
    ]
    if not requests:
        return
    try:
        ws.spreadsheet.batch_update({"requests": requests})
    except Exception as exc:  # noqa: BLE001 — column widths are cosmetic
        logger.warning("Could not set column widths on %s: %s", ws.title, exc)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _row_to_dict(ws: gspread.Worksheet, row_index: int) -> Row:
    """Read a single row and return it keyed by header."""
    headers = read_header_row(ws)
    raw = ws.row_values(
        row_index, value_render_option=ValueRenderOption.formatted
    )
    out: Row = {}
    for i, h in enumerate(headers):
        cell = raw[i] if i < len(raw) else ""
        out[h] = str(cell) if cell is not None else ""
    return out


def _col_letter(idx_1based: int) -> str:
    """Convert a 1-based column index to an A1 notation letter."""
    n = idx_1based
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
