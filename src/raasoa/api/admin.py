"""Admin API — manage principal groups, memberships, and personal API keys.

Every endpoint here requires BOTH an admin-capable caller (the tenant's own
legacy/master key, or a personal key with ``is_admin=true``) AND the tenant
having explicitly opted in via ``tenants.admin_api_enabled``. The opt-in is
deliberate and never automatic: a fresh tenant starts with
``admin_api_enabled=false``, and only the tenant's own master key can flip
it on (see ``enable_admin_api``) — a personal admin key cannot exist until
the flag is already true, so bootstrapping is always anchored to the one
credential every tenant already trusts. This is what "A7" (in the design
review) meant by "must not auto-enable for legacy keys": the flag is never
a side effect of anything, only a deliberate act with the master key.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.security.principal import (
    Principal,
    expand_principal_ids,
    resolve_principal_async,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def require_admin(request: Request, session: AsyncSession) -> Principal:
    """Resolve the caller and enforce admin privileges + tenant opt-in."""
    principal = await resolve_principal_async(request)
    if not (principal.is_legacy_tenant_wide or principal.is_admin):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    result = await session.execute(
        text("SELECT admin_api_enabled FROM tenants WHERE id = :tid"),
        {"tid": principal.tenant_id},
    )
    row = result.first()
    if not row or not row.admin_api_enabled:
        raise HTTPException(
            status_code=403,
            detail="Admin API is not enabled for this tenant. "
            "Call POST /v1/admin/enable with the tenant's master key first.",
        )
    return principal


@router.post("/enable")
async def enable_admin_api(
    request: Request, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Turn on the Admin API for the caller's tenant.

    Requires the tenant's own legacy/master key — never a personal key,
    and never automatic — so ``admin_api_enabled`` can't be flipped on as
    a side effect of any other action.
    """
    principal = await resolve_principal_async(request)
    if not principal.is_legacy_tenant_wide:
        raise HTTPException(
            status_code=403,
            detail="Only the tenant's own master API key can enable the admin API.",
        )
    await session.execute(
        text("UPDATE tenants SET admin_api_enabled = true WHERE id = :tid"),
        {"tid": principal.tenant_id},
    )
    await session.commit()
    return {"status": "enabled", "tenant_id": str(principal.tenant_id)}


@router.get("/status")
async def admin_status(
    request: Request, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cheap introspection — does not itself require admin privileges, so
    a non-admin caller can find out whether they'd need a different key."""
    principal = await resolve_principal_async(request)
    result = await session.execute(
        text("SELECT admin_api_enabled FROM tenants WHERE id = :tid"),
        {"tid": principal.tenant_id},
    )
    row = result.first()
    return {
        "admin_api_enabled": bool(row and row.admin_api_enabled),
        "caller_is_admin_capable": principal.is_legacy_tenant_wide or principal.is_admin,
        "caller_is_legacy": principal.is_legacy_tenant_wide,
    }


# ---- Groups ----


class GroupCreate(BaseModel):
    principal_id: str = Field(..., description="e.g. 'group:sales'")
    display_name: str | None = None


class GroupResponse(BaseModel):
    principal_id: str
    display_name: str | None
    origin: str
    created_at: str


@router.post("/groups", response_model=GroupResponse)
async def create_group(
    request: Request, body: GroupCreate, session: AsyncSession = Depends(get_session),
) -> GroupResponse:
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "INSERT INTO principal_groups (id, tenant_id, principal_id, display_name, origin) "
            "VALUES (:id, :tid, :pid, :dname, 'manual') "
            "ON CONFLICT (tenant_id, principal_id) DO NOTHING "
            "RETURNING principal_id, display_name, origin, created_at"
        ),
        {
            "id": uuid.uuid4(), "tid": principal.tenant_id,
            "pid": body.principal_id, "dname": body.display_name,
        },
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=409, detail="Group already exists")
    await session.commit()
    return GroupResponse(
        principal_id=row.principal_id, display_name=row.display_name,
        origin=row.origin, created_at=str(row.created_at),
    )


@router.get("/groups", response_model=list[GroupResponse])
async def list_groups(
    request: Request, session: AsyncSession = Depends(get_session),
) -> list[GroupResponse]:
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "SELECT principal_id, display_name, origin, created_at "
            "FROM principal_groups WHERE tenant_id = :tid ORDER BY created_at DESC"
        ),
        {"tid": principal.tenant_id},
    )
    return [
        GroupResponse(
            principal_id=r.principal_id, display_name=r.display_name,
            origin=r.origin, created_at=str(r.created_at),
        )
        for r in result.fetchall()
    ]


@router.delete("/groups/{group_principal_id}")
async def delete_group(
    request: Request, group_principal_id: str, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "DELETE FROM principal_groups WHERE tenant_id = :tid AND principal_id = :pid "
            "RETURNING principal_id"
        ),
        {"tid": principal.tenant_id, "pid": group_principal_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Group not found")
    await session.execute(
        text(
            "DELETE FROM principal_memberships WHERE tenant_id = :tid "
            "AND (group_principal_id = :pid OR member_principal_id = :pid)"
        ),
        {"tid": principal.tenant_id, "pid": group_principal_id},
    )
    await session.commit()
    return {"status": "deleted", "principal_id": group_principal_id}


# ---- Memberships ----


class MembershipCreate(BaseModel):
    member_principal_id: str = Field(..., description="e.g. 'user:jane' or 'group:eu-sales'")


class MemberResponse(BaseModel):
    member_principal_id: str
    group_principal_id: str
    created_at: str


@router.post("/groups/{group_principal_id}/members", response_model=MemberResponse)
async def add_member(
    request: Request, group_principal_id: str, body: MembershipCreate,
    session: AsyncSession = Depends(get_session),
) -> MemberResponse:
    principal = await require_admin(request, session)
    if body.member_principal_id == group_principal_id:
        raise HTTPException(status_code=400, detail="A group cannot be a member of itself")
    result = await session.execute(
        text(
            "INSERT INTO principal_memberships "
            "(id, tenant_id, member_principal_id, group_principal_id) "
            "VALUES (:id, :tid, :mpid, :gpid) "
            "ON CONFLICT (tenant_id, member_principal_id, group_principal_id) DO NOTHING "
            "RETURNING member_principal_id, group_principal_id, created_at"
        ),
        {
            "id": uuid.uuid4(), "tid": principal.tenant_id,
            "mpid": body.member_principal_id, "gpid": group_principal_id,
        },
    )
    row = result.first()
    await session.commit()
    if not row:
        existing = await session.execute(
            text(
                "SELECT member_principal_id, group_principal_id, created_at "
                "FROM principal_memberships WHERE tenant_id = :tid "
                "AND member_principal_id = :mpid AND group_principal_id = :gpid"
            ),
            {
                "tid": principal.tenant_id, "mpid": body.member_principal_id,
                "gpid": group_principal_id,
            },
        )
        row = existing.first()
        if not row:
            raise HTTPException(status_code=500, detail="Membership insert failed")
    return MemberResponse(
        member_principal_id=row.member_principal_id,
        group_principal_id=row.group_principal_id,
        created_at=str(row.created_at),
    )


@router.get("/groups/{group_principal_id}/members", response_model=list[MemberResponse])
async def list_members(
    request: Request, group_principal_id: str, session: AsyncSession = Depends(get_session),
) -> list[MemberResponse]:
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "SELECT member_principal_id, group_principal_id, created_at "
            "FROM principal_memberships WHERE tenant_id = :tid AND group_principal_id = :gpid "
            "ORDER BY created_at DESC"
        ),
        {"tid": principal.tenant_id, "gpid": group_principal_id},
    )
    return [
        MemberResponse(
            member_principal_id=r.member_principal_id,
            group_principal_id=r.group_principal_id,
            created_at=str(r.created_at),
        )
        for r in result.fetchall()
    ]


@router.delete("/groups/{group_principal_id}/members/{member_principal_id}")
async def remove_member(
    request: Request, group_principal_id: str, member_principal_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "DELETE FROM principal_memberships WHERE tenant_id = :tid "
            "AND group_principal_id = :gpid AND member_principal_id = :mpid "
            "RETURNING member_principal_id"
        ),
        {"tid": principal.tenant_id, "gpid": group_principal_id, "mpid": member_principal_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Membership not found")
    await session.commit()
    return {"status": "deleted"}


# ---- Personal API keys ----


class AdminKeyCreate(BaseModel):
    name: str
    principal_id: str = Field(
        ..., description="e.g. 'user:jane' — required, unlike self-service /v1/keys",
    )
    clearance: str = Field(default="public", description="public, internal, confidential, secret")
    is_admin: bool = Field(default=False)
    scopes: list[str] = Field(default=["all"])


class AdminKeyCreated(BaseModel):
    id: str
    name: str
    key_prefix: str
    key: str  # Full key, shown ONCE
    principal_id: str
    clearance: str
    is_admin: bool


@router.post("/keys", response_model=AdminKeyCreated)
async def create_admin_key(
    request: Request, body: AdminKeyCreate, session: AsyncSession = Depends(get_session),
) -> AdminKeyCreated:
    """Issue a personal API key bound to a ``principal_id`` — this is what
    turns a person/service into something ACL grants can actually target.
    Unlike ``POST /v1/keys`` (self-service, always legacy/tenant-wide),
    every key minted here resolves through the ACL/RBAC path.
    """
    principal = await require_admin(request, session)
    if not body.principal_id.strip():
        raise HTTPException(status_code=400, detail="principal_id is required")
    if body.is_admin and not (principal.is_legacy_tenant_wide or principal.is_admin):
        raise HTTPException(status_code=403, detail="Only an admin can mint another admin key")

    raw_key = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    key_prefix = f"{raw_key[:7]}...{raw_key[-4:]}"
    key_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO api_keys "
            "(id, tenant_id, key_hash, key_prefix, name, principal_id, clearance, "
            " is_admin, scopes) "
            "VALUES (:id, :tid, :hash, :prefix, :name, :pid, :clearance, :is_admin, "
            " CAST(:scopes AS jsonb))"
        ),
        {
            "id": key_id, "tid": principal.tenant_id, "hash": key_hash,
            "prefix": key_prefix, "name": body.name, "pid": body.principal_id,
            "clearance": body.clearance, "is_admin": body.is_admin,
            "scopes": json.dumps(body.scopes),
        },
    )
    await session.commit()
    return AdminKeyCreated(
        id=str(key_id), name=body.name, key_prefix=key_prefix, key=raw_key,
        principal_id=body.principal_id, clearance=body.clearance, is_admin=body.is_admin,
    )


class AdminKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    principal_id: str | None
    clearance: str
    is_admin: bool
    is_active: bool
    last_used_at: str | None
    created_at: str


@router.get("/keys", response_model=list[AdminKeyResponse])
async def list_admin_keys(
    request: Request, session: AsyncSession = Depends(get_session),
) -> list[AdminKeyResponse]:
    """Like GET /v1/keys but also surfaces principal_id/clearance/is_admin
    — the RBAC-relevant fields the self-service endpoint deliberately
    hides."""
    principal = await require_admin(request, session)
    result = await session.execute(
        text(
            "SELECT id, name, key_prefix, principal_id, clearance, is_admin, "
            "is_active, last_used_at, created_at FROM api_keys "
            "WHERE tenant_id = :tid ORDER BY created_at DESC"
        ),
        {"tid": principal.tenant_id},
    )
    return [
        AdminKeyResponse(
            id=str(r.id), name=r.name, key_prefix=r.key_prefix,
            principal_id=r.principal_id, clearance=r.clearance, is_admin=r.is_admin,
            is_active=r.is_active,
            last_used_at=str(r.last_used_at) if r.last_used_at else None,
            created_at=str(r.created_at),
        )
        for r in result.fetchall()
    ]


# ---- Effective access introspection ----
#
# Source-level only — per-document acl_entries grants aren't enumerated
# here since a tenant can have an unbounded number of documents. To see a
# principal's full effective document set, use that principal's own key
# against GET /v1/documents.


class SourceAccess(BaseModel):
    source_id: str
    name: str
    source_type: str
    default_visibility: str
    visible: bool
    via: str


class EffectiveAccessResponse(BaseModel):
    principal_id: str
    resolved_principal_ids: list[str]
    sources: list[SourceAccess]


@router.get("/effective-access", response_model=EffectiveAccessResponse)
async def effective_access(
    request: Request, principal_id: str, session: AsyncSession = Depends(get_session),
) -> EffectiveAccessResponse:
    """Source-level visibility matrix for a given principal — lets an
    admin answer "what can user:jane see" without impersonating them."""
    admin_principal = await require_admin(request, session)
    resolved_ids = await expand_principal_ids(session, admin_principal.tenant_id, principal_id)

    result = await session.execute(
        text(
            "SELECT s.id, s.name, s.source_type, s.default_visibility, "
            "EXISTS (SELECT 1 FROM source_acl_grants g WHERE g.source_id = s.id "
            "  AND g.tenant_id = :tid AND g.principal_id = ANY(:pids)) AS via_grant "
            "FROM sources s WHERE s.tenant_id = :tid ORDER BY s.name"
        ),
        {"tid": admin_principal.tenant_id, "pids": resolved_ids},
    )
    sources = []
    for r in result.fetchall():
        if r.default_visibility != "restricted":
            visible, via = True, "open_source"
        elif r.via_grant:
            visible, via = True, "source_acl_grant"
        else:
            visible, via = (
                False,
                "no_grant_at_source_level "
                "(per-document acl_entries may still grant access)",
            )
        sources.append(SourceAccess(
            source_id=str(r.id), name=r.name, source_type=r.source_type,
            default_visibility=r.default_visibility, visible=visible, via=via,
        ))
    return EffectiveAccessResponse(
        principal_id=principal_id, resolved_principal_ids=resolved_ids, sources=sources,
    )
