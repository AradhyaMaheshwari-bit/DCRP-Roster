"""
sheets.exceptions
----------------
Typed exceptions raised by the Sheets layer.

Services translate these into user-facing error states; cogs never see them
directly.
"""

from __future__ import annotations


class SheetsError(Exception):
    """Base class for all Sheets-layer errors."""


class SheetTabMissingError(SheetsError):
    """Requested tab does not exist in the spreadsheet."""

    def __init__(self, spreadsheet_id: str, tab_name: str) -> None:
        super().__init__(f"Tab '{tab_name}' missing in spreadsheet {spreadsheet_id}")
        self.spreadsheet_id = spreadsheet_id
        self.tab_name = tab_name


class SheetColumnMissingError(SheetsError):
    """A required column header is not present in the tab."""

    def __init__(self, tab_name: str, column: str) -> None:
        super().__init__(f"Column '{column}' missing in tab '{tab_name}'")
        self.tab_name = tab_name
        self.column = column


class SheetRateLimitError(SheetsError):
    """Google Sheets API rate limit / quota exceeded."""


class SheetAuthError(SheetsError):
    """Service-account credentials invalid or unauthorized for this sheet."""


class SheetNotFoundError(SheetsError):
    """Spreadsheet ID not found or not shared with the service account."""


class SheetWriteError(SheetsError):
    """A write/update operation failed (after retries)."""

    def __init__(self, tab_name: str, reason: str) -> None:
        super().__init__(f"Write to tab '{tab_name}' failed: {reason}")
        self.tab_name = tab_name
        self.reason = reason
