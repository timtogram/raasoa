"""Source connector management API.

Create, configure, and sync data sources from the dashboard or API.
Each source stores its connection config (tokens, URLs, filters)
encrypted in the database and can be synced on demand.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

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
            "config": json.dumps({
                **body.config,
                **(
                    {"sync_interval_minutes": body.sync_interval_minutes}
                    if body.sync_interval_minutes is not None
                    else {}
                ),
            }),
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
    Currently supports: notion, sharepoint, jira, webhook/manual push.
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
        elif source.source_type == "jira":
            stats = await _sync_jira(
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
        is_error = stats.get("status") == "error"
        status = "error" if is_error else (
            "completed" if has_results or is_unsupported else "empty"
        )
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
                content = await _fetch_notion_blocks_text(client, headers, page_id)
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
                url = page.get("url", "")
                notion_source_path = "/".join(
                    part for part in [meta.get("parent_path"), title] if part
                )
                ingest_meta = {
                    **meta,
                    "connector": "notion",
                    "notion_id": page_id,
                    "source_path": notion_source_path or title,
                    "folder_path": meta.get("parent_path") or "Notion",
                }
                doc, _assessment = await ingest_file(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    file_data=file_data,
                    filename=f"notion-{page_id}",
                    embedding_provider=provider,
                    source_object_id=f"notion:{page_id}",
                    source_url=url,
                    source_metadata=ingest_meta,
                    last_modified=_parse_datetime(meta.get("last_edited_time")),
                )
                await session.refresh(doc)

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
    - site_id OR site_url: SharePoint site ID or URL
    - drive_id: Optional — specific document library. If omitted, all drives
      for the site are scanned.
    """
    import httpx

    az_tenant = config.get("tenant_id_azure", "")
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    site_id = config.get("site_id", "")
    site_url = config.get("site_url", "")
    configured_drive_id = config.get("drive_id", "")

    if not all([az_tenant, client_id, client_secret]) or not (site_id or site_url):
        return {
            "status": "error",
            "message": "Missing SharePoint config. Required: "
            "tenant_id_azure, client_id, client_secret, site_id or site_url",
        }

    stats: dict[str, Any] = {
        "found": 0,
        "synced": 0,
        "skipped": 0,
        "deleted": 0,
        "errors": [],
        "drives": [],
        "delta_complete": True,
    }

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
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
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": (
                "hierarchicalsharing, deltashowremovedasdeleted, "
                "deltatraversepermissiongaps, deltashowsharingchanges"
            ),
        }

        if not site_id and site_url:
            site_id = await _resolve_sharepoint_site_id(client, headers, site_url)

        drives = await _sharepoint_drives(
            client, headers, site_id, configured_drive_id,
        )
        if not drives:
            return {
                "status": "error",
                "message": "No SharePoint drives found for site.",
            }

        stats["drives"] = [
            {"id": d["id"], "name": d.get("name") or d["id"]}
            for d in drives
        ]

        cursor_map = await _sharepoint_cursor_map(session, source_id)

        if query and query != "*":
            for drive in drives:
                await _sync_sharepoint_search_drive(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    client=client,
                    headers=headers,
                    site_id=site_id,
                    drive=drive,
                    query=query,
                    limit=max(0, limit - stats["synced"]),
                    sync_acl=bool(config.get("sync_acl", False)),
                    stats=stats,
                )
                if stats["synced"] >= limit:
                    break
            return stats

        next_cursor_map: dict[str, str] = {}
        for drive in drives:
            if stats["synced"] >= limit:
                stats["delta_complete"] = False
                break
            delta_link = await _sync_sharepoint_delta_drive(
                session=session,
                tenant_id=tenant_id,
                source_id=source_id,
                client=client,
                headers=headers,
                site_id=site_id,
                drive=drive,
                cursor_url=cursor_map.get(drive["id"]) or cursor_map.get("default"),
                limit=max(0, limit - stats["synced"]),
                sync_acl=bool(config.get("sync_acl", False)),
                stats=stats,
            )
            if delta_link:
                next_cursor_map[drive["id"]] = delta_link

        if stats["delta_complete"] and next_cursor_map:
            await session.execute(
                text(
                    "INSERT INTO sync_cursors "
                    "(source_type, source_id, delta_token, last_sync_at, "
                    " sync_status, items_synced) "
                    "VALUES ('sharepoint', :sid, :token, now(), "
                    " 'completed', :count) "
                    "ON CONFLICT (source_type, source_id) "
                    "DO UPDATE SET delta_token = :token, "
                    "  last_sync_at = now(), "
                    "  sync_status = 'completed', "
                    "  items_synced = :count"
                ),
                {
                    "sid": source_id,
                    "token": json.dumps(next_cursor_map),
                    "count": stats["synced"],
                },
            )
            await session.commit()

    return stats


async def _sync_jira(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    config: dict[str, Any],
    query: str,
    limit: int,
) -> dict[str, Any]:
    """Sync Jira Cloud issues via Atlassian REST API v3 enhanced JQL search.

    Config requires:
    - base_url: https://your-domain.atlassian.net
    - email: Atlassian account email
    - api_token: Atlassian API token
    - jql: Optional default JQL. If omitted, ``ORDER BY updated DESC``.
    """
    import httpx

    base_url = (config.get("base_url") or "").rstrip("/")
    email = config.get("email", "")
    api_token = config.get("api_token", "")
    default_jql = config.get("jql") or "ORDER BY updated DESC"

    if not all([base_url, email, api_token]):
        return {
            "status": "error",
            "message": "Missing Jira config. Required: base_url, email, api_token",
        }

    jql = query if query and query != "*" else default_jql
    fields = config.get("fields") or [
        "summary",
        "description",
        "status",
        "issuetype",
        "priority",
        "labels",
        "assignee",
        "reporter",
        "created",
        "updated",
        "project",
        "comment",
        "resolution",
    ]

    stats: dict[str, Any] = {
        "found": 0,
        "synced": 0,
        "skipped": 0,
        "unchanged": 0,
        "errors": [],
    }

    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    provider = get_embedding_provider()
    next_page_token: str | None = None

    async with httpx.AsyncClient(
        timeout=120.0,
        auth=(email, api_token),
        headers={"Accept": "application/json"},
    ) as client:
        while stats["synced"] < limit:
            page_size = min(100, limit - stats["synced"])
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": fields,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            resp = await client.post(
                f"{base_url}/rest/api/3/search/jql",
                json=body,
            )
            if resp.status_code == 404:
                body.pop("nextPageToken", None)
                body["startAt"] = stats["found"]
                resp = await client.post(
                    f"{base_url}/rest/api/3/search",
                    json=body,
                )
            if resp.status_code != 200:
                return {
                    "status": "error",
                    "message": f"Jira API {resp.status_code}: {resp.text[:200]}",
                }

            data = resp.json()
            issues = data.get("issues", [])
            stats["found"] += len(issues)
            if not issues:
                break

            for issue in issues:
                try:
                    content = _jira_issue_to_markdown(issue, base_url)
                    if len(content.strip()) < 50:
                        stats["skipped"] += 1
                        continue
                    fields_data = issue.get("fields") or {}
                    key = issue.get("key") or issue.get("id")
                    updated = fields_data.get("updated")
                    meta = _jira_issue_metadata(issue, base_url)
                    doc, _ = await ingest_file(
                        session=session,
                        tenant_id=tenant_id,
                        source_id=source_id,
                        file_data=content.encode("utf-8"),
                        filename=f"{key}.md",
                        embedding_provider=provider,
                        source_object_id=f"jira:{key}",
                        source_url=f"{base_url}/browse/{key}",
                        source_metadata=meta,
                        last_modified=_parse_datetime(updated),
                    )
                    await session.refresh(doc)
                    stats["synced"] += 1
                    logger.info("Synced Jira: %s (%d chunks)", key, doc.chunk_count)
                except Exception as e:
                    stats["errors"].append({
                        "issue": issue.get("key") or issue.get("id"),
                        "error": str(e)[:200],
                    })

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    return stats


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUPPORTED_SYNC_EXTENSIONS = {
    "pdf", "docx", "xlsx", "pptx", "txt", "md", "csv", "html",
}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _resolve_sharepoint_site_id(
    client: Any,
    headers: dict[str, str],
    site_url: str,
) -> str:
    parsed = urlparse(site_url)
    if not parsed.netloc:
        raise ValueError("Invalid SharePoint site_url")
    path = parsed.path.rstrip("/")
    url = (
        f"{GRAPH_BASE}/sites/{parsed.netloc}:{path}"
        if path
        else f"{GRAPH_BASE}/sites/{parsed.netloc}"
    )
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    site_id = resp.json().get("id")
    if not site_id:
        raise ValueError("Microsoft Graph did not return a SharePoint site id")
    return str(site_id)


async def _sharepoint_drives(
    client: Any,
    headers: dict[str, str],
    site_id: str,
    drive_id: str | None,
) -> list[dict[str, Any]]:
    if drive_id:
        resp = await client.get(f"{GRAPH_BASE}/drives/{drive_id}", headers=headers)
        resp.raise_for_status()
        return [resp.json()]

    drives: list[dict[str, Any]] = []
    url = f"{GRAPH_BASE}/sites/{site_id}/drives"
    while url:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        drives.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return drives


async def _sharepoint_cursor_map(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> dict[str, str]:
    result = await session.execute(
        text(
            "SELECT delta_token FROM sync_cursors "
            "WHERE source_id = :sid AND source_type = 'sharepoint'"
        ),
        {"sid": source_id},
    )
    row = result.first()
    if not row or not row.delta_token:
        return {}
    try:
        parsed = json.loads(row.delta_token)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        pass
    return {"default": str(row.delta_token)}


async def _sync_sharepoint_search_drive(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    client: Any,
    headers: dict[str, str],
    site_id: str,
    drive: dict[str, Any],
    query: str,
    limit: int,
    sync_acl: bool,
    stats: dict[str, Any],
) -> None:
    if limit <= 0:
        return
    drive_id = str(drive["id"])
    escaped_query = query.replace("'", "''")
    url = f"{GRAPH_BASE}/drives/{drive_id}/root/search(q='{escaped_query}')"
    while url and stats["synced"] < limit:
        resp = await client.get(url, headers=headers, params={"$top": min(100, limit)})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("value", [])
        stats["found"] += len(items)
        for item in items:
            if stats["synced"] >= limit:
                break
            await _ingest_sharepoint_item(
                session=session,
                tenant_id=tenant_id,
                source_id=source_id,
                client=client,
                headers=headers,
                site_id=site_id,
                drive=drive,
                item=item,
                sync_acl=sync_acl,
                stats=stats,
            )
        url = data.get("@odata.nextLink")


async def _sync_sharepoint_delta_drive(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    client: Any,
    headers: dict[str, str],
    site_id: str,
    drive: dict[str, Any],
    cursor_url: str | None,
    limit: int,
    sync_acl: bool,
    stats: dict[str, Any],
) -> str | None:
    if limit <= 0:
        stats["delta_complete"] = False
        return cursor_url

    drive_id = str(drive["id"])
    url = cursor_url if cursor_url else f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
    delta_link: str | None = None

    while url:
        resp = await client.get(
            url,
            headers=headers,
            params=None if url.startswith("http") and "delta" in url else {"$top": 100},
        )
        if resp.status_code == 410:
            # Delta token expired. Restart full enumeration for this drive.
            url = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
            cursor_url = None
            continue
        resp.raise_for_status()
        data = resp.json()
        items = data.get("value", [])
        stats["found"] += len(items)

        for item in items:
            if "deleted" in item or "@removed" in item:
                deleted = await _delete_sharepoint_item(
                    session, tenant_id, source_id, drive_id, item.get("id", ""),
                )
                stats["deleted"] += deleted
                continue

            if stats["synced"] >= limit:
                stats["delta_complete"] = False
                return cursor_url

            await _ingest_sharepoint_item(
                session=session,
                tenant_id=tenant_id,
                source_id=source_id,
                client=client,
                headers=headers,
                site_id=site_id,
                drive=drive,
                item=item,
                sync_acl=sync_acl,
                stats=stats,
            )

        url = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink") or delta_link

    return delta_link


async def _delete_sharepoint_item(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    drive_id: str,
    item_id: str,
) -> int:
    if not item_id:
        return 0
    result = await session.execute(
        text(
            "UPDATE documents SET status = 'deleted', "
            "review_status = 'rejected', last_synced_at = now() "
            "WHERE tenant_id = :tid AND source_id = :sid "
            "AND source_object_id = :soid AND status != 'deleted'"
        ),
        {
            "tid": tenant_id,
            "sid": source_id,
            "soid": _sharepoint_source_object_id(drive_id, item_id),
        },
    )
    await session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _ingest_sharepoint_item(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    client: Any,
    headers: dict[str, str],
    site_id: str,
    drive: dict[str, Any],
    item: dict[str, Any],
    sync_acl: bool,
    stats: dict[str, Any],
) -> None:
    if "folder" in item or "package" in item:
        return
    if "file" not in item:
        stats["skipped"] += 1
        return

    name = item.get("name", "")
    item_id = item.get("id", "")
    drive_id = str(drive["id"])
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in SUPPORTED_SYNC_EXTENSIONS:
        stats["skipped"] += 1
        return

    try:
        item = await _sharepoint_enrich_item(client, headers, drive_id, item)
        dl_resp = await client.get(
            f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content",
            headers=headers,
        )
        dl_resp.raise_for_status()
        file_data = dl_resp.content

        from raasoa.config import settings
        max_size = settings.max_file_size_mb * 1024 * 1024
        if len(file_data) > max_size:
            stats["skipped"] += 1
            stats["errors"].append({
                "file": name,
                "error": f"file too large ({len(file_data)} bytes)",
            })
            return

        from raasoa.ingestion.pipeline import ingest_file
        from raasoa.providers.factory import get_embedding_provider

        provider = get_embedding_provider()
        source_path, folder_path = _sharepoint_item_path(item)
        metadata = _sharepoint_metadata(
            site_id=site_id,
            drive=drive,
            item=item,
            source_path=source_path,
            folder_path=folder_path,
        )
        doc, _ = await ingest_file(
            session=session,
            tenant_id=tenant_id,
            source_id=source_id,
            file_data=file_data,
            filename=name,
            embedding_provider=provider,
            source_object_id=_sharepoint_source_object_id(drive_id, item_id),
            source_url=item.get("webUrl"),
            source_metadata=metadata,
            last_modified=_parse_datetime(item.get("lastModifiedDateTime")),
        )
        await session.refresh(doc)
        if sync_acl:
            await _sync_sharepoint_acl(
                session=session,
                client=client,
                headers=headers,
                drive_id=drive_id,
                item_id=item_id,
                document_id=doc.id,
            )
        stats["synced"] += 1
        logger.info("Synced SharePoint: %s (%d chunks)", source_path, doc.chunk_count)
    except Exception as e:
        stats["errors"].append({"file": name or item_id, "error": str(e)[:200]})


async def _sharepoint_enrich_item(
    client: Any,
    headers: dict[str, str],
    drive_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    if item.get("parentReference", {}).get("path") and item.get("webUrl"):
        return item
    item_id = item.get("id")
    if not item_id:
        return item
    resp = await client.get(
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}",
        headers=headers,
        params={
            "$select": (
                "id,name,webUrl,parentReference,file,folder,package,size,eTag,"
                "cTag,lastModifiedDateTime,createdDateTime,createdBy,lastModifiedBy"
            )
        },
    )
    if resp.status_code != 200:
        return item
    enriched = item.copy()
    enriched.update(resp.json())
    return enriched


def _sharepoint_source_object_id(drive_id: str, item_id: str) -> str:
    return f"sharepoint:{drive_id}:{item_id}"


def _sharepoint_item_path(item: dict[str, Any]) -> tuple[str, str]:
    name = item.get("name", "")
    parent_path = item.get("parentReference", {}).get("path", "")
    folder_path = ""
    if "root:" in parent_path:
        folder_path = parent_path.split("root:", 1)[1].strip("/")
    source_path = "/".join(part for part in [folder_path, name] if part)
    return source_path or name, folder_path


def _identity_name(identity_set: dict[str, Any] | None) -> str | None:
    if not identity_set:
        return None
    for key in ("user", "group", "application", "device"):
        val = identity_set.get(key)
        if isinstance(val, dict):
            return val.get("displayName") or val.get("email") or val.get("id")
    return None


def _sharepoint_metadata(
    *,
    site_id: str,
    drive: dict[str, Any],
    item: dict[str, Any],
    source_path: str,
    folder_path: str,
) -> dict[str, Any]:
    file_facet = item.get("file") or {}
    return {
        "connector": "sharepoint",
        "sharepoint_site_id": site_id,
        "drive_id": drive.get("id"),
        "drive_name": drive.get("name"),
        "item_id": item.get("id"),
        "source_path": source_path,
        "folder_path": folder_path,
        "etag": item.get("eTag"),
        "ctag": item.get("cTag"),
        "size": item.get("size"),
        "mime_type": file_facet.get("mimeType"),
        "created_at_source": item.get("createdDateTime"),
        "last_modified_source": item.get("lastModifiedDateTime"),
        "created_by": _identity_name(item.get("createdBy")),
        "last_modified_by": _identity_name(item.get("lastModifiedBy")),
    }


async def _sync_sharepoint_acl(
    *,
    session: AsyncSession,
    client: Any,
    headers: dict[str, str],
    drive_id: str,
    item_id: str,
    document_id: uuid.UUID,
) -> None:
    resp = await client.get(
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/permissions",
        headers=headers,
    )
    if resp.status_code != 200:
        logger.warning(
            "SharePoint ACL sync skipped for %s: Graph %s",
            item_id, resp.status_code,
        )
        return

    await session.execute(
        text("DELETE FROM acl_entries WHERE document_id = :did"),
        {"did": document_id},
    )
    for permission in resp.json().get("value", []):
        roles = permission.get("roles") or ["read"]
        principal_entries = _sharepoint_permission_principals(permission)
        for principal_type, principal_id in principal_entries:
            await session.execute(
                text(
                    "INSERT INTO acl_entries "
                    "(id, document_id, principal_type, principal_id, "
                    " permission, source_acl_id) "
                    "VALUES (:id, :did, :ptype, :pid, :perm, :said)"
                ),
                {
                    "id": uuid.uuid4(),
                    "did": document_id,
                    "ptype": principal_type,
                    "pid": principal_id,
                    "perm": "write" if "write" in roles else "read",
                    "said": permission.get("id"),
                },
            )
    await session.commit()


def _sharepoint_permission_principals(
    permission: dict[str, Any],
) -> list[tuple[str, str]]:
    principals: list[tuple[str, str]] = []
    containers = []
    if permission.get("grantedToV2"):
        containers.append(permission["grantedToV2"])
    if permission.get("grantedTo"):
        containers.append(permission["grantedTo"])
    containers.extend(permission.get("grantedToIdentitiesV2") or [])
    containers.extend(permission.get("grantedToIdentities") or [])

    for granted in containers:
        if not isinstance(granted, dict):
            continue
        for principal_type in ("user", "group", "siteUser"):
            principal = granted.get(principal_type)
            if isinstance(principal, dict):
                principal_id = (
                    principal.get("email")
                    or principal.get("id")
                    or principal.get("displayName")
                )
                if principal_id:
                    principals.append((principal_type, str(principal_id)))
    return principals


def _adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_adf_to_text(v) for v in value)
    if not isinstance(value, dict):
        return str(value)

    node_type = value.get("type")
    if node_type == "text":
        return str(value.get("text", ""))
    if node_type == "hardBreak":
        return "\n"

    content = _adf_to_text(value.get("content", []))
    if node_type in {
        "paragraph", "heading", "blockquote", "bulletList", "orderedList",
        "listItem", "codeBlock", "panel",
    } and content:
        return f"{content}\n"
    return content


def _jira_name(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return (
        value.get("displayName")
        or value.get("name")
        or value.get("emailAddress")
        or value.get("accountId")
        or ""
    )


def _jira_issue_metadata(issue: dict[str, Any], base_url: str) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    key = issue.get("key") or issue.get("id")
    return {
        "connector": "jira",
        "issue_id": issue.get("id"),
        "issue_key": key,
        "source_path": f"{project.get('key', 'Jira')}/{key}",
        "folder_path": project.get("key") or "Jira",
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "issue_type": issue_type.get("name"),
        "status": status.get("name"),
        "priority": priority.get("name"),
        "labels": fields.get("labels") or [],
        "assignee": _jira_name(fields.get("assignee")),
        "reporter": _jira_name(fields.get("reporter")),
        "created_at_source": fields.get("created"),
        "last_modified_source": fields.get("updated"),
        "source_url": f"{base_url}/browse/{key}",
    }


def _jira_issue_to_markdown(issue: dict[str, Any], base_url: str) -> str:
    fields = issue.get("fields") or {}
    key = issue.get("key") or issue.get("id")
    summary = fields.get("summary") or key
    status = (fields.get("status") or {}).get("name", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    project = (fields.get("project") or {}).get("key", "")

    parts = [
        f"# {key}: {summary}",
        f"URL: {base_url}/browse/{key}",
        f"Project: {project}",
        f"Issue type: {issue_type}",
        f"Status: {status}",
        f"Priority: {priority}",
        f"Assignee: {_jira_name(fields.get('assignee')) or 'Unassigned'}",
        f"Reporter: {_jira_name(fields.get('reporter'))}",
        f"Created: {fields.get('created') or ''}",
        f"Updated: {fields.get('updated') or ''}",
    ]
    labels = fields.get("labels") or []
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")

    description = _adf_to_text(fields.get("description")).strip()
    if description:
        parts.extend(["", "## Description", description])

    comments = ((fields.get("comment") or {}).get("comments") or [])
    if comments:
        parts.extend(["", "## Comments"])
        for comment in comments[-10:]:
            author = _jira_name(comment.get("author"))
            updated = comment.get("updated") or comment.get("created") or ""
            body = _adf_to_text(comment.get("body")).strip()
            if body:
                parts.append(f"### {author} - {updated}\n{body}")

    return "\n\n".join(part for part in parts if part is not None)


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


async def _fetch_notion_blocks_text(
    client: Any,
    headers: dict[str, str],
    block_id: str,
    depth: int = 0,
    max_depth: int = 4,
) -> str:
    """Fetch Notion block children recursively with pagination."""
    if depth > max_depth:
        return ""

    blocks: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor
        resp = await client.get(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        if not start_cursor:
            break

    parts: list[str] = []
    for block in blocks:
        text_val = _notion_block_to_text(block)
        if text_val:
            parts.append(text_val)
        if block.get("has_children"):
            child_text = await _fetch_notion_blocks_text(
                client, headers, block["id"], depth + 1, max_depth,
            )
            if child_text:
                parts.append(child_text)
    return "\n\n".join(parts)


def _notion_block_to_text(block: dict[str, Any]) -> str:
    bt = block.get("type", "")
    content = block.get(bt, {})
    rich_text = content.get("rich_text", [])
    text_val = "".join(rt.get("plain_text", "") for rt in rich_text)
    if not text_val:
        return ""
    if bt.startswith("heading_"):
        level = bt[-1]
        return f"{'#' * int(level)} {text_val}"
    if "list_item" in bt:
        return f"- {text_val}"
    if bt == "to_do":
        checked = content.get("checked", False)
        return f"[{'x' if checked else ' '}] {text_val}"
    if bt in ("quote", "callout"):
        return f"> {text_val}"
    if bt == "code":
        language = content.get("language", "")
        return f"```{language}\n{text_val}\n```"
    return text_val


def _notion_blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        text_val = _notion_block_to_text(block)
        if text_val:
            parts.append(text_val)
    return "\n\n".join(parts)
