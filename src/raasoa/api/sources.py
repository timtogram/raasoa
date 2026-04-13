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
from raasoa.middleware.auth import resolve_tenant_async

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
    sync_interval_minutes: int | None = Field(
        default=None,
        description="Auto-sync interval in minutes. None = manual only.",
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
    tenant_id = await resolve_tenant_async(request)

    # Quota check: source limit
    from raasoa.middleware.metering import check_quota
    allowed, reason = await check_quota(session, tenant_id, "sources")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

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
    tenant_id = await resolve_tenant_async(request)

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
    tenant_id = await resolve_tenant_async(request)

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
    tenant_id = await resolve_tenant_async(request)

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
        elif source.source_type == "sharepoint":
            stats = await _sync_sharepoint(
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
    """Sync pages from Notion with full metadata extraction.

    Extracts: title, author, last_edited_by, last_edited_time,
    created_time, status, tags/topics, parent page path.
    Uses last_edited_time for delta-sync (only re-ingest changed pages).
    """
    import httpx

    token = config.get("token", "")
    if not token:
        return {
            "status": "error",
            "message": "No Notion token configured.",
        }

    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    stats: dict[str, Any] = {
        "found": 0, "synced": 0, "skipped": 0,
        "unchanged": 0, "errors": [],
    }

    # Get last sync time for delta-sync
    cursor_result = await session.execute(
        text(
            "SELECT delta_token FROM sync_cursors "
            "WHERE source_id = :sid AND source_type = 'notion'"
        ),
        {"sid": source_id},
    )
    cursor_row = cursor_result.first()
    last_sync_token = cursor_row.delta_token if cursor_row else None

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Search Notion
        search_body: dict[str, Any] = {
            "page_size": min(limit, 100),
        }
        if query and query != "*":
            search_body["query"] = query
        if last_sync_token:
            search_body["filter"] = {"property": "object", "value": "page"}
            search_body["sort"] = {
                "direction": "descending",
                "timestamp": "last_edited_time",
            }

        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json=search_body,
        )
        if resp.status_code != 200:
            return {
                "status": "error",
                "message": f"Notion API {resp.status_code}",
            }

        results = resp.json().get("results", [])
        stats["found"] = len(results)
        provider = get_embedding_provider()

        for page in results:
            if page.get("object") != "page":
                stats["skipped"] += 1
                continue

            page_id = page["id"]
            title = _notion_title(page)

            # Extract rich metadata
            meta = _notion_metadata(page)

            # Delta-sync: skip if not changed since last sync
            if (
                last_sync_token
                and meta.get("last_edited_time")
                and meta["last_edited_time"] <= last_sync_token
            ):
                stats["unchanged"] += 1
                continue

            # Fetch page blocks
            try:
                blocks_resp = await client.get(
                    f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers=headers,
                    params={"page_size": 100},
                )
                blocks_resp.raise_for_status()
                content = _notion_blocks_to_text(
                    blocks_resp.json().get("results", []),
                )
            except Exception:
                content = title

            if len(content.strip()) < 50:
                stats["skipped"] += 1
                continue

            # Build file with metadata header
            meta_header = ""
            if meta.get("author"):
                meta_header += f"Author: {meta['author']}\n"
            if meta.get("last_edited_by"):
                meta_header += f"Last edited by: {meta['last_edited_by']}\n"
            if meta.get("last_edited_time"):
                meta_header += f"Last edited: {meta['last_edited_time']}\n"
            if meta.get("status"):
                meta_header += f"Status: {meta['status']}\n"
            if meta.get("tags"):
                meta_header += f"Tags: {', '.join(meta['tags'])}\n"
            if meta.get("parent_path"):
                meta_header += f"Path: {meta['parent_path']}\n"

            file_content = f"# {title}\n"
            if meta_header:
                file_content += f"\n{meta_header}\n"
            file_content += f"\n{content}"
            file_data = file_content.encode("utf-8")

            # Ingest
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
                        text(
                            "UPDATE documents "
                            "SET source_url = :url "
                            "WHERE id = :did"
                        ),
                        {"url": url, "did": doc.id},
                    )
                await session.commit()

                stats["synced"] += 1
                logger.info(
                    "Synced Notion: %s (%d chunks)", title, doc.chunk_count,
                )
            except Exception as e:
                stats["errors"].append(
                    {"page": title, "error": str(e)[:200]},
                )

    # Update delta token for next sync
    if stats["synced"] > 0:
        from datetime import UTC, datetime

        await session.execute(
            text(
                "INSERT INTO sync_cursors "
                "(source_type, source_id, delta_token, "
                " last_sync_at, sync_status, items_synced) "
                "VALUES ('notion', :sid, :token, now(), "
                " 'completed', :count) "
                "ON CONFLICT (source_type, source_id) "
                "DO UPDATE SET delta_token = :token, "
                "  last_sync_at = now(), "
                "  sync_status = 'completed', "
                "  items_synced = :count"
            ),
            {
                "sid": source_id,
                "token": datetime.now(UTC).isoformat(),
                "count": stats["synced"],
            },
        )
        await session.commit()

    return stats


async def _sync_sharepoint(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    config: dict[str, Any],
    query: str,
    limit: int,
) -> dict[str, Any]:
    """Sync documents from SharePoint via Microsoft Graph API.

    Config requires:
    - tenant_id_azure: Azure AD tenant ID
    - client_id: App registration client ID
    - client_secret: App registration secret
    - site_id: SharePoint site ID (or site URL)
    - drive_id: Optional — specific document library
    """
    import httpx

    az_tenant = config.get("tenant_id_azure", "")
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    site_id = config.get("site_id", "")

    if not all([az_tenant, client_id, client_secret, site_id]):
        return {
            "status": "error",
            "message": "Missing SharePoint config. Required: "
            "tenant_id_azure, client_id, client_secret, site_id",
        }

    stats: dict[str, Any] = {
        "found": 0, "synced": 0, "skipped": 0, "errors": [],
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get OAuth token
        token_resp = await client.post(
            f"https://login.microsoftonline.com/{az_tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if token_resp.status_code != 200:
            return {
                "status": "error",
                "message": f"Azure OAuth failed: {token_resp.status_code}",
            }

        access_token = token_resp.json().get("access_token", "")
        headers = {"Authorization": f"Bearer {access_token}"}

        # Search for documents in the site
        search_url = (
            f"https://graph.microsoft.com/v1.0/sites/{site_id}"
            f"/drive/root/search(q='{query}')"
        )
        resp = await client.get(
            search_url,
            headers=headers,
            params={"$top": limit},
        )
        if resp.status_code != 200:
            return {
                "status": "error",
                "message": f"Graph API error: {resp.status_code}",
            }

        items = resp.json().get("value", [])
        stats["found"] = len(items)

        from raasoa.ingestion.pipeline import ingest_file
        from raasoa.providers.factory import get_embedding_provider

        provider = get_embedding_provider()

        for item in items:
            name = item.get("name", "")
            item_id = item.get("id", "")

            # Only process supported file types
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in (
                "pdf", "docx", "xlsx", "pptx", "txt", "md", "csv",
            ):
                stats["skipped"] += 1
                continue

            # Download file content
            try:
                dl_url = (
                    f"https://graph.microsoft.com/v1.0/sites/{site_id}"
                    f"/drive/items/{item_id}/content"
                )
                dl_resp = await client.get(dl_url, headers=headers)
                dl_resp.raise_for_status()
                file_data = dl_resp.content

                doc, _ = await ingest_file(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    file_data=file_data,
                    filename=name,
                    embedding_provider=provider,
                )
                await session.refresh(doc)

                web_url = item.get("webUrl", "")
                if web_url:
                    await session.execute(
                        text(
                            "UPDATE documents SET source_url = :url "
                            "WHERE id = :did"
                        ),
                        {"url": web_url, "did": doc.id},
                    )
                await session.commit()

                stats["synced"] += 1
                logger.info(
                    "Synced SharePoint: %s (%d chunks)",
                    name, doc.chunk_count,
                )
            except Exception as e:
                stats["errors"].append(
                    {"file": name, "error": str(e)[:200]},
                )

    return stats


def _notion_title(page: dict[str, Any]) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            titles = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in titles) or "Untitled"
    return "Untitled"


def _notion_metadata(page: dict[str, Any]) -> dict[str, Any]:
    """Extract rich metadata from a Notion page object.

    Pulls: author, last_edited_by, timestamps, status, tags,
    parent page path — everything useful for quality/governance.
    """
    meta: dict[str, Any] = {}

    # Timestamps
    meta["created_time"] = page.get("created_time")
    meta["last_edited_time"] = page.get("last_edited_time")

    # Author / editor
    created_by = page.get("created_by", {})
    if created_by.get("name"):
        meta["author"] = created_by["name"]
    elif created_by.get("id"):
        meta["author"] = created_by["id"]

    edited_by = page.get("last_edited_by", {})
    if edited_by.get("name"):
        meta["last_edited_by"] = edited_by["name"]

    # Parent path
    parent = page.get("parent", {})
    if parent.get("type") == "page_id":
        meta["parent_id"] = parent["page_id"]
    elif parent.get("type") == "database_id":
        meta["parent_database"] = parent["database_id"]

    # Properties — extract common types
    props = page.get("properties", {})
    tags: list[str] = []
    for prop_name, prop in props.items():
        ptype = prop.get("type", "")

        if ptype == "status":
            status_obj = prop.get("status")
            if status_obj and status_obj.get("name"):
                meta["status"] = status_obj["name"]

        elif ptype == "select":
            select_obj = prop.get("select")
            if select_obj and select_obj.get("name"):
                meta[prop_name.lower()] = select_obj["name"]

        elif ptype == "multi_select":
            options = prop.get("multi_select", [])
            for opt in options:
                if opt.get("name"):
                    tags.append(opt["name"])

        elif ptype == "people":
            people = prop.get("people", [])
            names = [p.get("name", "") for p in people if p.get("name")]
            if names:
                meta[f"property_{prop_name.lower()}"] = ", ".join(names)

        elif ptype == "date":
            date_obj = prop.get("date")
            if date_obj and date_obj.get("start"):
                meta[f"date_{prop_name.lower()}"] = date_obj["start"]

        elif ptype == "url":
            url_val = prop.get("url")
            if url_val:
                meta[f"url_{prop_name.lower()}"] = url_val

        elif ptype == "rich_text":
            texts = prop.get("rich_text", [])
            text_val = "".join(t.get("plain_text", "") for t in texts)
            if text_val and len(text_val) < 200:
                meta[prop_name.lower()] = text_val

    if tags:
        meta["tags"] = tags

    # Build parent path string
    parent_parts: list[str] = []
    if meta.get("parent_database"):
        parent_parts.append(f"db:{meta['parent_database'][:8]}")
    if meta.get("parent_id"):
        parent_parts.append(f"page:{meta['parent_id'][:8]}")
    if parent_parts:
        meta["parent_path"] = " > ".join(parent_parts)

    return meta


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
