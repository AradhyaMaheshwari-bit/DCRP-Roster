"""
services.role_service
---------------------
Discord role resolution and reconciliation.

Owns the mapping (Department, Rank) → Discord role IDs (`role_mapping.yaml`).
Cogs never resolve roles themselves.

Public API:
  * `resolve_rank_roles(department, rank) -> list[int]`
  * `resolve_department_role(department) -> int | None`
  * `reconcile_roles(guild_member, new_department, new_rank) -> DiscordResult`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml

from config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RoleConfigError(Exception):
    """Raised when the role mapping is missing/invalid for a given (dept, rank)."""


class RoleConfigMissingError(RoleConfigError):
    """Raised when an entry exists but `role_ids` is empty, or doesn't exist at all."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class DiscordResult:
    """Structured result of a Discord-side operation."""

    success: bool
    role_ids_added: list[int] = field(default_factory=list)
    role_ids_removed: list[int] = field(default_factory=list)
    role_ids_failed: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_human(self) -> str:
        if self.success:
            return (
                f"Discord roles: added={self.role_ids_added} "
                f"removed={self.role_ids_removed}"
            )
        return (
            f"Discord roles FAILED: added={self.role_ids_added} "
            f"removed={self.role_ids_removed} failed={self.role_ids_failed} "
            f"errors={self.errors}"
        )


class _MemberLike(Protocol):
    id: int
    roles: list[Any]

    async def add_roles(self, *roles: Any, reason: Optional[str] = None) -> Any: ...
    async def remove_roles(self, *roles: Any, reason: Optional[str] = None) -> Any: ...
    async def fetch_guild(self) -> Any: ...


# ---------------------------------------------------------------------------
# Mapping load
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_role_mapping(path: str) -> dict[str, Any]:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _get_mapping() -> dict[str, Any]:
    return _load_role_mapping(str(get_settings().role_mapping_path))


def reset_role_mapping_cache() -> None:
    """Drop the lru_cache — useful in tests after editing role_mapping.yaml."""
    _load_role_mapping.cache_clear()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_rank_roles(department: str, rank: str) -> list[int]:
    """Return the list of Discord role IDs for (department, rank).

    Raises RoleConfigMissingError if the entry is missing or has an empty
    role_ids list. This is intentional: the bot will refuse to complete
    an approval that targets an unconfigured (department, rank) pair.
    """
    mapping = _get_mapping()
    key = f"{department}::{rank}"
    rank_roles: dict[str, list[int]] = mapping.get("rank_roles", {})
    if key not in rank_roles:
        raise RoleConfigMissingError(
            f"No entry in role_mapping.yaml for '{key}'. "
            f"Add one before approving requests for this rank."
        )
    role_ids = list(rank_roles[key] or [])
    if not role_ids:
        raise RoleConfigMissingError(
            f"Entry '{key}' exists in role_mapping.yaml but has empty role_ids. "
            f"Fill in the real Discord role ID(s) before approving."
        )
    return [int(r) for r in role_ids]


def resolve_department_role(department: str) -> Optional[int]:
    """Return the Discord role ID for the department itself, or None.

    Returns None if the entry exists but role_ids is empty (treat as
    "not configured" — caller may log and continue).
    """
    mapping = _get_mapping()
    dept_roles: dict[str, list[int]] = mapping.get("department_roles", {})
    role_ids = dept_roles.get(department, []) or []
    if not role_ids:
        return None
    return int(role_ids[0])


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def reconcile_roles(
    member: _MemberLike,
    new_department: str,
    new_rank: str,
) -> DiscordResult:
    """Bring `member`'s rank/department roles into line with (new_department, new_rank).

    Adds the new department + rank roles, and removes every rank role from
    any other (department, rank) entry the member currently holds. Best
    effort: errors on individual role add/remove are recorded but do not
    stop the rest of the operation.
    """
    result = DiscordResult(success=True)

    new_rank_ids = set(resolve_rank_roles(new_department, new_rank))
    new_dept_id = resolve_department_role(new_department)

    # Compute the union of all rank role IDs from mapping (so we can spot
    # "obsolete" ones currently on the member).
    mapping = _get_mapping()
    all_rank_ids: set[int] = set()
    for rids in mapping.get("rank_roles", {}).values():
        for rid in rids or []:
            all_rank_ids.add(int(rid))

    member_role_ids = {int(getattr(r, "id", r)) for r in member.roles}

    # --- removes ---
    obsolete = (member_role_ids & all_rank_ids) - new_rank_ids
    if obsolete:
        try:
            removable = [r for r in member.roles if int(getattr(r, "id", r)) in obsolete]
            if removable:
                await member.remove_roles(*removable, reason="DCRP reconcile obsolete rank roles")
            result.role_ids_removed = sorted(obsolete)
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.errors.append(f"remove_roles failed: {exc}")

    # --- adds (rank) ---
    to_add_rank = [r for r in member.roles if int(getattr(r, "id", r)) in new_rank_ids]
    # But we need the actual Role objects — fetch by ID from guild roles.
    guild = await member.fetch_guild()
    role_lookup = {int(r.id): r for r in guild.roles}

    missing_ranks = new_rank_ids - member_role_ids
    rank_objs = [role_lookup[rid] for rid in missing_ranks if rid in role_lookup]
    if rank_objs:
        try:
            await member.add_roles(*rank_objs, reason=f"DCRP assign rank {new_rank} ({new_department})")
            result.role_ids_added += sorted(missing_ranks)
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.errors.append(f"add_roles(rank) failed: {exc}")
            result.role_ids_failed += sorted(missing_ranks)

    # --- adds (department) ---
    if new_dept_id and new_dept_id not in member_role_ids and new_dept_id in role_lookup:
        try:
            await member.add_roles(
                role_lookup[new_dept_id],
                reason=f"DCRP assign department {new_department}",
            )
            result.role_ids_added.append(new_dept_id)
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.errors.append(f"add_roles(department) failed: {exc}")
            result.role_ids_failed.append(new_dept_id)

    return result
