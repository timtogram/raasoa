"""Regression test for the fail-closed identity resolution fix (F-017).

Before this fix, resolve_principal_async caught nothing around
_resolve_key_row_from_db (which itself swallowed every exception and
returned None), so a scoped personal key's identity query failing with a
genuine infrastructure error (DB blip, timeout) fell through to the
final branch and returned an unfiltered legacy-admin Principal — the
same outcome as "this key has no principal_id". A transient DB error
must not silently upgrade a restricted key to admin.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

import raasoa.security.principal as principal_mod
from raasoa.config import settings


def _request_with_bearer(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _auth_enabled() -> None:
    original = settings.auth_enabled
    settings.auth_enabled = True
    yield
    settings.auth_enabled = original


async def test_db_error_during_identity_lookup_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(_api_key: str) -> None:
        raise ConnectionError("simulated DB outage")

    monkeypatch.setattr(principal_mod, "_resolve_key_row_from_db", _raise)
    monkeypatch.setattr(principal_mod, "_get_env_key_map", lambda: {})

    request = _request_with_bearer("sk-some-personal-key")

    with pytest.raises(Exception) as exc_info:
        await principal_mod.resolve_principal_async(request)

    # Must be a 503 (service unavailable), never a resolved Principal —
    # and specifically not the legacy-tenant-wide/is_admin=True shape.
    assert getattr(exc_info.value, "status_code", None) == 503


async def test_successful_lookup_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check the fixture/monkeypatch scaffolding: a clean
    successful row still resolves normally (not every path raises)."""
    import uuid

    tenant_id = uuid.uuid4()

    async def _ok(_api_key: str) -> tuple[uuid.UUID, str | None, str, bool]:
        return (tenant_id, "user:jane", "internal", False)

    monkeypatch.setattr(principal_mod, "_resolve_key_row_from_db", _ok)
    monkeypatch.setattr(principal_mod, "_get_env_key_map", lambda: {})

    request = _request_with_bearer("sk-some-personal-key")
    result = await principal_mod.resolve_principal_async(request)

    assert result.tenant_id == tenant_id
    assert result.principal_id == "user:jane"
    assert result.is_legacy_tenant_wide is False
    assert result.is_admin is False
