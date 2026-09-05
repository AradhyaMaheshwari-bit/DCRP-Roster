"""
services.lookup_service
-----------------------
Stub: look up a member's personnel record + history.

Returns a PersonnelHistory object with:
  * Current roster entry (tab, row, all fields).
  * All audit-log entries mentioning this Discord ID.
  * The current state of the most recent role_request (if any).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class PersonnelHistory:
    discord_id: str
    current_record: Optional[Any]  # PersonnelRecord | None
    audit_entries: list[Any]       # list[AuditEntry]
    last_request_state: Optional[str] = None


def lookup(discord_id: str) -> PersonnelHistory:
    raise NotImplementedError(
        "lookup_service.lookup is a Phase 4 stub. "
        "Implementation deferred until the user specifies the /lookup command shape."
    )
