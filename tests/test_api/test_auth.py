"""Tests for authentication and tenant isolation."""

import uuid

import pytest

from raasoa.middleware.auth import (
    DEFAULT_TENANT,
    resolve_tenant,
    verify_webhook_secret,
)


class FakeRequest:
    """Minimal request mock for auth tests."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class TestResolveTenanAuthDisabled:
    def test_returns_default_tenant_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("raasoa.middleware.auth.settings.auth_enabled", False)
        req = FakeRequest()
        tid = resolve_tenant(req)  # type: ignore[arg-type]
        assert tid == DEFAULT_TENANT

    def test_returns_default_regardless_of_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("raasoa.middleware.auth.settings.auth_enabled", False)
        req = FakeRequest({"authorization": "Bearer garbage"})
        tid = resolve_tenant(req)  # type: ignore[arg-type]
        assert tid == DEFAULT_TENANT


_TEST_TENANT = "00000000-0000-0000-0000-000000000001"
_AUTH = "raasoa.middleware.auth.settings"


class TestResolveTenanAuthEnabled:
    def test_missing_key_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{_AUTH}.auth_enabled", True)
        monkeypatch.setattr(f"{_AUTH}.api_keys", f"sk-test:{_TEST_TENANT}")
        monkeypatch.setattr("raasoa.middleware.auth._key_map", None)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            resolve_tenant(FakeRequest())  # type: ignore[arg-type]
        assert exc.value.status_code == 401

    def test_wrong_key_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{_AUTH}.auth_enabled", True)
        monkeypatch.setattr(f"{_AUTH}.api_keys", f"sk-valid:{_TEST_TENANT}")
        monkeypatch.setattr("raasoa.middleware.auth._key_map", None)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            resolve_tenant(FakeRequest({"authorization": "Bearer sk-wrong"}))  # type: ignore[arg-type]
        assert exc.value.status_code == 401

    def test_correct_key_returns_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tid = "11111111-1111-1111-1111-111111111111"
        monkeypatch.setattr("raasoa.middleware.auth.settings.auth_enabled", True)
        monkeypatch.setattr("raasoa.middleware.auth.settings.api_keys", f"sk-good:{tid}")
        monkeypatch.setattr("raasoa.middleware.auth._key_map", None)

        result = resolve_tenant(FakeRequest({"authorization": "Bearer sk-good"}))  # type: ignore[arg-type]
        assert result == uuid.UUID(tid)

    def test_x_api_key_header_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tid = "22222222-2222-2222-2222-222222222222"
        monkeypatch.setattr("raasoa.middleware.auth.settings.auth_enabled", True)
        monkeypatch.setattr("raasoa.middleware.auth.settings.api_keys", f"sk-alt:{tid}")
        monkeypatch.setattr("raasoa.middleware.auth._key_map", None)

        result = resolve_tenant(FakeRequest({"x-api-key": "sk-alt"}))  # type: ignore[arg-type]
        assert result == uuid.UUID(tid)


class TestWebhookSecret:
    def test_no_secret_auth_disabled_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(f"{_AUTH}.webhook_secret", "")
        monkeypatch.setattr(f"{_AUTH}.auth_enabled", False)
        verify_webhook_secret(FakeRequest())  # type: ignore[arg-type]

    def test_no_secret_auth_enabled_blocks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(f"{_AUTH}.webhook_secret", "")
        monkeypatch.setattr(f"{_AUTH}.auth_enabled", True)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            verify_webhook_secret(FakeRequest())  # type: ignore[arg-type]
        assert exc.value.status_code == 401

    def test_correct_secret_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(f"{_AUTH}.webhook_secret", "whsec-test")
        req = FakeRequest({"x-webhook-secret": "whsec-test"})
        verify_webhook_secret(req)  # type: ignore[arg-type]

    def test_wrong_secret_blocks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(f"{_AUTH}.webhook_secret", "whsec-real")

        from fastapi import HTTPException
        req = FakeRequest({"x-webhook-secret": "whsec-fake"})
        with pytest.raises(HTTPException) as exc:
            verify_webhook_secret(req)  # type: ignore[arg-type]
        assert exc.value.status_code == 401
