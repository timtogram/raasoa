"""Tests for raasoa.security.crypto (F-046 follow-up).

sources.py's module docstring claimed connection_config (Notion tokens,
SharePoint client_secrets, Jira API tokens) was "encrypted in the
database", but Source.connection_config is plain JSONB with no
application-level encryption anywhere -- this closes that gap for the
known credential fields while leaving non-sensitive fields (site_id,
sync_interval_minutes, sync_acl) untouched, since several call sites
read those directly via Postgres JSONB path operators.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from raasoa.security.crypto import (
    SENSITIVE_KEYS,
    decrypt_sensitive_config,
    encrypt_sensitive_config,
)


class TestNoKeyConfigured:
    def test_encrypt_returns_plaintext_unchanged_when_no_key(
        self, monkeypatch,
    ) -> None:
        from raasoa.config import settings

        monkeypatch.setattr(settings, "connector_encryption_key", "")
        config = {"token": "secret-value", "site_id": "abc"}
        result = encrypt_sensitive_config(config)
        assert result == config

    def test_decrypt_passes_through_plaintext_unchanged(
        self, monkeypatch,
    ) -> None:
        from raasoa.config import settings

        monkeypatch.setattr(settings, "connector_encryption_key", "")
        config = {"token": "plain-token", "sync_interval_minutes": 60}
        assert decrypt_sensitive_config(config) == config


class TestWithKeyConfigured:
    def test_encrypt_then_decrypt_round_trips(self, monkeypatch) -> None:
        from raasoa.config import settings

        key = Fernet.generate_key().decode()
        monkeypatch.setattr(settings, "connector_encryption_key", key)

        config = {
            "token": "sk-real-secret-123",
            "client_secret": "another-secret",
            "site_id": "not-sensitive",
            "sync_interval_minutes": 30,
        }
        encrypted = encrypt_sensitive_config(config)

        # Sensitive fields must actually change and not be plaintext.
        assert encrypted["token"] != config["token"]
        assert encrypted["client_secret"] != config["client_secret"]
        assert encrypted["token"].startswith("enc:v1:")
        # Non-sensitive fields untouched -- these are read via JSONB path
        # operators directly in SQL and must remain plain.
        assert encrypted["site_id"] == "not-sensitive"
        assert encrypted["sync_interval_minutes"] == 30

        decrypted = decrypt_sensitive_config(encrypted)
        assert decrypted["token"] == config["token"]
        assert decrypted["client_secret"] == config["client_secret"]
        assert decrypted["site_id"] == "not-sensitive"

    def test_legacy_plaintext_rows_still_decrypt_as_plaintext(
        self, monkeypatch,
    ) -> None:
        """A row written before this feature existed (or while no key was
        configured) has no enc:v1: prefix -- must pass through
        unchanged, not be mistaken for corrupted ciphertext."""
        from raasoa.config import settings

        monkeypatch.setattr(
            settings, "connector_encryption_key", Fernet.generate_key().decode(),
        )
        legacy_config = {"token": "old-plaintext-token", "site_id": "x"}
        assert decrypt_sensitive_config(legacy_config) == legacy_config

    def test_wrong_key_does_not_raise_and_leaves_value_undecrypted(
        self, monkeypatch,
    ) -> None:
        from raasoa.config import settings

        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()

        monkeypatch.setattr(settings, "connector_encryption_key", key1)
        encrypted = encrypt_sensitive_config({"token": "secret"})

        monkeypatch.setattr(settings, "connector_encryption_key", key2)
        # Must not raise -- a wrong/rotated key shouldn't crash a sync.
        result = decrypt_sensitive_config(encrypted)
        # Can't decrypt with the wrong key -- left as the (still-prefixed,
        # undecryptable) ciphertext rather than silently returning
        # garbage as if it were the real credential.
        assert result["token"] == encrypted["token"]

    def test_empty_config_returns_empty_dict(self, monkeypatch) -> None:
        from raasoa.config import settings

        monkeypatch.setattr(
            settings, "connector_encryption_key", Fernet.generate_key().decode(),
        )
        assert encrypt_sensitive_config(None) == {}
        assert encrypt_sensitive_config({}) == {}
        assert decrypt_sensitive_config(None) == {}
        assert decrypt_sensitive_config({}) == {}

    def test_only_known_sensitive_keys_are_touched(self, monkeypatch) -> None:
        from raasoa.config import settings

        monkeypatch.setattr(
            settings, "connector_encryption_key", Fernet.generate_key().decode(),
        )
        config = {k: f"value-{k}" for k in SENSITIVE_KEYS}
        config["not_sensitive_field"] = "value-not_sensitive_field"
        encrypted = encrypt_sensitive_config(config)
        for k in SENSITIVE_KEYS:
            assert encrypted[k] != config[k]
        assert encrypted["not_sensitive_field"] == config["not_sensitive_field"]

    def test_reencrypting_already_encrypted_value_is_a_no_op(
        self, monkeypatch,
    ) -> None:
        """Calling encrypt twice on the same dict (e.g. saving a source
        that was already encrypted) must not double-wrap the value."""
        from raasoa.config import settings

        monkeypatch.setattr(
            settings, "connector_encryption_key", Fernet.generate_key().decode(),
        )
        once = encrypt_sensitive_config({"token": "secret"})
        twice = encrypt_sensitive_config(once)
        assert once["token"] == twice["token"]
        assert decrypt_sensitive_config(twice)["token"] == "secret"
