"""Regression tests for the dashboard auth-bypass fix (F-001).

Before this fix, `_check_auth` returned "OK" whenever DASHBOARD_PASSWORD
was unset — which is the shipped default — regardless of AUTH_ENABLED.
That let any anonymous caller mint real tenant API keys via
POST /dashboard/api/keys on a deployment that had turned AUTH_ENABLED=true
(the flag meant to signal "this is a real deployment, enforce auth").

Most of these tests don't need Postgres — the misconfigured/blocked paths
never reach the database. The two that authenticate all the way through
to a rendered dashboard page do, and are skipped when it's unreachable.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    try:
        async def _check() -> bool:
            engine = create_async_engine(DATABASE_URL)
            try:
                async with engine.connect() as conn:
                    await conn.execute(sql_text("SELECT 1"))
                return True
            finally:
                await engine.dispose()

        return asyncio.run(_check())
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}",
)


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    """Tests that reach an authenticated dashboard page exercise the
    app's global raasoa.db engine; dispose it around each test to avoid
    cross-event-loop asyncpg errors (same rationale as
    tests/test_api/test_admin_api.py)."""
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def _auth_enabled_no_password() -> AsyncGenerator[None, None]:
    """AUTH_ENABLED=true, DASHBOARD_PASSWORD unset — the exact
    misconfigured-but-previously-open state from F-001."""
    import raasoa.config as config_module

    original_auth = config_module.settings.auth_enabled
    original_password = config_module.settings.dashboard_password
    config_module.settings.auth_enabled = True
    config_module.settings.dashboard_password = ""
    yield
    config_module.settings.auth_enabled = original_auth
    config_module.settings.dashboard_password = original_password


@pytest.fixture
async def _auth_enabled_with_password() -> AsyncGenerator[str, None]:
    import raasoa.config as config_module

    original_auth = config_module.settings.auth_enabled
    original_password = config_module.settings.dashboard_password
    config_module.settings.auth_enabled = True
    config_module.settings.dashboard_password = "correct-horse-battery-staple"
    yield config_module.settings.dashboard_password
    config_module.settings.auth_enabled = original_auth
    config_module.settings.dashboard_password = original_password


async def test_anonymous_key_mint_blocked_when_misconfigured(
    _auth_enabled_no_password: None,
) -> None:
    """The exact escalation from F-001: with AUTH_ENABLED=true and no
    dashboard password configured, minting a key must fail, not succeed.
    (This route hardcodes 401 for any denial rather than propagating
    _check_auth's specific 503 — either way it must not return a key.)"""
    async with await _client() as client:
        resp = await client.post(
            "/dashboard/api/keys", json={"name": "anon-test"},
        )
    assert resp.status_code in (401, 503)
    assert "key" not in resp.json()


async def test_dashboard_home_blocked_when_misconfigured(
    _auth_enabled_no_password: None,
) -> None:
    async with await _client() as client:
        resp = await client.get("/dashboard")
    assert resp.status_code == 503


async def test_login_page_does_not_redirect_loop_when_misconfigured(
    _auth_enabled_no_password: None,
) -> None:
    """Previously /dashboard/login would bounce straight back to
    /dashboard when no password was set, which combined with a fixed
    _check_auth would just loop. It must now report the misconfiguration
    instead of redirecting."""
    async with await _client() as client:
        resp = await client.get("/dashboard/login")
    assert resp.status_code == 503


@requires_db
async def test_dashboard_open_when_auth_disabled() -> None:
    """AUTH_ENABLED=false (explicit dev/local trust-everyone mode, same
    flag used to disable API-key auth elsewhere) still leaves the
    dashboard open by default — this is unchanged, intentional behavior."""
    import raasoa.config as config_module

    original_auth = config_module.settings.auth_enabled
    original_password = config_module.settings.dashboard_password
    config_module.settings.auth_enabled = False
    config_module.settings.dashboard_password = ""
    try:
        async with await _client() as client:
            resp = await client.get("/dashboard")
        assert resp.status_code == 200
    finally:
        config_module.settings.auth_enabled = original_auth
        config_module.settings.dashboard_password = original_password


@requires_db
async def test_login_flow_works_when_password_configured(
    _auth_enabled_with_password: str,
) -> None:
    """The normal, correctly-configured path (AUTH_ENABLED=true +
    DASHBOARD_PASSWORD set) must keep working end-to-end."""
    password = _auth_enabled_with_password
    async with await _client() as client:
        blocked = await client.get("/dashboard", follow_redirects=False)
        assert blocked.status_code == 302
        assert blocked.headers["location"] == "/dashboard/login"

        bad_login = await client.post(
            "/dashboard/login", data={"password": "wrong"},
        )
        assert bad_login.status_code == 200
        assert "Invalid password" in bad_login.text

        good_login = await client.post(
            "/dashboard/login", data={"password": password},
            follow_redirects=False,
        )
        assert good_login.status_code == 302
        session_cookie = good_login.cookies.get("raasoa_session")
        assert session_cookie

        client.cookies.set("raasoa_session", session_cookie)
        authed = await client.get("/dashboard")
        assert authed.status_code == 200


async def test_empty_password_submission_never_authenticates(
    _auth_enabled_no_password: None,
) -> None:
    """Guards against the edge case where an empty DASHBOARD_PASSWORD
    setting could match an empty submitted form field."""
    async with await _client() as client:
        resp = await client.post("/dashboard/login", data={"password": ""})
    assert resp.status_code != 302
