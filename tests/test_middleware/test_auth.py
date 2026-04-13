"""Tests for API key authentication middleware."""

import hashlib
import uuid
from unittest.mock import patch

from raasoa.middleware.auth import DEFAULT_TENANT, _get_env_key_map, _hash_key


class TestHashKey:
    def test_is_sha256(self) -> None:
        key = "sk-test-key-123"
        expected = hashlib.sha256(key.encode()).hexdigest()
        assert _hash_key(key) == expected

    def test_deterministic(self) -> None:
        assert _hash_key("sk-key") == _hash_key("sk-key")

    def test_different_keys(self) -> None:
        assert _hash_key("key-a") != _hash_key("key-b")


class TestEnvKeyMap:
    def test_parses_correctly(self) -> None:
        import raasoa.middleware.auth as auth_mod

        auth_mod._env_key_map = None
        with patch.object(
            auth_mod, "settings",
            api_keys="sk-test:00000000-0000-0000-0000-000000000001,"
            "sk-other:00000000-0000-0000-0000-000000000002",
        ):
            key_map = _get_env_key_map()
            assert "sk-test" in key_map
            assert key_map["sk-test"] == uuid.UUID(
                "00000000-0000-0000-0000-000000000001"
            )
            assert "sk-other" in key_map
        auth_mod._env_key_map = None

    def test_handles_empty(self) -> None:
        import raasoa.middleware.auth as auth_mod

        auth_mod._env_key_map = None
        with patch.object(auth_mod, "settings", api_keys=""):
            key_map = _get_env_key_map()
            assert key_map == {}
        auth_mod._env_key_map = None

    def test_skips_invalid(self) -> None:
        import raasoa.middleware.auth as auth_mod

        auth_mod._env_key_map = None
        with patch.object(
            auth_mod, "settings",
            api_keys="sk-good:00000000-0000-0000-0000-000000000001,bad,",
        ):
            key_map = _get_env_key_map()
            assert "sk-good" in key_map
            assert len(key_map) == 1
        auth_mod._env_key_map = None


def test_default_tenant_is_valid_uuid() -> None:
    assert isinstance(DEFAULT_TENANT, uuid.UUID)
