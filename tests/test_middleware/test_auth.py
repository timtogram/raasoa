"""Tests for API key authentication middleware."""

import uuid
from unittest.mock import patch

from raasoa.middleware.auth import DEFAULT_TENANT, _get_key_map


def test_key_map_parses_correctly() -> None:
    """API keys are correctly parsed into tenant map."""
    import raasoa.middleware.auth as auth_mod

    auth_mod._key_map = None  # Reset cache

    with patch.object(
        auth_mod, "settings",
        api_keys="sk-test:00000000-0000-0000-0000-000000000001,"
        "sk-other:00000000-0000-0000-0000-000000000002",
    ):
        key_map = _get_key_map()
        assert "sk-test" in key_map
        assert key_map["sk-test"] == uuid.UUID(
            "00000000-0000-0000-0000-000000000001"
        )
        assert "sk-other" in key_map

    auth_mod._key_map = None  # Reset after test


def test_key_map_handles_empty_config() -> None:
    import raasoa.middleware.auth as auth_mod

    auth_mod._key_map = None
    with patch.object(auth_mod, "settings", api_keys=""):
        key_map = _get_key_map()
        assert key_map == {}
    auth_mod._key_map = None


def test_key_map_skips_invalid_entries() -> None:
    import raasoa.middleware.auth as auth_mod

    auth_mod._key_map = None
    with patch.object(
        auth_mod, "settings",
        api_keys="sk-good:00000000-0000-0000-0000-000000000001,bad-entry,",
    ):
        key_map = _get_key_map()
        assert "sk-good" in key_map
        assert len(key_map) == 1
    auth_mod._key_map = None


def test_default_tenant_is_valid_uuid() -> None:
    assert isinstance(DEFAULT_TENANT, uuid.UUID)
