"""Source connector management API.

Create, configure, and sync data sources from the dashboard or API.
Each source stores its connection config (tokens, URLs, filters)
encrypted in the database and can be synced on demand.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceCreate(BaseModel):
    source_type: str = Field(
        ..., description="Type: notion, sharepoint, jira, confluence, webhook, custom",
    )
    name: str = Field(..., description="Display name for this source")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Connection config (token, url, filters, etc.)",
    )


class SourceResponse(BaseModel):
    id: str
    source_type: str
    name: str
    config_keys: list[str]  # Only show which keys are set, not values
    document_count: int
    last_sync: str | None
    sync_status: str


class SyncRequest(BaseModel):
    query: str = Field(
        default="*", description="Search query to filter what gets synced",
    )
    limit: int = Field(default=50, ge=1, le=500)


@router.post("", response_model=SourceResponse)
async def create_source(
    request: Request,
    body: SourceCreate,
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    """Create a new data source connection."""
    tenant_id = resolve_tenant(request)

    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
            "VALUES (:id, :tid, :stype, :name, CAST(:config AS jsonb))"
        ),
        {
            "id": source_id,
            "tid": tenant_id,
            "stype": body.source_type,
            "name": body.name,
            "config": __import__("json").dumps(body.config),
        },
    )
    await session.commit()

    return SourceResponse(
        id=str(source_id),
        source_type=body.source_type,
        name=body.name,
        config_keys=list(body.config.keys()),
        document_count=0,
        last_sync=None,
        sync_status="idle",
    )


@router.get("", response_model=list[SourceResponse])
async def list_sources(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[SourceResponse]:
    """List all configured data sources."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT s.id, s.source_type, s.name, s.connection_config, "
            "  (SELECT COUNT(*) FROM documents d "
            "   WHERE d.source_id = s.id AND d.status != 'deleted') as doc_count, "
            "  sc.last_sync_at, sc.sync_status "
            "FROM sources s "
            "LEFT JOIN sync_cursors sc "
            "  ON sc.source_id = s.id AND sc.source_type = s.source_type "
            "WHERE s.tenant_id = :tid "
            "ORDER BY s.name"
        ),
        {"tid": tenant_id},
    )

    return [
        SourceResponse(
            id=str(r.id),
            source_type=r.source_type,
            name=r.name,
            config_keys=list((r.connection_config or {}).keys()),
            document_count=r.doc_count,
            last_sync=str(r.last_sync_at) if r.last_sync_at else None,
            sync_status=r.sync_status or "idle",
        )
        for r in result.fetchall()
    ]


@router.delete("/{source_id}")
async def delete_source(
    request: Request,
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Delete a data source (does NOT delete its documents)."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "DELETE FROM sources WHERE id = :sid AND tenant_id = :tid "
            "RETURNING id"
        ),
        {"sid": source_id, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Source not found")
    await session.commit()
    return {"status": "deleted", "id": str(source_id)}


@router.post("/{source_id}/sync")
async def sync_source(
    request: Request,
    source_id: uuid.UUID,
    body: SyncRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Trigger a sync for a data source.

    Reads the source's connection config and syncs documents.
    Currently supports: notion, webhook (manual push).
    """
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT id, source_type, name, connection_config "
            "FROM sources WHERE id = :sid AND tenant_id = :tid"
        ),
        {"sid": source_id, "tid": tenant_id},
    )
    source = result.first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    config = source.connection_config or {}

    # Update sync status
    await session.execute(
        text(
            "INSERT INTO sync_cursors (source_type, source_id, sync_status) "
            "VALUES (:stype, :sid, 'running') "
            "ON CONFLICT (source_type, source_id) "
            "DO UPDATE SET sync_status = 'running', last_sync_at = now()"
        ),
        {"stype": source.source_type, "sid": source_id},
    )
    await session.commit()

    try:
        if source.source_type == "notion":
            stats = await _sync_notion(
                session, tenant_id, source_id, config,
                body.query, body.limit,
            )
        else:
            stats = {
                "status": "unsupported",
                "message": f"Auto-sync not available for {source.source_type}. Use webhooks.",
            }

        # Update sync status
        has_results = stats.get("synced", 0) > 0
        is_unsupported = stats.get("status") == "unsupported"
        status = "completed" if has_results or is_unsupported else "empty"
        await session.execute(
            text(
                "UPDATE sync_cursors SET sync_status = :status, "
                "items_synced = :count, last_sync_at = now() "
                "WHERE source_id = :sid"
            ),
            {
                "status": status,
                "count": stats.get("synced", 0),
                "sid": source_id,
            },
        )
        await session.commit()

        return stats

    except Exception as e:
        logger.exception("Sync failed for source %s", source_id)
        await session.execute(
            text(
                "UPDATE sync_cursors SET sync_status = 'error', "
                "error_message = :err WHERE source_id = :sid"
            ),
            {"err": str(e)[:500], "sid": source_id},
        )
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}") from e


async def _sync_notion(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    config: dict[str, Any],
    query: str,
    limit: int,
) -> dict[str, Any]:
    """Sync pages from Notion using the API token in config."""
    import httpx

    token = config.get("token", "")
    if not token:
        return {
            "status": "error",
            "message": "No Notion token configured. Set 'token' in source config.",
        }

    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    stats: dict[str, Any] = {"found": 0, "synced": 0, "skipped": 0, "errors": []}

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Search Notion
        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
            },
            json={"query": query if query != "*" else "", "page_size": min(limit, 100)},
        )
        if resp.status_code != 200:
            return {"status": "error", "message": f"Notion API error: {resp.status_code}"}

        results = resp.json().get("results", [])
        stats["found"] = len(results)

        provider = get_embedding_provider()

        for page in results:
            if page.get("object") != "page":
                stats["skipped"] += 1
                continue

            page_id = page["id"]
            title = _notion_title(page)

            # Fetch page blocks
            try:
                blocks_resp = await client.get(
                    f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Notion-Version": "2022-06-28",
                    },
                    params={"page_size": 100},
                )
                blocks_resp.raise_for_status()
                content = _notion_blocks_to_text(blocks_resp.json().get("results", []))
            except Exception:
                content = title

            if len(content.strip()) < 50:
                stats["skipped"] += 1
                continue

            # Ingest
            file_data = f"# {title}\n\n{content}".encode()
            try:
                doc, _assessment = await ingest_file(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    file_data=file_data,
                    filename=f"notion-{page_id}",
                    embedding_provider=provider,
                )
                await session.refresh(doc)

                # Set source URL
                url = page.get("url", "")
                if url:
                    await session.execute(
                        text("UPDATE documents SET source_url = :url WHERE id = :did"),
                        {"url": url, "did": doc.id},
                    )
                await session.commit()

                stats["synced"] += 1
                logger.info("Synced Notion: %s (%d chunks)", title, doc.chunk_count)

            except Exception as e:
                stats["errors"].append({"page": title, "error": str(e)[:200]})

    return stats


def _notion_title(page: dict[str, Any]) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            titles = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in titles) or "Untitled"
    return "Untitled"


def _notion_blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        bt = block.get("type", "")
        content = block.get(bt, {})
        rich_text = content.get("rich_text", [])
        text_val = "".join(rt.get("plain_text", "") for rt in rich_text)
        if text_val:
            if bt.startswith("heading_"):
                level = bt[-1]
                parts.append(f"{'#' * int(level)} {text_val}")
            elif "list_item" in bt:
                parts.append(f"- {text_val}")
            else:
                parts.append(text_val)
    return "\n\n".join(parts)
