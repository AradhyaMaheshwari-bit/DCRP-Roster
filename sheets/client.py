"""
sheets.client
-------------
Google Sheets authentication + singleton client.

Authenticates with a service-account JSON key file (path from settings).
The same client is used for the Target sheet (read/write) and the Nova
sheet (read-only — never written to).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import get_settings
from .exceptions import SheetAuthError, SheetNotFoundError

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def _authorize(credentials_path: Path) -> Credentials:
    """Load the service-account credentials from disk."""
    if not credentials_path.exists():
        raise SheetAuthError(
            f"Service-account credentials file not found: {credentials_path}. "
            "Drop the JSON key at this path and re-run."
        )
    try:
        return Credentials.from_service_account_file(
            str(credentials_path), scopes=_SCOPES
        )
    except Exception as exc:  # noqa: BLE001 — surface as typed error
        raise SheetAuthError(f"Failed to load credentials: {exc}") from exc


@lru_cache(maxsize=1)
def get_gspread_client() -> gspread.Client:
    """Return a process-wide gspread client authorized with the SA key."""
    settings = get_settings()
    creds = _authorize(settings.credentials_file)
    logger.info(
        "Authorized Google service account: %s",
        creds.service_account_email,
    )
    return gspread.authorize(creds)


def open_spreadsheet(spreadsheet_id: str) -> gspread.Spreadsheet:
    """Open a spreadsheet by ID. Raises SheetNotFoundError on failure."""
    client = get_gspread_client()
    try:
        return client.open_by_key(spreadsheet_id)
    except gspread.SpreadsheetNotFound as exc:
        raise SheetNotFoundError(
            f"Spreadsheet {spreadsheet_id} not found or not shared with the "
            "service account. Share it with the SA email at "
            "<https://console.cloud.google.com/iam-admin/serviceaccounts>."
        ) from exc
    except gspread.exceptions.APIError as exc:
        # 401/403 → unauthorized; 404 → not found; otherwise surface as APIError.
        code = getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None
        if code in (401, 403):
            raise SheetAuthError(f"Unauthorized for {spreadsheet_id}: {exc}") from exc
        raise


def open_target_spreadsheet() -> gspread.Spreadsheet:
    """Open the production Target spreadsheet (the one we write to)."""
    settings = get_settings()
    return open_spreadsheet(settings.target_spreadsheet_id)


def open_nova_spreadsheet() -> gspread.Spreadsheet:
    """Open the reference Nova spreadsheet. READ-ONLY callers only.

    The Nova spreadsheet is NEVER written to. The repository layer enforces
    this by not exposing write helpers for it.
    """
    settings = get_settings()
    return open_spreadsheet(settings.nova_spreadsheet_id)


def reset_client_cache() -> None:
    """Drop the cached client — useful in tests."""
    get_gspread_client.cache_clear()
