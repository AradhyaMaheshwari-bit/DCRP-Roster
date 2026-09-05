"""
services.transfer_service
-------------------------
Stub: transfer a member from one (department, rank) to another.
"""

from __future__ import annotations

from typing import Any


def transfer(
    discord_id: str,
    new_department: str,
    new_rank: str,
    approver_id: int,
) -> Any:
    raise NotImplementedError(
        "transfer_service.transfer is a Phase 4 stub. "
        "Implementation deferred until the user specifies the /transfer command shape."
    )
