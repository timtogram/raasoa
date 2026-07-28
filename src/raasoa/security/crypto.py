"""Encryption for sensitive fields inside sources.connection_config.

connection_config stores connector credentials (a Notion integration
token, a SharePoint app registration's client_secret, a Jira API token)
alongside non-sensitive settings (site_id, drive_id, sync_interval_minutes,
sync_acl) in the same JSONB blob. Several call sites read the
non-sensitive fields directly via Postgres JSONB path operators (e.g.
``connection_config->>'sync_interval_minutes'`` in the scheduler's due
query), so encrypting the whole blob would break those without a much
larger refactor. Instead, only the known-sensitive keys are encrypted;
everything else stays in plaintext exactly as before.

Encrypted values are stored as ``"enc:v1:<fernet token>"`` so
``decrypt_sensitive_config`` can tell an already-encrypted value apart
from plaintext -- this makes the scheme backward-compatible with
existing rows written before this feature existed, and safe to deploy
without a data migration: old plaintext values are read as-is (never
mistaken for ciphertext), and any value written or re-saved afterward
gets encrypted going forward.
"""
from __future__ import annotations

import logging
from typing import Any

from raasoa.config import settings

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"

# Keys actually read as credentials by the connector sync code
# (src/raasoa/api/sources.py) -- see that module for the exact
# config.get(...) call sites this list is kept in sync with.
SENSITIVE_KEYS = frozenset({"token", "client_secret", "api_token"})

_warned_no_key = False


def _get_fernet() -> Any | None:
    if not settings.connector_encryption_key:
        global _warned_no_key
        if not _warned_no_key:
            logger.warning(
                "CONNECTOR_ENCRYPTION_KEY is not set -- connector "
                "credentials (Notion/SharePoint/Jira tokens and secrets) "
                "are stored in plaintext in the database. Set "
                "CONNECTOR_ENCRYPTION_KEY to enable encryption at rest; "
                "see DEPLOYMENT.md."
            )
            _warned_no_key = True
        return None

    from cryptography.fernet import Fernet

    return Fernet(settings.connector_encryption_key.encode())


def encrypt_sensitive_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Encrypt SENSITIVE_KEYS in ``config`` before storing it.

    Returns a new dict; ``config`` itself is not mutated. Non-string
    values and empty strings are left as-is (nothing meaningful to
    encrypt). If no encryption key is configured, returns ``config``
    unchanged (today's plaintext behavior, with a one-time warning).
    """
    if not config:
        return {}
    fernet = _get_fernet()
    if fernet is None:
        return dict(config)

    result = dict(config)
    for key in SENSITIVE_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value and not value.startswith(_ENC_PREFIX):
            result[key] = _ENC_PREFIX + fernet.encrypt(value.encode()).decode()
    return result


def decrypt_sensitive_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Decrypt any SENSITIVE_KEYS values previously encrypted by
    ``encrypt_sensitive_config``.

    Values without the ``enc:v1:`` prefix (plaintext -- either legacy
    rows from before this feature, or written while no key was
    configured) pass through unchanged. Returns a new dict.
    """
    if not config:
        return {}
    result = dict(config)
    encrypted_keys = [
        k for k in SENSITIVE_KEYS
        if isinstance(result.get(k), str) and result[k].startswith(_ENC_PREFIX)
    ]
    if not encrypted_keys:
        return result

    fernet = _get_fernet()
    if fernet is None:
        # A key WAS used to encrypt these values previously (they carry
        # the prefix) but none is configured now -- surface this loudly
        # rather than silently handing back an undecryptable token as if
        # it were the real credential.
        logger.error(
            "connection_config has encrypted fields but "
            "CONNECTOR_ENCRYPTION_KEY is not set -- cannot decrypt %s",
            encrypted_keys,
        )
        return result

    from cryptography.fernet import InvalidToken

    for key in encrypted_keys:
        raw = result[key][len(_ENC_PREFIX):]
        try:
            result[key] = fernet.decrypt(raw.encode()).decode()
        except InvalidToken:
            logger.error(
                "Failed to decrypt connection_config[%r] -- wrong "
                "CONNECTOR_ENCRYPTION_KEY, or the value was corrupted",
                key,
            )
    return result
