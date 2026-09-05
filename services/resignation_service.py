"""
services.resignation_service
----------------------------
Stub: resign / fire a member.

On resign/fire: append a row to the LEO Resign/Fire tab, clear the active
roster row, remove rank roles, set Activity Status to RESIGNED or FIRED,
audit.
"""

from __future__ import annotations

from typing import Any, Literal


def resign_or_fire(
    discord_id: str,
    action: Literal["RESIGN", "FIRE"],
    approver_id: int,
) -> Any:
    raise NotImplementedError(
        "resignation_service.resign_or_fire is a Phase 4 stub."
    )
