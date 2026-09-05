"""
cogs.role_request
-----------------
The `/role_request` slash command and its approval/rejection buttons.

The cog is intentionally thin: it parses user input, calls
`services.approval_service` to create a PENDING request, posts an embed
with Approve/Reject buttons, and (in the persistent view) routes button
clicks to the orchestrator. No Sheets / Discord / role-resolution logic
lives here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View

from config import get_settings
from services import (
    approval_service,
    permissions,
    role_request_orchestrator,
)
from services.approval_service import (
    PENDING, REJECTED, FAILED, COMPLETED,
)
from services.role_request_orchestrator import OrchestrationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Choices (Departments, Ranks) — derived from role_mapping.yaml keys.
# We expose canonical departments here; ranks are passed as free-form
# strings (the role_mapping.yaml is the source of truth for valid combos).
# ---------------------------------------------------------------------------

DEPARTMENT_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="SASP", value="SASP"),
    app_commands.Choice(name="LSPD", value="LSPD"),
    app_commands.Choice(name="BCSO", value="BCSO"),
    app_commands.Choice(name="SASPR", value="SASPR"),
    app_commands.Choice(name="SAHP", value="SAHP"),
    app_commands.Choice(name="DOC", value="DOC"),
    app_commands.Choice(name="SWAT", value="SWAT"),
    app_commands.Choice(name="CID", value="CID"),
    # Sub-departments share the SUB DEPARTMENT ROSTER tab.
    app_commands.Choice(name="HR", value="HR"),
    app_commands.Choice(name="MPU", value="MPU"),
    app_commands.Choice(name="PIU", value="PIU"),
    app_commands.Choice(name="AIR", value="AIR"),
    app_commands.Choice(name="K9", value="K9"),
    app_commands.Choice(name="GIU", value="GIU"),
    app_commands.Choice(name="FTD", value="FTD"),
    app_commands.Choice(name="SEU", value="SEU"),
    app_commands.Choice(name="WING", value="WING"),
    app_commands.Choice(name="MBU", value="MBU"),
    app_commands.Choice(name="ACADEMY", value="ACADEMY"),
]


# ---------------------------------------------------------------------------
# Approval embed view
# ---------------------------------------------------------------------------

class _RoleRequestView(View):
    """Persistent view for Approve / Reject buttons on a role-request embed."""

    def __init__(self, request_id: str) -> None:
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="rr:approve",
    )
    async def _approve(self, interaction: discord.Interaction, _button: Button) -> None:
        await _handle_approval_click(interaction, self.request_id, approve=True)

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="rr:reject",
    )
    async def _reject(self, interaction: discord.Interaction, _button: Button) -> None:
        await _handle_approval_click(interaction, self.request_id, approve=False)


# ---------------------------------------------------------------------------
# Click handler
# ---------------------------------------------------------------------------

async def _handle_approval_click(
    interaction: discord.Interaction,
    request_id: str,
    *,
    approve: bool,
) -> None:
    # 1. Permission gate.
    if not permissions.is_authorized_approver(interaction.user):
        await interaction.response.send_message(
            "You don't have the HR role required to approve requests.",
            ephemeral=True,
        )
        return

    # Acknowledge immediately to avoid the 3-second timeout.
    await interaction.response.defer(ephemeral=False, thinking=True)

    if approve:
        # Resolve the target guild member from the request.
        rec = approval_service.get_request(request_id)
        if not rec:
            await interaction.followup.send(
                f"Request {request_id} not found.", ephemeral=True,
            )
            return
        # Idempotency: if the request is already terminal, just report it.
        if approval_service.is_terminal(rec.status):
            await interaction.followup.send(
                f"Request {request_id} already {rec.status}.", ephemeral=True,
            )
            return

        # Look up the guild member by target ID.
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Cannot resolve guild for this interaction.", ephemeral=True,
            )
            return
        member = guild.get_member(rec.target_discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(rec.target_discord_id)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            await interaction.followup.send(
                f"Target user (ID {rec.target_discord_id}) is not in this guild.",
                ephemeral=True,
            )
            return

        # 2. Mark APPROVED first (so the embed flips to "approving...").
        try:
            approval_service.transition(
                request_id, from_states={PENDING}, to_state="APPROVED",
                updates={"approver_id": interaction.user.id},
            )
        except approval_service.InvalidStateTransitionError:
            # Someone else already acted.
            await interaction.followup.send(
                f"Request {request_id} was already acted on.", ephemeral=True,
            )
            return

        # 3. Run the orchestrator (this is the heavy lift).
        result: OrchestrationResult = await role_request_orchestrator.orchestrate_approval(
            request_id, approver_id=interaction.user.id, guild_member=member,
        )

        # 4. Update the original embed with the final result.
        await _update_approval_embed(interaction, result, approve=True)
    else:
        # Reject path.
        try:
            approval_service.mark_rejected(
                request_id,
                approver_id=interaction.user.id,
            )
        except approval_service.InvalidStateTransitionError:
            await interaction.followup.send(
                f"Request {request_id} was already acted on.", ephemeral=True,
            )
            return
        rec = approval_service.get_request(request_id)
        if rec is None:
            await interaction.followup.send("Request vanished.", ephemeral=True)
            return
        # We don't have the original embed here, so post a followup summary.
        await interaction.followup.send(
            embed=_build_rejected_embed(rec, interaction.user),
        )


async def _update_approval_embed(
    interaction: discord.Interaction,
    result: OrchestrationResult,
    *,
    approve: bool,
) -> None:
    """Update the original approval message with the final outcome."""
    color = (
        discord.Color.green() if result.success
        else discord.Color.orange() if result.audit_overall == "PARTIAL"
        else discord.Color.red()
    )
    title = "✅ Request approved" if result.success else "⚠️ Approval did not complete"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Request ID", value=result.request_id, inline=False)
    embed.add_field(name="Final state", value=result.final_state, inline=True)
    if result.sheets_outcome:
        embed.add_field(name="Sheets", value=result.sheets_outcome, inline=True)
    if result.discord_result is not None:
        embed.add_field(
            name="Discord",
            value=(
                f"added={result.discord_result.role_ids_added or '—'} "
                f"removed={result.discord_result.role_ids_removed or '—'}"
            ),
            inline=False,
        )
    if result.failure_reason:
        embed.add_field(
            name="Failure reason",
            value=result.failure_reason[:1000],
            inline=False,
        )
    if result.audit_overall:
        embed.add_field(name="Audit", value=result.audit_overall, inline=True)
    # Edit the original message (it's the embed with the buttons).
    try:
        await interaction.edit_original_response(embed=embed, view=None)
    except discord.HTTPException:
        # Fall back to followup if edit fails (e.g. message was deleted).
        await interaction.followup.send(embed=embed, ephemeral=False)


def _build_rejected_embed(
    rec: approval_service.RoleRequestRecord,
    approver: discord.abc.User,
) -> discord.Embed:
    embed = discord.Embed(title="❌ Request rejected", color=discord.Color.red())
    embed.add_field(name="Request ID", value=rec.request_id, inline=False)
    embed.add_field(name="Rejected by", value=str(approver), inline=True)
    embed.set_footer(text=f"Target: {rec.target_username} (ID {rec.target_discord_id})")
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class RoleRequestCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Register the persistent view so buttons keep working across restarts.
        # The view is registered with a placeholder request_id="*" — but
        # discord.py 2.x's persistent views are bound to specific custom_ids
        # and don't actually need the request_id at registration time.
        # We do a no-op registration here; the click handler still validates
        # the request_id from the request state.
        self.bot.add_view(_RoleRequestView(request_id="*"))

    @app_commands.command(
        name="role_request",
        description=(
            "File a role/department request for a Discord member. "
            "Routes to an HR approver."
        ),
    )
    @app_commands.describe(
        user="The member this request is for.",
        department="The department.",
        rank="The rank (free-form; validated against role_mapping.yaml).",
        approved_by="The HR staff member expected to approve.",
    )
    @app_commands.choices(department=DEPARTMENT_CHOICES)
    async def role_request(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        department: app_commands.Choice[str],
        rank: str,
        approved_by: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)

        # --- Permission gate: requester must be allowed to file ---
        # v1: anyone in the same guild can file. Future: restrict to a
        # "can-request-for-others" allowlist.
        if interaction.guild is None:
            await interaction.followup.send("This command only works in a guild.")
            return

        # --- Department may be passed as Choice.value or a raw string ---
        dept = department.value if isinstance(department, app_commands.Choice) else str(department)

        # --- Create the PENDING request ---
        rec = approval_service.create_request(
            requester_id=interaction.user.id,
            target_discord_id=user.id,
            target_username=f"{user.name}#{user.discriminator}" if user.discriminator else user.name,
            department=dept,
            rank=rank,
            approved_by_discord_id=approved_by.id,
        )

        # --- Post approval embed ---
        embed = _build_pending_embed(rec, user, interaction.user, approved_by)
        view = _RoleRequestView(request_id=rec.request_id)
        await interaction.followup.send(
            content=f"<@{approved_by.id}> — approval needed.",
            embed=embed,
            view=view,
        )


def _build_pending_embed(
    rec: approval_service.RoleRequestRecord,
    target: discord.abc.User,
    requester: discord.abc.User,
    approver: discord.abc.User,
) -> discord.Embed:
    embed = discord.Embed(
        title="🟡 Role request — pending approval",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Request ID", value=rec.request_id, inline=False)
    embed.add_field(
        name="Target",
        value=f"{target.mention} (`{target.id}`)",
        inline=False,
    )
    embed.add_field(name="Department", value=rec.department, inline=True)
    embed.add_field(name="Rank", value=rec.rank, inline=True)
    embed.add_field(
        name="Requested by",
        value=f"{requester.mention}",
        inline=True,
    )
    embed.add_field(
        name="Expected approver",
        value=f"{approver.mention}",
        inline=True,
    )
    embed.set_footer(
        text="Only users with the HR role can approve / reject this request."
    )
    return embed


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleRequestCog(bot))
    logger.info("cogs.role_request loaded")
