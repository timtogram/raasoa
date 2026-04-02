"""API Key authentication and tenant resolution.

Every API request must include an API key via:
  - Header: Authorization: Bearer sk-xxx
  - Header: X-API-Key: sk-xxx

The API key maps to a specific tenant. The tenant is NOT client-settable;
it is derived from the authenticated key. This prevents tenant spoofing.

Configuration (env / .env):
    AUTH_ENABLED=true
    API_KEYS=sk-dev-key:00000000-0000-0000-0000-000000000001,sk-other:tenant-uuid

When AUTH_ENABLED=false (dev mode), all requests use the default tenant.
"""

import contextlib
import uuid

from fastapi import HTTPException, Request

from raasoa.config import settings

# Parsed key→tenant map (lazy init)
_key_map: dict[str, uuid.UUID] | None = None

DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _get_key_map() -> dict[str, uuid.UUID]:
    global _key_map
    if _key_map is None:
        _key_map = {}
        raw = settings.api_keys.strip()
        if raw:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" not in pair:
                    continue
                key, tid = pair.split(":", 1)
                with contextlib.suppress(ValueError):
                    _key_map[key.strip()] = uuid.UUID(tid.strip())
    return _key_map


def resolve_tenant(request: Request) -> uuid.UUID:
    """Extract and validate API key, return the associated tenant UUID.

    When auth is disabled (dev mode), returns the default tenant.
    Raises 401 if auth is enabled and key is missing/invalid.
    """
    if not settings.auth_enabled:
        return DEFAULT_TENANT

    # Extract API key from headers
    api_key: str | None = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:].strip()

    if not api_key:
        api_key = request.headers.get("x-api-key", "").strip() or None

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Set Authorization: Bearer <key> or X-API-Key header.",
        )

    key_map = _get_key_map()
    tenant_id = key_map.get(api_key)

    if tenant_id is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
        )

    return tenant_id


def verify_webhook_secret(request: Request) -> None:
    """Verify the webhook shared secret.

    Raises 401 if webhook_secret is configured but request doesn't match.
    """
    if not settings.webhook_secret:
        return  # No secret configured — skip verification

    provided = request.headers.get("x-webhook-secret", "").strip()
    if provided != settings.webhook_secret:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing webhook secret.",
        )
