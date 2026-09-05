"""
services.promotion_service
--------------------------
Stub: promote a member to a new (department, rank) tuple.

Not implemented yet. When the user specifies the exact command shape, the
real implementation will:
  1. Validate the approver.
  2. Resolve new role mapping.
  3. Reconcile Discord roles.
  4. Update the existing roster row (UPSERT semantics — only Rank, Last
     Promotion Date, and the new Department fields change; everything
     else is preserved).
  5. Append an AUDIT_LOG entry with prev/new rank + department.
"""

from __future__ import annotations

from typing import Any


def promote(
    discord_id: str,
    new_rank: str,
    new_department: str,
    approver_id: int,
) -> Any:
    raise NotImplementedError(
        "promotion_service.promote is a Phase 4 stub. "
        "Implementation deferred until the user specifies the /promote command shape."
    )
