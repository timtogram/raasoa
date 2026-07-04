"""Structured CRM query endpoint.

Typed filtering over crm_objects via the whitelisted DSL in
raasoa.retrieval.crm_query — the right tool for "which deals are over
$10k in stage closedwon", a question hybrid/semantic search answers
poorly but a single indexed WHERE clause answers exactly.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.retrieval.crm_query import CrmQuery, run_crm_query
from raasoa.security.principal import expand_principal_ids, resolve_principal_async

router = APIRouter(prefix="/v1/crm", tags=["crm"])


@router.post("/query")
async def crm_query(
    request: Request,
    body: CrmQuery,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    principal = await resolve_principal_async(request)
    principal_ids = (
        None if principal.is_legacy_tenant_wide
        else await expand_principal_ids(session, principal.tenant_id, principal.principal_id)  # type: ignore[arg-type]
    )
    try:
        results = await run_crm_query(session, principal.tenant_id, principal_ids, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"object_type": body.object_type, "count": len(results), "results": results}
