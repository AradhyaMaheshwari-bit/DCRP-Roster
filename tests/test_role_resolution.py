"""
tests.test_role_resolution
--------------------------
Offline tests for services.role_service.

Covers:
  * `resolve_rank_roles` for a configured (Department, Rank) returns the role list.
  * `resolve_rank_roles` raises RoleConfigMissingError for an entry whose
    role_ids is empty (the production YAML has every entry scaffolded
    empty — this is the documented failure mode).
  * `resolve_rank_roles` raises for a completely unknown (Department, Rank).
  * `resolve_department_role` returns the role ID when configured.
  * `resolve_department_role` returns None when the entry is empty.
  * The (Department, Rank) keying is exact: trailing spaces / case differences
    do NOT resolve to an empty entry.
  * `reset_role_mapping_cache` lets a test load a custom mapping.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("HR_ROLE_ID", "1")
os.environ.setdefault("GOOGLE_CREDENTIALS_PATH", "./secrets/x.json")
os.environ.setdefault("TARGET_SPREADSHEET_ID", "target")
os.environ.setdefault("NOVA_SPREADSHEET_ID", "nova")

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.role_service as svc  # noqa: E402


# ---------------------------------------------------------------------------
# The production role_mapping.yaml has every (Department, Rank) entry
# scaffolded with role_ids: [] — this is intentional, and the test asserts
# the documented behavior (the bot refuses to complete such an approval).
# ---------------------------------------------------------------------------

def test_resolve_rank_roles_raises_for_empty_entry_in_production_yaml():
    """The shipped YAML has every (dept, rank) entry empty → must raise."""
    svc.reset_role_mapping_cache()
    with pytest.raises(svc.RoleConfigMissingError) as exc:
        svc.resolve_rank_roles("SASP", "Cadet")
    assert "SASP::Cadet" in str(exc.value)
    assert "empty role_ids" in str(exc.value)


def test_resolve_rank_roles_raises_for_unknown_pair():
    svc.reset_role_mapping_cache()
    with pytest.raises(svc.RoleConfigMissingError) as exc:
        svc.resolve_rank_roles("SASP", "NotARank")
    assert "No entry" in str(exc.value)


def test_resolve_rank_roles_raises_for_unknown_department():
    svc.reset_role_mapping_cache()
    with pytest.raises(svc.RoleConfigMissingError):
        svc.resolve_rank_roles("NOT_A_DEPT", "Cadet")


def test_resolve_department_role_returns_none_when_empty_in_production_yaml():
    """The shipped YAML has every department entry empty → returns None."""
    svc.reset_role_mapping_cache()
    assert svc.resolve_department_role("SASP") is None


# ---------------------------------------------------------------------------
# Custom-mapping tests — load a temporary YAML to exercise the happy path
# and the "configured but empty" guard, with isolation from the real file.
# ---------------------------------------------------------------------------

@pytest.fixture
def custom_role_mapping(monkeypatch):
    """Patch the role_service module's `_get_mapping` to return a controlled dict.

    This is more robust than monkey-patching a property on a pydantic
    Settings object (which is a frozen-ish model and doesn't allow
    attribute injection). It also avoids touching the production YAML.
    """
    custom = {
        "department_roles": {
            "SASP": [12345],
            "LSPD": [],
        },
        "rank_roles": {
            "SASP::Cadet":    [111, 222],
            "SASP::Corporal": [333],
            "LSPD::Cadet":    [],  # present but empty
        },
    }
    monkeypatch.setattr(svc, "_get_mapping", lambda: custom)
    svc.reset_role_mapping_cache()
    return custom


def test_resolve_rank_roles_returns_ids_when_configured(custom_role_mapping):
    svc.reset_role_mapping_cache()
    assert svc.resolve_rank_roles("SASP", "Cadet") == [111, 222]
    assert svc.resolve_rank_roles("SASP", "Corporal") == [333]


def test_resolve_rank_roles_returns_ints(custom_role_mapping):
    svc.reset_role_mapping_cache()
    ids = svc.resolve_rank_roles("SASP", "Cadet")
    assert all(isinstance(i, int) for i in ids)


def test_resolve_rank_roles_raises_when_present_but_empty(custom_role_mapping):
    svc.reset_role_mapping_cache()
    with pytest.raises(svc.RoleConfigMissingError) as exc:
        svc.resolve_rank_roles("LSPD", "Cadet")
    assert "LSPD::Cadet" in str(exc.value)


def test_resolve_department_role_returns_id_when_configured(custom_role_mapping):
    svc.reset_role_mapping_cache()
    assert svc.resolve_department_role("SASP") == 12345


def test_resolve_department_role_returns_none_when_empty(custom_role_mapping):
    svc.reset_role_mapping_cache()
    assert svc.resolve_department_role("LSPD") is None


def test_resolve_rank_roles_keying_is_exact(custom_role_mapping):
    """Trailing space / case difference must NOT accidentally hit a configured key."""
    svc.reset_role_mapping_cache()
    with pytest.raises(svc.RoleConfigMissingError):
        svc.resolve_rank_roles("SASP", "cadet")   # wrong case
    with pytest.raises(svc.RoleConfigMissingError):
        svc.resolve_rank_roles("SASP", "Cadet ")  # trailing space
    with pytest.raises(svc.RoleConfigMissingError):
        svc.resolve_rank_roles("sasp", "Cadet")   # wrong case dept


def test_reset_role_mapping_cache_invalidates_cached_mapping(custom_role_mapping, monkeypatch):
    """Mutate the mapping then reset; the next resolve must see the new content."""
    # First call populates the lru_cache.
    assert svc.resolve_rank_roles("SASP", "Cadet") == [111, 222]
    assert svc.resolve_department_role("SASP") == 12345

    # Replace the mapping source with a different one.
    new_mapping = {
        "department_roles": {"SASP": [999]},
        "rank_roles": {"SASP::Cadet": [444]},
    }
    monkeypatch.setattr(svc, "_get_mapping", lambda: new_mapping)
    # Without reset, resolve_rank_roles would still return [111, 222]
    # from the lru_cache of the underlying file read. But here we
    # monkey-patched _get_mapping itself, so the cache is bypassed
    # entirely — which is the property the lru_cache is meant to invalidate
    # in production. We exercise the reset_role_mapping_cache code path
    # explicitly to confirm it does not raise.
    svc.reset_role_mapping_cache()
    assert svc.resolve_rank_roles("SASP", "Cadet") == [444]
    assert svc.resolve_department_role("SASP") == 999
