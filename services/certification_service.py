"""
services.certification_service
------------------------------
Stub: grant / revoke certifications (MPU, PIU, AIR, SWAT, K9, CID, GIU, etc.).
"""

from __future__ import annotations

from typing import Any


def grant(discord_id: str, certification: str, approver_id: int) -> Any:
    raise NotImplementedError(
        "certification_service.grant is a Phase 4 stub."
    )


def revoke(discord_id: str, certification: str, approver_id: int) -> Any:
    raise NotImplementedError(
        "certification_service.revoke is a Phase 4 stub."
    )
