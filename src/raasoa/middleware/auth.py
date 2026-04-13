"""API Key authentication and tenant resolution.

Key resolution order:
1. Database (api_keys table) — for managed/SaaS deployments
2. ENV fallback (API_KEYS setting) — for simple self-hosted setups

The API key maps to a specific tenant. The tenant is NOT client-settable.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import text

from raasoa.config import settings

# ENV-based key→tenant map (lazy init, fallback only)
_env_key_map: dict[str, uuid.UUID] | None = None

DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _get_env_key_map() -> dict[str, uuid.UUID]:
    """Parse API_KEYS from env (fallback for non-DB setups)."""
    global _env_key_map
    if _env_key_map is None:
        _env_key_map = {}
        raw = settings.api_keys.strip()
        if raw:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" not in pair:
                    continue
                key, tid = pair.split(":", 1)
                with contextlib.suppress(ValueError):
                    _env_key_map[key.strip()] = uuid.UUID(tid.strip())
    return _env_key_map


def _hash_key(key: str) -> str:
    """SHA-256 hash for DB key lookup."""
    return hashlib.sha256(key.encode()).hexdigest()


async def _resolve_key_from_db(api_key: str) -> uuid.UUID | None:
    """Look up API key in the database. Returns tenant_id or None."""
    try:
        from raasoa.db import async_session

        key_hash = _hash_key(api_key)

        async with async_session() as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id FROM api_keys "
                    "WHERE key_hash = :hash AND is_active = true "
                    "AND (expires_at IS NULL OR expires_at > now())"
                ),
                {"hash": key_hash},
            )
            row = result.first()
            if row:
                # Update last_used_at (best-effort)
                await session.execute(
                    text(
                        "UPDATE api_keys SET last_used_at = now() "
                        "WHERE key_hash = :hash"
                    ),
                    {"hash": key_hash},
                )
                await session.commit()
                return uuid.UUID(str(row.tenant_id))
    except Exception:
        pass  # DB lookup failed — fall through to ENV
    return None


def _extract_api_key(request: Request) -> str | None:
    """Extract API key from request headers."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:].strip()
        if key:
            return key

    key = request.headers.get("x-api-key", "").strip()
    return key or None


def resolve_tenant(request: Request) -> uuid.UUID:
    """Sync resolve — ENV keys only. Fast, no DB call.

    Used by the sync fallback paths. For full DB+ENV resolution,
    use resolve_tenant_async.
    """
    if not settings.auth_enabled:
        return DEFAULT_TENANT

    api_key = _extract_api_key(request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Set Authorization: Bearer <key> or X-API-Key header.",
        )

    env_map = _get_env_key_map()
    tenant_id = env_map.get(api_key)
    if tenant_id:
        return tenant_id

    raise HTTPException(
        status_code=401,
        detail="Invalid API key.",
    )


async def resolve_tenant_async(request: Request) -> uuid.UUID:
    """Async version of resolve_tenant — use in async endpoints.

    Resolution order:
    1. If auth disabled → return default tenant
    2. Try ENV lookup (fast)
    3. Try DB lookup (api_keys table)
    4. Fail with 401
    """
    if not settings.auth_enabled:
        return DEFAULT_TENANT

    api_key = _extract_api_key(request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Set Authorization: Bearer <key> or X-API-Key header.",
        )

    # Try ENV keys first (fast)
    env_map = _get_env_key_map()
    tenant_id = env_map.get(api_key)
    if tenant_id:
        return tenant_id

    # Try DB keys
    tenant_id = await _resolve_key_from_db(api_key)
    if tenant_id:
        return tenant_id

    raise HTTPException(
        status_code=401,
        detail="Invalid API key.",
    )


def verify_webhook_secret(request: Request) -> None:
    """Verify webhook authentication via shared secret.

    Raises 401 if auth enabled but no valid credential.
    """
    if not settings.webhook_secret:
        if settings.auth_enabled:
            raise HTTPException(
                status_code=401,
                detail="Webhook requires API key or WEBHOOK_SECRET.",
            )
        return

    provided = request.headers.get("x-webhook-secret", "").strip()
    if provided == settings.webhook_secret:
        return

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing webhook secret.",
    )
