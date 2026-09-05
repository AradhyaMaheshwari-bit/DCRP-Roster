"""
services.loa_service
--------------------
Stub: set / clear LOA (Leave of Absence) for a member.
"""

from __future__ import annotations

from typing import Any


def set_loa(
    discord_id: str,
    start_date: str,
    end_date: str,
    approver_id: int,
) -> Any:
    raise NotImplementedError(
        "loa_service.set_loa is a Phase 4 stub. "
        "Implementation deferred until the user specifies the /loa command shape."
    )


def clear_loa(discord_id: str, approver_id: int) -> Any:
    raise NotImplementedError(
        "loa_service.clear_loa is a Phase 4 stub. "
        "Implementation deferred until the user specifies the /loa command shape."
    )
