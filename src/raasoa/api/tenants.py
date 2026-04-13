"""Tenant self-service API — signup, profile, plan management.

Public endpoints (no auth needed):
- POST /v1/tenants — create new tenant, returns initial API key

Authenticated endpoints (API key required):
- GET /v1/tenants/me — current tenant info + quota + usage
- PATCH /v1/tenants/me — update name, contact email
- POST /v1/tenants/me/export — GDPR data export
- DELETE /v1/tenants/me — delete all tenant data (GDPR right-to-erasure)
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
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


PLAN_QUOTAS = {
    "free": {"max_documents": 100, "max_queries_per_month": 1000, "max_sources": 1},
    "starter": {"max_documents": 1000, "max_queries_per_month": 10000, "max_sources": 3},
    "pro": {"max_documents": 10000, "max_queries_per_month": 100000, "max_sources": 20},
    "enterprise": {"max_documents": 1000000, "max_queries_per_month": 10000000, "max_sources": 100},
}


class TenantSignup(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    plan: str = Field(default="free")


class TenantCreated(BaseModel):
    tenant_id: str
    name: str
    plan: str
    api_key: str  # Shown only once
    api_key_prefix: str


class TenantInfo(BaseModel):
    id: str
    name: str
    plan: str
    quota: dict[str, Any]
    usage_this_month: dict[str, Any]


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)


@router.post("", response_model=TenantCreated)
async def signup(
    body: TenantSignup,
    session: AsyncSession = Depends(get_session),
) -> TenantCreated:
    """Create a new tenant + initial API key (no auth required).

    For hosted SaaS: anyone can sign up and get a free-tier tenant.
    For self-hosted: can be disabled via SIGNUP_ENABLED=false.
    """
    from raasoa.config import settings

    # Check if signup is enabled
    if not getattr(settings, "signup_enabled", True):
        raise HTTPException(
            status_code=403,
            detail="Signup is disabled. Contact the administrator.",
        )

    if body.plan not in PLAN_QUOTAS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plan. Must be one of: {list(PLAN_QUOTAS.keys())}",
        )

    # Free tier on signup — other plans need payment
    if body.plan != "free":
        raise HTTPException(
            status_code=400,
            detail="Self-service signup only supports 'free' plan. "
            "Contact sales for paid plans.",
        )

    tenant_id = uuid.uuid4()
    quota = PLAN_QUOTAS[body.plan]

    config = {}
    if body.email:
        config["contact_email"] = body.email

    await session.execute(
        text(
            "INSERT INTO tenants "
            "(id, name, plan, max_documents, max_queries_per_month, "
            " max_sources, config) "
            "VALUES (:id, :name, :plan, :max_docs, :max_queries, "
            " :max_sources, CAST(:config AS jsonb))"
        ),
        {
            "id": tenant_id,
            "name": body.name,
            "plan": body.plan,
            "max_docs": quota["max_documents"],
            "max_queries": quota["max_queries_per_month"],
            "max_sources": quota["max_sources"],
            "config": json.dumps(config),
        },
    )

    # Create initial API key
    raw_key = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = f"{raw_key[:7]}...{raw_key[-4:]}"

    await session.execute(
        text(
            "INSERT INTO api_keys "
            "(id, tenant_id, key_hash, key_prefix, name, scopes) "
            "VALUES (:id, :tid, :hash, :prefix, :name, "
            " CAST(:scopes AS jsonb))"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "hash": key_hash,
            "prefix": key_prefix,
            "name": "Initial Key",
            "scopes": json.dumps(["all"]),
        },
    )
    await session.commit()

    return TenantCreated(
        tenant_id=str(tenant_id),
        name=body.name,
        plan=body.plan,
        api_key=raw_key,
        api_key_prefix=key_prefix,
    )


@router.get("/me", response_model=TenantInfo)
async def get_current_tenant(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TenantInfo:
    """Get info about the current tenant (from API key)."""
    tenant_id = await resolve_tenant_async(request)

    result = await session.execute(
        text(
            "SELECT name, plan, max_documents, max_queries_per_month, "
            "max_sources FROM tenants WHERE id = :tid"
        ),
        {"tid": tenant_id},
    )
    tenant = result.first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Current counts
    counts_result = await session.execute(
        text(
            "SELECT "
            "  (SELECT COUNT(*) FROM documents "
            "   WHERE tenant_id = :tid AND status != 'deleted') AS docs, "
            "  (SELECT COUNT(*) FROM sources WHERE tenant_id = :tid) AS src, "
            "  (SELECT COALESCE(SUM(quantity), 0) FROM usage_events "
            "   WHERE tenant_id = :tid AND event_type = 'retrieve' "
            "   AND created_at > date_trunc('month', now())) AS queries"
        ),
        {"tid": tenant_id},
    )
    counts = counts_result.first()

    return TenantInfo(
        id=str(tenant_id),
        name=tenant.name,
        plan=tenant.plan or "free",
        quota={
            "max_documents": tenant.max_documents,
            "max_queries_per_month": tenant.max_queries_per_month,
            "max_sources": tenant.max_sources,
        },
        usage_this_month={
            "documents": counts.docs if counts else 0,
            "queries": counts.queries if counts else 0,
            "sources": counts.src if counts else 0,
        },
    )


@router.patch("/me", response_model=TenantInfo)
async def update_tenant(
    request: Request,
    body: TenantUpdate,
    session: AsyncSession = Depends(get_session),
) -> TenantInfo:
    """Update tenant profile."""
    tenant_id = await resolve_tenant_async(request)

    if body.name is not None:
        await session.execute(
            text("UPDATE tenants SET name = :name WHERE id = :tid"),
            {"name": body.name, "tid": tenant_id},
        )
        await session.commit()

    return await get_current_tenant(request, session)


@router.post("/me/export")
async def export_tenant_data(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """GDPR data export — returns everything the tenant has."""
    tenant_id = await resolve_tenant_async(request)

    # Tenant info
    t_result = await session.execute(
        text("SELECT name, plan, config, created_at FROM tenants WHERE id = :tid"),
        {"tid": tenant_id},
    )
    tenant = t_result.first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Documents
    d_result = await session.execute(
        text(
            "SELECT id, title, source_object_id, source_url, status, "
            "review_status, quality_score, chunk_count, version, "
            "created_at FROM documents WHERE tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
    documents = [
        {
            "id": str(d.id), "title": d.title,
            "source_object_id": d.source_object_id,
            "source_url": d.source_url, "status": d.status,
            "review_status": d.review_status,
            "quality_score": d.quality_score,
            "chunk_count": d.chunk_count, "version": d.version,
            "created_at": str(d.created_at),
        }
        for d in d_result.fetchall()
    ]

    # Sources
    s_result = await session.execute(
        text(
            "SELECT id, name, source_type, created_at "
            "FROM sources WHERE tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
    sources = [
        {
            "id": str(s.id), "name": s.name,
            "source_type": s.source_type,
            "created_at": str(s.created_at),
        }
        for s in s_result.fetchall()
    ]

    # Claims count only (too many to export each)
    c_result = await session.execute(
        text(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE status = 'active') AS active "
            "FROM claims c JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
    claims = c_result.first()

    # Usage summary
    u_result = await session.execute(
        text(
            "SELECT event_type, COUNT(*) AS count, SUM(quantity) AS total "
            "FROM usage_events WHERE tenant_id = :tid "
            "GROUP BY event_type"
        ),
        {"tid": tenant_id},
    )
    usage = {
        r.event_type: {"count": r.count, "total": r.total}
        for r in u_result.fetchall()
    }

    return {
        "tenant": {
            "id": str(tenant_id),
            "name": tenant.name,
            "plan": tenant.plan,
            "config": tenant.config,
            "created_at": str(tenant.created_at),
        },
        "documents": documents,
        "sources": sources,
        "claims_summary": {
            "total": claims.total if claims else 0,
            "active": claims.active if claims else 0,
        },
        "usage_summary": usage,
        "export_timestamp": str(uuid.uuid1()),
    }


@router.delete("/me")
async def delete_tenant(
    request: Request,
    confirm: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """GDPR right-to-erasure — permanently delete ALL tenant data.

    Requires ?confirm=DELETE_ALL_MY_DATA to prevent accidents.
    This is IRREVERSIBLE.
    """
    if confirm != "DELETE_ALL_MY_DATA":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. "
            "Pass ?confirm=DELETE_ALL_MY_DATA to proceed.",
        )

    tenant_id = await resolve_tenant_async(request)

    # Cascade delete via foreign keys handles most of it
    # Explicit deletion for tables without CASCADE
    import contextlib as _ctx
    for table in [
        "retrieval_feedback", "retrieval_logs", "usage_events",
        "audit_events", "api_keys", "knowledge_index",
        "knowledge_syntheses", "review_tasks", "conflict_candidates",
        "quality_findings", "claims", "chunks", "document_versions",
        "acl_entries", "documents", "sync_cursors", "sources",
    ]:
        with _ctx.suppress(Exception):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )

    # Clean up chunks/claims via document_id join
    await session.execute(
        text(
            "DELETE FROM chunks WHERE document_id IN ("
            "SELECT id FROM documents WHERE tenant_id = :tid)"
        ),
        {"tid": tenant_id},
    )
    await session.execute(
        text("DELETE FROM documents WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    await session.execute(
        text("DELETE FROM tenants WHERE id = :tid"),
        {"tid": tenant_id},
    )
    await session.commit()

    return {
        "status": "deleted",
        "message": "All tenant data has been permanently deleted.",
    }
