"""Regression test for the CORS wildcard+credentials fix (F-004).

Before this fix, the app always set allow_credentials=True regardless of
allow_origins, including the default wildcard (["*"]) case. Combined
with the cookie-authenticated dashboard, that let any origin drive
credentialed cross-origin requests against a logged-in dashboard user.
"""
from __future__ import annotations

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _reset_main_module() -> None:
    """main.py computes CORS settings at import time from
    settings.cors_origins; reload it fresh after each test so later test
    modules get the default (unset) configuration back."""
    yield
    import raasoa.config as config_module

    config_module.settings.cors_origins = ""
    import raasoa.main

    importlib.reload(raasoa.main)


async def test_wildcard_origin_does_not_allow_credentials() -> None:
    """Default config (CORS_ORIGINS unset): any origin is allowed for
    header-based API access, but credentialed (cookie) requests must not
    be reflected back as allowed."""
    import raasoa.config as config_module

    config_module.settings.cors_origins = ""
    import raasoa.main

    importlib.reload(raasoa.main)
    from raasoa.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get("/health", headers={"Origin": "https://evil.test"})

    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers


async def test_explicit_allowlist_permits_credentials_for_trusted_origin() -> None:
    """When the deployer explicitly configures CORS_ORIGINS, credentials
    are allowed for that trusted origin."""
    import raasoa.config as config_module

    config_module.settings.cors_origins = "https://trusted.example.com"
    import raasoa.main

    importlib.reload(raasoa.main)
    from raasoa.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get(
            "/health", headers={"Origin": "https://trusted.example.com"},
        )

    assert resp.headers.get("access-control-allow-origin") == "https://trusted.example.com"
    assert resp.headers.get("access-control-allow-credentials") == "true"


async def test_explicit_allowlist_rejects_untrusted_origin() -> None:
    """An origin outside the explicit allowlist must not be reflected
    back as an allowed origin (the header a browser actually checks
    before exposing the response to script)."""
    import raasoa.config as config_module

    config_module.settings.cors_origins = "https://trusted.example.com"
    import raasoa.main

    importlib.reload(raasoa.main)
    from raasoa.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get("/health", headers={"Origin": "https://evil.test"})

    assert resp.headers.get("access-control-allow-origin") != "https://evil.test"
