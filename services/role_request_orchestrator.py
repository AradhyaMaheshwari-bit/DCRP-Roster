"""
services.role_request_orchestrator
----------------------------------
The single entry point for executing a `role_request` approval end-to-end.

Cogs call `orchestrate_approval(request_id, approver_id)`. Everything else
(role resolution, Discord reconciliation, Sheets upsert, audit logging,
state transition) happens here. The function returns a structured result
that the cog can use to update the approval embed.

Failure policy:
  * If anything fails BEFORE the Discord-side effect → mark the request
    FAILED with no audit entry, and let the caller decide what to do.
  * If the Discord side succeeded but Sheets failed → mark the request
    FAILED with a "PARTIAL" audit entry, and surface a clear message to
    the user.
  * If both succeed → mark COMPLETED with a SUCCESS audit entry.
  * If either side fails → we never roll back the side that succeeded.
    (Best-effort: log the orphan, notify staff, audit captures the state.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import approval_service, audit_service, personnel_service, role_service

logger = logging.getLogger(__name__)


@dataclass
class OrchestrationResult:
    request_id: str
    success: bool
    final_state: str
    discord_result: Optional[role_service.DiscordResult] = None
    sheets_outcome: str = ""  # "CREATED" | "UPDATED" | "CONFLICT_MULTIPLE" | "NOT_FOUND_TAB" | ""
    failure_reason: str = ""
    audit_overall: str = ""  # "SUCCESS" | "PARTIAL" | "FAILURE"

    def as_human(self) -> str:
        if self.success:
            return (
                f"Approved (request {self.request_id}). "
                f"Discord roles reconciled. Roster row {self.sheets_outcome or 'updated'}. "
                f"Audit: {self.audit_overall}."
            )
        if self.audit_overall == "PARTIAL":
            return (
                f"Partial success on {self.request_id}: Discord roles assigned, but "
                f"roster update FAILED ({self.sheets_outcome}). A coordinator has been "
                f"notified. Reason: {self.failure_reason or 'unknown'}."
            )
        return (
            f"Approval failed for {self.request_id} "
            f"(state={self.final_state}). Reason: {self.failure_reason or 'unknown'}."
        )


async def orchestrate_approval(
    request_id: str,
    approver_id: int,
    guild_member: Any,
) -> OrchestrationResult:
    """Execute the full approval pipeline for a pending `role_request`."""
    # --- 1. Lookup the request ---
    rec = approval_service.get_request(request_id)
    if not rec:
        return OrchestrationResult(
            request_id=request_id, success=False, final_state="MISSING",
            failure_reason="Request not found in PENDING REQUESTS tab.",
        )

    # --- 2. Idempotency: if already terminal, return existing result ---
    if approval_service.is_terminal(rec.status):
        return OrchestrationResult(
            request_id=request_id, success=(rec.status == approval_service.COMPLETED),
            final_state=rec.status,
            failure_reason=f"Already {rec.status}.",
        )

    # --- 3. APPROVED -> PROCESSING ---
    try:
        rec = approval_service.mark_processing(
            request_id, approver_id=approver_id
        )
    except approval_service.InvalidStateTransitionError as exc:
        return OrchestrationResult(
            request_id=request_id, success=False, final_state=rec.status,
            failure_reason=str(exc),
        )

    # --- 4. Resolve roles ---
    try:
        role_service.resolve_rank_roles(rec.department, rec.rank)
    except role_service.RoleConfigError as exc:
        await _mark_failed_with_audit(rec, approver_id, exc, discord_result=None, sheets_outcome="")
        return OrchestrationResult(
            request_id=request_id, success=False, final_state=approval_service.FAILED,
            failure_reason=str(exc),
        )

    # --- 5. Discord reconcile ---
    discord_result: Optional[role_service.DiscordResult] = None
    try:
        discord_result = await role_service.reconcile_roles(
            guild_member, rec.department, rec.rank
        )
    except Exception as exc:  # noqa: BLE001
        await _mark_failed_with_audit(rec, approver_id, exc, discord_result=None, sheets_outcome="")
        return OrchestrationResult(
            request_id=request_id, success=False, final_state=approval_service.FAILED,
            failure_reason=f"Discord reconcile crashed: {exc}",
        )

    if not discord_result.success:
        # Discord itself failed (e.g. missing permission for a role).
        # Sheets was never touched — we mark FAILED with no partial state.
        await _mark_failed_with_audit(rec, approver_id, "", discord_result=discord_result, sheets_outcome="")
        return OrchestrationResult(
            request_id=request_id, success=False, final_state=approval_service.FAILED,
            discord_result=discord_result,
            failure_reason="Discord role assignment failed: " + "; ".join(discord_result.errors),
        )

    # --- 6. Sheets upsert ---
    sheets_outcome = ""
    failure_reason = ""
    try:
        outcome = personnel_service.upsert(
            discord_id=str(rec.target_discord_id),
            target_discord_username=rec.target_username,
            department=rec.department,
            rank=rec.rank,
            hire_date=personnel_service.today_iso("UTC"),
        )
        sheets_outcome = outcome.outcome
        if outcome.outcome == "CONFLICT_MULTIPLE":
            failure_reason = (
                f"Discord ID {rec.target_discord_id} found in multiple tabs: "
                f"{', '.join(outcome.conflicting_tabs)}. NOT modifying any roster."
            )
        elif outcome.outcome == "NOT_FOUND_TAB":
            failure_reason = f"Could not find/create roster tab for department {rec.department!r}."
    except Exception as exc:  # noqa: BLE001
        failure_reason = f"Sheets upsert crashed: {exc}"

    if failure_reason:
        # Discord succeeded but Sheets did not. PARTIAL failure.
        try:
            approval_service.mark_failed(
                request_id,
                result_metadata={
                    "discord": discord_result.role_ids_added,
                    "sheets_outcome": sheets_outcome,
                    "sheets_error": failure_reason,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not mark request FAILED after Sheets error")
        _audit(rec, approver_id, discord_result, sheets_outcome, "PARTIAL", failure_reason)
        return OrchestrationResult(
            request_id=request_id, success=False, final_state=approval_service.FAILED,
            discord_result=discord_result, sheets_outcome=sheets_outcome,
            failure_reason=failure_reason, audit_overall="PARTIAL",
        )

    # --- 7. COMPLETED ---
    try:
        approval_service.mark_completed(
            request_id,
            result_metadata={
                "discord": discord_result.role_ids_added,
                "sheets_outcome": sheets_outcome,
            },
        )
    except Exception as exc:  # noqa: BLE001 — extremely rare
        _audit(rec, approver_id, discord_result, sheets_outcome, "PARTIAL",
               f"Discord+Sheets ok but mark_completed failed: {exc}")
        return OrchestrationResult(
            request_id=request_id, success=False, final_state="UNKNOWN",
            discord_result=discord_result, sheets_outcome=sheets_outcome,
            failure_reason=f"mark_completed crashed: {exc}", audit_overall="PARTIAL",
        )

    _audit(rec, approver_id, discord_result, sheets_outcome, "SUCCESS", "")
    return OrchestrationResult(
        request_id=request_id, success=True, final_state=approval_service.COMPLETED,
        discord_result=discord_result, sheets_outcome=sheets_outcome,
        audit_overall="SUCCESS",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mark_failed_with_audit(
    rec: approval_service.RoleRequestRecord,
    approver_id: int,
    err: Any,
    *,
    discord_result: Optional[role_service.DiscordResult],
    sheets_outcome: str,
) -> None:
    try:
        approval_service.mark_failed(
            rec.request_id,
            approver_id=approver_id,
            result_metadata={
                "discord": (discord_result.role_ids_added if discord_result else []),
                "sheets_outcome": sheets_outcome,
                "error": str(err),
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("mark_failed crashed")
    _audit(rec, approver_id, discord_result, sheets_outcome, "FAILURE", str(err))


def _audit(
    rec: approval_service.RoleRequestRecord,
    approver_id: int,
    discord_result: Optional[role_service.DiscordResult],
    sheets_outcome: str,
    overall: str,
    failure_reason: str,
) -> None:
    entry = audit_service.AuditEntry(
        request_id=rec.request_id,
        action="ROLE_REQUEST_APPROVED" if overall == "SUCCESS" else "ROLE_REQUEST_FAILED",
        target_discord_id=rec.target_discord_id,
        target_username=rec.target_username,
        prev_rank="",  # we don't have a "before" snapshot here; out of scope for v1
        new_rank=rec.rank,
        prev_department="",
        new_department=rec.department,
        requester_id=rec.requester_id,
        approver_id=approver_id,
        discord_result=discord_result.as_human() if discord_result else "",
        sheets_result=sheets_outcome or "",
        overall_result=overall,
        failure_reason=failure_reason or "",
    )
    audit_service.append_audit(entry)
