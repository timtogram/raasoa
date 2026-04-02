"""Webhook endpoints for external source connectors.

External systems (SharePoint, Jira, Confluence, custom) can push
document changes to RAASOA via webhooks. This enables event-driven
ingestion without polling.

Supported events:
- document.created: New document available
- document.updated: Existing document changed
- document.deleted: Document removed from source

Usage:
    POST /v1/webhooks/ingest
    {
      "event": "document.created",
      "source": "sharepoint",
      "title": "Q1 Report",
      "content": "...",
      "source_object_id": "sp://site/lib/doc.docx",
      "source_url": "https://company.sharepoint.com/...",
      "metadata": {"author": "Jane", "department": "Finance"}
    }
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.models.source import Source
from raasoa.providers.factory import get_embedding_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


class WebhookPayload(BaseModel):
    event: str = Field(
        ..., description="Event type: document.created/updated/deleted",
    )
    source: str = Field(..., description="Source identifier (sharepoint, jira, confluence, custom)")
    title: str | None = None
    content: str | None = None
    source_object_id: str = Field(..., description="Unique identifier in the source system")
    source_url: str | None = None
    metadata: dict = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    status: str
    event: str
    document_id: str | None = None
    message: str


@router.post("/ingest", response_model=WebhookResponse)
async def webhook_ingest(
    payload: WebhookPayload,
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    x_webhook_secret: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> WebhookResponse:
    """Receive document events from external sources."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

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

    if payload.event == "document.deleted":
        # Soft-delete the document
        result = await session.execute(
            text(
                "UPDATE documents SET status = 'deleted', "
                "review_status = 'rejected' "
                "WHERE tenant_id = :tid AND source_id = :sid "
                "AND source_object_id = :soid AND status != 'deleted'"
            ),
            {"tid": tenant_id, "sid": source_id, "soid": payload.source_object_id},
        )
        await session.commit()

        return WebhookResponse(
            status="processed",
            event=payload.event,
            message=f"Document deletion processed ({result.rowcount} affected)",
        )

    if payload.event in ("document.created", "document.updated"):
        if not payload.content:
            raise HTTPException(
                status_code=400,
                detail="Content required for document.created/updated events",
            )

        # Build file data from content
        title = payload.title or payload.source_object_id
        file_content = f"# {title}\n\n{payload.content}"
        file_data = file_content.encode("utf-8")

        provider = get_embedding_provider()

        try:
            doc, assessment = await ingest_file(
                session=session,
                tenant_id=tenant_id,
                source_id=source_id,
                file_data=file_data,
                filename=payload.source_object_id,
                embedding_provider=provider,
            )
            await session.refresh(doc)

            # Update source URL if provided
            if payload.source_url:
                await session.execute(
                    text("UPDATE documents SET source_url = :url WHERE id = :did"),
                    {"url": payload.source_url, "did": doc.id},
                )
                await session.commit()

            return WebhookResponse(
                status="processed",
                event=payload.event,
                document_id=str(doc.id),
                message=(
                    f"Document '{title}' ingested: "
                    f"{doc.chunk_count} chunks, "
                    f"quality={doc.quality_score or 'N/A'}"
                ),
            )

        except Exception as e:
            logger.exception("Webhook ingestion failed for %s", payload.source_object_id)
            raise HTTPException(
                status_code=500,
                detail="Ingestion failed. Check server logs.",
            ) from e

    raise HTTPException(
        status_code=400,
        detail=f"Unknown event type: {payload.event}. "
        "Supported: document.created, document.updated, document.deleted",
    )
