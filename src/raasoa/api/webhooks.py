"""Webhook endpoints for external source connectors.

Webhooks are authenticated via:
  - API Key (same as other endpoints), OR
  - Shared secret (X-Webhook-Secret header, configured via WEBHOOK_SECRET env)
"""

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.middleware.auth import resolve_tenant_async, verify_webhook_secret
from raasoa.models.source import Source
from raasoa.providers.factory import get_embedding_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


class WebhookPayload(BaseModel):
    event: str = Field(
        ...,
        description="Event type: document.created/updated/deleted",
    )
    source: str = Field(
        ...,
        description="Source identifier (sharepoint, jira, notion, custom)",
    )
    title: str | None = None
    content: str | None = None
    source_object_id: str = Field(
        ..., description="Unique identifier in the source system",
    )
    source_url: str | None = None
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Unique key to prevent duplicate processing on retries. A "
            "second delivery with the same key returns the exact cached "
            "response from the first successful/rejected delivery without "
            "reprocessing. Not honored across a transient failure (500) — "
            "retry with the same key to actually retry. Cached entries "
            "expire after 48h."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    status: str
    event: str
    document_id: str | None = None
    message: str


async def _cached_idempotent_response(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    idempotency_key: str,
) -> WebhookResponse | None:
    """Look up a previously-cached terminal response for this key.

    Returns None on a cache miss (first delivery, or the key expired and
    was purged — a retry old enough to miss the cache just reprocesses,
    which is safe since idempotency keys only protect against
    close-in-time network retries, not indefinite dedup).
    """
    result = await session.execute(
        text(
            "SELECT response_json FROM webhook_idempotency_keys "
            "WHERE tenant_id = :tid AND idempotency_key = :key"
        ),
        {"tid": tenant_id, "key": idempotency_key},
    )
    row = result.first()
    if row is None:
        return None
    return WebhookResponse(**row.response_json)


async def _cache_idempotent_response(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    response: WebhookResponse,
) -> None:
    """Cache a terminal, deterministic response for this idempotency key.

    Only call this for outcomes that are safe to replay verbatim on a
    retry (success, or a deterministic rejection) — never for a transient
    failure, which must remain retryable with the same key.
    """
    await session.execute(
        text(
            "INSERT INTO webhook_idempotency_keys "
            "(tenant_id, idempotency_key, response_json) "
            "VALUES (:tid, :key, CAST(:resp AS jsonb)) "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING"
        ),
        {
            "tid": tenant_id,
            "key": idempotency_key,
            "resp": json.dumps(response.model_dump()),
        },
    )
    await session.commit()


@router.post("/ingest", response_model=WebhookResponse)
async def webhook_ingest(
    request: Request,
    payload: WebhookPayload,
    session: AsyncSession = Depends(get_session),
) -> WebhookResponse:
    """Receive document events from external sources.

    Authentication: API key OR webhook secret required.
    """
    # Auth: try API key first, fall back to webhook secret
    if settings.auth_enabled:
        try:
            tenant_id = await resolve_tenant_async(request)
        except HTTPException:
            # API key failed — try webhook secret
            verify_webhook_secret(request)
            # With secret-only auth, use default tenant from config
            from raasoa.middleware.auth import DEFAULT_TENANT
            tenant_id = DEFAULT_TENANT
    else:
        tenant_id = await resolve_tenant_async(request)

    # Idempotency: a cached hit short-circuits ALL processing below,
    # including source lookup/creation.
    if payload.idempotency_key:
        cached = await _cached_idempotent_response(
            session, tenant_id, payload.idempotency_key,
        )
        if cached is not None:
            return cached

    # Ensure source exists
    # Ensure tenant exists (auto-create for webhook flows)
    from raasoa.api.ingestion import _ensure_default_tenant_and_source
    await _ensure_default_tenant_and_source(session, tenant_id)

    result = await session.execute(
        text(
            "SELECT id FROM sources "
            "WHERE tenant_id = :tid AND source_type = :stype"
        ),
        {"tid": tenant_id, "stype": payload.source},
    )
    row = result.first()
    if row:
        source_id = row.id
    else:
        source = Source(
            tenant_id=tenant_id,
            source_type=payload.source,
            name=f"{payload.source.title()} Connector",
            connection_config={"webhook": True},
        )
        session.add(source)
        await session.flush()
        source_id = source.id

    # Data contract validation (before any processing)
    if payload.event in ("document.created", "document.updated"):
        from raasoa.ingestion.validation import validate_webhook_payload

        validation = validate_webhook_payload(
            source=payload.source,
            content=payload.content,
            metadata=payload.metadata,
            title=payload.title,
        )
        if not validation.valid:
            response = WebhookResponse(
                status="rejected",
                event=payload.event,
                message=f"Data contract violated: {validation.reason}",
            )
            if payload.idempotency_key:
                await _cache_idempotent_response(
                    session, tenant_id, payload.idempotency_key, response,
                )
            return response

    if payload.event == "document.deleted":
        from raasoa.api.sources import _cascade_delete_document_data

        result = await session.execute(
            text(
                "UPDATE documents SET status = 'deleted', "
                "review_status = 'rejected' "
                "WHERE tenant_id = :tid AND source_id = :sid "
                "AND source_object_id = :soid "
                "AND status != 'deleted' "
                "RETURNING id"
            ),
            {
                "tid": tenant_id,
                "sid": source_id,
                "soid": payload.source_object_id,
            },
        )
        deleted_doc_ids = [row.id for row in result.fetchall()]
        # A deleted document's ACL grants (e.g. a HubSpot record owner's
        # read access) and CRM object row have no FK to documents, so
        # they'd otherwise persist forever; chunks/claims are covered too
        # for defense-in-depth even though they FK-cascade on hard delete.
        await _cascade_delete_document_data(session, deleted_doc_ids)
        await session.commit()
        response = WebhookResponse(
            status="processed",
            event=payload.event,
            message=f"Deletion processed ({len(deleted_doc_ids)} affected)",
        )
        if payload.idempotency_key:
            await _cache_idempotent_response(
                session, tenant_id, payload.idempotency_key, response,
            )
        return response

    if payload.event in ("document.created", "document.updated"):
        if not payload.content:
            raise HTTPException(
                status_code=400,
                detail="Content required for create/update events",
            )

        title = payload.title or payload.source_object_id
        content = payload.content
        # Don't prepend title if content has frontmatter (would break parsing)
        file_content = content if content.strip().startswith("---") else f"# {title}\n\n{content}"
        file_data = file_content.encode("utf-8")

        provider = get_embedding_provider()

        try:
            doc, _assessment = await ingest_file(
                session=session,
                tenant_id=tenant_id,
                source_id=source_id,
                file_data=file_data,
                filename=payload.source_object_id,
                embedding_provider=provider,
                source_object_id=payload.source_object_id,
                source_url=payload.source_url,
                source_metadata=payload.metadata,
            )
            await session.refresh(doc)

            response = WebhookResponse(
                status="processed",
                event=payload.event,
                document_id=str(doc.id),
                message=(
                    f"'{title}' ingested: {doc.chunk_count} chunks, "
                    f"quality={doc.quality_score or 'N/A'}"
                ),
            )
            if payload.idempotency_key:
                await _cache_idempotent_response(
                    session, tenant_id, payload.idempotency_key, response,
                )
            return response
        except Exception:
            # Deliberately NOT cached: a transient failure (e.g. an
            # embedding provider outage) must remain retryable with the
            # same idempotency_key, not get permanently locked in.
            logger.exception(
                "Webhook ingestion failed for %s",
                payload.source_object_id,
            )
            raise HTTPException(
                status_code=500,
                detail="Ingestion failed. Check server logs.",
            ) from None

    raise HTTPException(
        status_code=400,
        detail=f"Unknown event: {payload.event}. "
        "Supported: document.created/updated/deleted",
    )
