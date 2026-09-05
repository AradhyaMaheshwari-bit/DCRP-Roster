"""
services.permissions
--------------------
HR-role gate.

A single Discord role (configured via `HR_ROLE_ID`) gates who can approve
role requests. This module is the only place that knows about the role ID;
cogs ask `is_authorized_approver(user)` and never inspect roles directly.
"""

from __future__ import annotations

from typing import Protocol

from config import get_settings


class _HasRoles(Protocol):
    """Anything that exposes a list of role IDs (discord.Member, etc.)."""
    @property
    def roles(self) -> list:  # type: ignore[type-arg]
        ...


def is_authorized_approver(user: _HasRoles) -> bool:
    """Return True if `user` holds the configured HR role.

    Works for any object with `.roles: list[Role | Snowflake]` — uses
    `getattr` to avoid an explicit import of `discord.Member` here.
    """
    role_id = get_settings().hr_role_id
    try:
        user_role_ids = {int(getattr(r, "id", r)) for r in user.roles}
    except (AttributeError, TypeError):
        return False
    return role_id in user_role_ids
