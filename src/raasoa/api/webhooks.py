"""Webhook endpoints for external source connectors.

Webhooks are authenticated via:
  - API Key (same as other endpoints), OR
  - Shared secret (X-Webhook-Secret header, configured via WEBHOOK_SECRET env)
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.middleware.auth import resolve_tenant, verify_webhook_secret
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
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    status: str
    event: str
    document_id: str | None = None
    message: str


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
            tenant_id = resolve_tenant(request)
        except HTTPException:
            # API key failed — try webhook secret
            verify_webhook_secret(request)
            # With secret-only auth, use default tenant from config
            from raasoa.middleware.auth import DEFAULT_TENANT
            tenant_id = DEFAULT_TENANT
    else:
        tenant_id = resolve_tenant(request)

    # Ensure source exists
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
            return WebhookResponse(
                status="rejected",
                event=payload.event,
                message=f"Data contract violated: {validation.reason}",
            )

    if payload.event == "document.deleted":
        result = await session.execute(
            text(
                "UPDATE documents SET status = 'deleted', "
                "review_status = 'rejected' "
                "WHERE tenant_id = :tid AND source_id = :sid "
                "AND source_object_id = :soid "
                "AND status != 'deleted'"
            ),
            {
                "tid": tenant_id,
                "sid": source_id,
                "soid": payload.source_object_id,
            },
        )
        await session.commit()
        return WebhookResponse(
            status="processed",
            event=payload.event,
            message=f"Deletion processed ({result.rowcount} affected)",  # type: ignore[attr-defined]
        )

    if payload.event in ("document.created", "document.updated"):
        if not payload.content:
            raise HTTPException(
                status_code=400,
                detail="Content required for create/update events",
            )

        title = payload.title or payload.source_object_id
        file_content = f"# {title}\n\n{payload.content}"
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
            )
            await session.refresh(doc)

            if payload.source_url:
                await session.execute(
                    text(
                        "UPDATE documents SET source_url = :url "
                        "WHERE id = :did"
                    ),
                    {"url": payload.source_url, "did": doc.id},
                )
                await session.commit()

            return WebhookResponse(
                status="processed",
                event=payload.event,
                document_id=str(doc.id),
                message=(
                    f"'{title}' ingested: {doc.chunk_count} chunks, "
                    f"quality={doc.quality_score or 'N/A'}"
                ),
            )
        except Exception:
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
