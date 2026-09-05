"""
config.settings
---------------
Application settings loaded from environment variables / .env file.

This module is the ONLY place that reads from the environment. All other
modules should import `get_settings()` and consume the resulting object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration. Backed by environment variables (or `.env`)."""

    # --- Discord ---
    discord_token: str = Field(..., alias="DISCORD_TOKEN")
    guild_id: int = Field(..., alias="GUILD_ID")
    hr_role_id: int = Field(..., alias="HR_ROLE_ID")

    # --- Google Sheets ---
    google_credentials_path: Path = Field(..., alias="GOOGLE_CREDENTIALS_PATH")
    target_spreadsheet_id: str = Field(..., alias="TARGET_SPREADSHEET_ID")
    nova_spreadsheet_id: str = Field(..., alias="NOVA_SPREADSHEET_ID")

    # --- Misc ---
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    timezone: str = Field("Asia/Kolkata", alias="TIMEZONE")

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Convenience derived paths ---

    @property
    def credentials_file(self) -> Path:
        """Absolute path to the service-account JSON key file."""
        path = Path(self.google_credentials_path)
        if path.is_absolute():
            return path
        return (PROJECT_ROOT / path).resolve()

    @property
    def role_mapping_path(self) -> Path:
        return PROJECT_ROOT / "config" / "role_mapping.yaml"

    @property
    def org_structure_path(self) -> Path:
        return PROJECT_ROOT / "config" / "org_structure.yaml"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (loads .env on first call).

    We expose this via a function (instead of a module-level instance) so
    tests can monkey-patch `get_settings.cache_clear()` between runs.
    """
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Clear the lru_cache — useful for tests and for hot-reloading .env."""
    get_settings.cache_clear()
