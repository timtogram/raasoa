"""API Key management — self-service create, list, revoke.

Keys are stored as SHA-256 hashes in the database. The plaintext
key is only shown once at creation time. This replaces the
static API_KEYS env var for managed deployments.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1/keys", tags=["api-keys"])


class KeyCreate(BaseModel):
    name: str = Field(..., description="Human-readable key name")
    scopes: list[str] = Field(
        default=["all"],
        description="Permission scopes: all, read, write, admin",
    )


class KeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    is_active: bool
    last_used_at: str | None
    created_at: str


class KeyCreated(KeyResponse):
    """Only returned on creation — contains the full key."""

    key: str  # Full key, shown ONCE


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@router.post("", response_model=KeyCreated)
async def create_key(
    request: Request,
    body: KeyCreate,
    session: AsyncSession = Depends(get_session),
) -> KeyCreated:
    """Create a new API key. The full key is shown only once."""
    tenant_id = await resolve_tenant_async(request)

    # Generate key
    raw_key = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    key_prefix = f"{raw_key[:7]}...{raw_key[-4:]}"
    key_id = uuid.uuid4()

    import json

    await session.execute(
        text(
            "INSERT INTO api_keys "
            "(id, tenant_id, key_hash, key_prefix, name, scopes) "
            "VALUES (:id, :tid, :hash, :prefix, :name, "
            " CAST(:scopes AS jsonb))"
        ),
        {
            "id": key_id,
            "tid": tenant_id,
            "hash": key_hash,
            "prefix": key_prefix,
            "name": body.name,
            "scopes": json.dumps(body.scopes),
        },
    )
    await session.commit()

    return KeyCreated(
        id=str(key_id),
        name=body.name,
        key_prefix=key_prefix,
        key=raw_key,  # Only time the full key is shown
        scopes=body.scopes,
        is_active=True,
        last_used_at=None,
        created_at="now",
    )


@router.get("", response_model=list[KeyResponse])
async def list_keys(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[KeyResponse]:
    """List all API keys for the tenant (no secrets shown)."""
    tenant_id = await resolve_tenant_async(request)

    result = await session.execute(
        text(
            "SELECT id, name, key_prefix, scopes, is_active, "
            "last_used_at, created_at "
            "FROM api_keys WHERE tenant_id = :tid "
            "ORDER BY created_at DESC"
        ),
        {"tid": tenant_id},
    )
    return [
        KeyResponse(
            id=str(r.id),
            name=r.name,
            key_prefix=r.key_prefix,
            scopes=r.scopes or ["all"],
            is_active=r.is_active,
            last_used_at=str(r.last_used_at) if r.last_used_at else None,
            created_at=str(r.created_at),
        )
        for r in result.fetchall()
    ]


@router.delete("/{key_id}")
async def revoke_key(
    request: Request,
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Revoke (deactivate) an API key."""
    tenant_id = await resolve_tenant_async(request)

    result = await session.execute(
        text(
            "UPDATE api_keys SET is_active = false "
            "WHERE id = :kid AND tenant_id = :tid "
            "RETURNING id"
        ),
        {"kid": key_id, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Key not found")
    await session.commit()
    return {"status": "revoked", "key_id": str(key_id)}
