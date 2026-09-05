# DCRP-Roster

A Discord ↔ Google Sheets personnel-management bot for a GTA RP law-enforcement
organization (DCRP). The Discord server is the operational interface; Google
Sheets is the personnel / administrative database.

> **Data integrity is the design center.** Every personnel change is gated on
> HR approval, every change is audited, every upsert is idempotent, and the
> system never silently overwrites a column it doesn't own.

---

## What it does

- `/role_request` — a user is filed for a rank/department, the bot asks an
  HR-staff approver to confirm, and on approval the bot assigns Discord
  roles, upserts the roster row by Discord ID, and writes an audit entry.
- No personnel data is copied from the reference (Nova) sheet — only its
  structure is cloned.
- All state lives in the Target Google Sheet (one tab per logical table, plus
  `PENDING REQUESTS` and `AUDIT_LOG` for in-flight requests and history).

---

## Architecture

```
cogs/         → Discord slash commands + button views
services/     → Application logic (approval, personnel, role, audit)
sheets/       → Google Sheets CRUD primitives (no business logic)
config/       → Settings (env) + org structure (YAML) + role mapping (YAML)
bot.py        → Entry point: loads cogs, syncs, logs ready
```

The strict layering means **no raw Sheets calls in cogs** and **no
hard-coded role IDs anywhere**. Add a new workflow → drop a service module +
cog, don't touch the rest.

---

## Prerequisites

- Python 3.11+ on Windows / macOS / Linux
- A Discord application + bot token (https://discord.com/developers/applications)
- A Google Cloud project with the Sheets API enabled
- A Google **service account** (JSON key file) with **Editor** access to the
  Target spreadsheet
- (Reference only) the Nova spreadsheet, shared read-only with the same
  service account

---

## Setup

```bash
# 1. Clone and enter
git clone https://github.com/AradhyaMaheshwari-bit/DCRP-Roster.git
cd DCRP-Roster

# 2. Project-local virtual environment (NEVER install deps globally)
py -m venv .venv                       # Windows
python3 -m venv .venv                  # macOS / Linux
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

# 3. Secrets — copy and fill in
cp .env.example .env                   # macOS / Linux
copy .env.example .env                 # Windows
# Edit .env with DISCORD_TOKEN, GUILD_ID, HR_ROLE_ID, GOOGLE_CREDENTIALS_PATH
mkdir secrets
# Drop the service-account JSON at secrets/google-sa.json

# 4. Share the Target spreadsheet with the service-account email
#    (Editor permission). Share Nova with the same email (Viewer).

# 5. Bootstrap the Target spreadsheet (creates 18 tabs + static reference data).
#    NEVER touches Nova.
.\.venv\Scripts\python -m sheets.bootstrap

# 6. Run the bot
.\.venv\Scripts\python bot.py
```

---

## Environment variables

| Variable                  | Purpose                                                    |
|---------------------------|------------------------------------------------------------|
| `DISCORD_TOKEN`           | Bot token                                                  |
| `GUILD_ID`                | Guild the bot is registered to                             |
| `HR_ROLE_ID`              | Discord role ID allowed to approve role requests           |
| `GOOGLE_CREDENTIALS_PATH` | Path to the service-account JSON key file                  |
| `TARGET_SPREADSHEET_ID`   | Production spreadsheet (the one the bot writes to)         |
| `NOVA_SPREADSHEET_ID`     | Reference spreadsheet (read-only, never written to)        |
| `LOG_LEVEL`               | Python logging level (default `INFO`)                      |
| `TIMEZONE`                | Timezone for date stamps in Sheets (default `Asia/Kolkata`)|

---

## Sheets layout (Target)

The Target spreadsheet is bootstrapped from `config/org_structure.yaml`.
Eight of the eighteen tabs are fully structured (from the captured Nova
content); the other ten are placeholders the coordinator fills in a follow-up
commit. **No personnel data is ever copied from Nova.**

State tabs (created automatically on first use):
- `PENDING REQUESTS` — one row per in-flight or completed `role_request`
- `AUDIT_LOG` — append-only log of every personnel change

---

## Commands

- `/role_request` — file a role/department request, routes to an HR approver.

Additional workflows (promotions, transfers, LOA, certifications,
armoury/vehicles, lookup) are scaffolded as service interfaces in
`services/` but not yet implemented as commands.

---

## Development

```bash
# Run all tests
.\.venv\Scripts\python -m pytest -q

# Run only offline tests (no Sheets/Discord calls)
.\.venv\Scripts\python -m pytest -q -m "not remote"
```

Tests that require a live Sheets/Discord backend are gated behind a
`MOCK_REMOTE=1` env var.

---

## Security

- `.env` and `secrets/` are git-ignored. Never commit them.
- No destructive git operations (`reset --hard`, force pushes, `clean -fd`)
  are used by the build scripts.
- The Nova spreadsheet is **never** written to by the bot.

---

## License

Internal project — not yet licensed for redistribution.
