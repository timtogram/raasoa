"""Source connector management API.

Create, configure, and sync data sources from the dashboard or API.
Each source's connection config (tokens, URLs, filters) has its
credential fields (token/client_secret/api_token) encrypted at rest via
raasoa.security.crypto when CONNECTOR_ENCRYPTION_KEY is configured --
see that module for the exact scheme and the plaintext fallback when no
key is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.connectors.net import UnsafeConnectorUrlError, validate_outbound_url
from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceCreate(BaseModel):
    source_type: str = Field(
        ..., description="Type: notion, sharepoint, jira, hubspot, confluence, webhook, custom",
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
    auto_index: bool = Field(
        default=True,
        description=(
            "Immediately run a sync after connecting, so data-quality "
            "problems (low scores, contradictions) surface right away "
            "instead of waiting for the next scheduled sync."
        ),
    )
    sync_query: str = Field(
        default="*", description="Search/filter query for the initial sync.",
    )
    sync_limit: int = Field(
        default=50, ge=1, le=500,
        description="Max records to pull in the initial sync.",
    )
    default_visibility: str | None = Field(
        default=None,
        description=(
            "'inherit' (open unless a document has its own ACL) or "
            "'restricted' (invisible without an explicit grant). Defaults "
            "to 'restricted' for hubspot — CRM records are owner-sensitive "
            "by nature — and 'inherit' for every other source type."
        ),
    )


class ConflictSummary(BaseModel):
    conflict_type: str
    confidence: float | None
    document_a_title: str | None
    document_b_title: str | None


class IndexingReport(BaseModel):
    """Immediate data-quality snapshot after connecting a source."""

    sync_status: str
    documents_synced: int
    documents_skipped: int
    sync_errors: list[dict[str, Any]]
    avg_quality_score: float | None
    critical_findings: int
    warning_findings: int
    new_conflicts: int
    top_conflicts: list[ConflictSummary]


class SourceResponse(BaseModel):
    id: str
    source_type: str
    name: str
    config_keys: list[str]  # Only show which keys are set, not values
    document_count: int
    last_sync: str | None
    sync_status: str
    default_visibility: str = "inherit"
    indexing_report: IndexingReport | None = None


class SourceVisibilityUpdate(BaseModel):
    default_visibility: str = Field(
        ..., description="'inherit' (open by default) or 'restricted'",
    )
    grant_principal_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Principals to grant whole-source read access to in the same "
            "call (inserted into source_acl_grants)."
        ),
    )


class SyncRequest(BaseModel):
    query: str = Field(
        default="*", description="Search query to filter what gets synced",
    )
    limit: int = Field(default=50, ge=1, le=500)


async def _build_indexing_report(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_status: str,
    sync_stats: dict[str, Any],
) -> IndexingReport:
    """Summarize data quality for the documents just synced from a source.

    This is what turns "connect a source" into an immediate signal: the
    admin sees average quality, how many findings are serious, and whether
    any of the freshly-ingested documents already contradict something —
    without waiting for a separate report or dashboard visit.
    """
    quality_result = await session.execute(
        text(
            "SELECT ROUND(AVG(quality_score)::numeric, 3) AS avg_score, "
            "COUNT(*) AS total "
            "FROM documents WHERE tenant_id = :tid AND source_id = :sid "
            "AND status != 'deleted' AND quality_score IS NOT NULL"
        ),
        {"tid": tenant_id, "sid": source_id},
    )
    q_row = quality_result.first()
    avg_score = float(q_row.avg_score) if q_row and q_row.avg_score is not None else None

    findings_result = await session.execute(
        text(
            "SELECT qf.severity, COUNT(*) AS cnt FROM quality_findings qf "
            "JOIN documents d ON d.id = qf.document_id "
            "WHERE d.tenant_id = :tid AND d.source_id = :sid "
            "GROUP BY qf.severity"
        ),
        {"tid": tenant_id, "sid": source_id},
    )
    severity_counts = {r.severity: r.cnt for r in findings_result.fetchall()}

    conflicts_result = await session.execute(
        text(
            "SELECT cc.conflict_type, cc.confidence, "
            "  da.title AS title_a, db.title AS title_b "
            "FROM conflict_candidates cc "
            "JOIN documents da ON da.id = cc.document_a_id "
            "JOIN documents db ON db.id = cc.document_b_id "
            "WHERE cc.tenant_id = :tid "
            "AND (da.source_id = :sid OR db.source_id = :sid) "
            "AND cc.status = 'new' "
            "ORDER BY cc.confidence DESC NULLS LAST LIMIT 3"
        ),
        {"tid": tenant_id, "sid": source_id},
    )
    top_conflicts = [
        ConflictSummary(
            conflict_type=r.conflict_type,
            confidence=float(r.confidence) if r.confidence is not None else None,
            document_a_title=r.title_a,
            document_b_title=r.title_b,
        )
        for r in conflicts_result.fetchall()
    ]

    conflict_count_result = await session.execute(
        text(
            "SELECT COUNT(*) FROM conflict_candidates cc "
            "JOIN documents da ON da.id = cc.document_a_id "
            "JOIN documents db ON db.id = cc.document_b_id "
            "WHERE cc.tenant_id = :tid "
            "AND (da.source_id = :sid OR db.source_id = :sid) "
            "AND cc.status = 'new'"
        ),
        {"tid": tenant_id, "sid": source_id},
    )
    new_conflicts = conflict_count_result.scalar() or 0

    return IndexingReport(
        sync_status=sync_status,
        documents_synced=sync_stats.get("synced", 0),
        documents_skipped=sync_stats.get("skipped", 0),
        sync_errors=sync_stats.get("errors", []),
        avg_quality_score=avg_score,
        critical_findings=severity_counts.get("critical", 0),
        warning_findings=severity_counts.get("warning", 0),
        new_conflicts=new_conflicts,
        top_conflicts=top_conflicts,
    )


@router.post("", response_model=SourceResponse)
async def create_source(
    request: Request,
    body: SourceCreate,
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    """Create a new data source connection (admin-only).

    By default (auto_index=True) also runs an immediate sync and returns
    a data-quality snapshot — connecting a source and seeing "12 documents
    indexed, 2 already contradict each other" happens in one call.

    Admin-gated because a source's connection config can point outbound
    requests (e.g. Jira's ``base_url``) at an arbitrary host — see
    ``raasoa.connectors.net`` — so creating one is a privileged action.
    Unlike ``raasoa.api.admin.require_admin``, this does NOT also require
    ``tenants.admin_api_enabled``: that flag opts a tenant into the
    delegated Admin API (personal keys, groups), a separate concern from
    a trusted master/legacy key performing a basic tenant operation.
    """
    from raasoa.security.principal import resolve_principal_async

    principal = await resolve_principal_async(request)
    if not (principal.is_legacy_tenant_wide or principal.is_admin):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    tenant_id = principal.tenant_id

    if body.source_type == "jira":
        try:
            validate_outbound_url(
                (body.config.get("base_url") or "").rstrip("/"),
                field_name="config.base_url",
            )
        except UnsafeConnectorUrlError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # Quota check: source limit
    from raasoa.middleware.metering import check_quota
    allowed, reason = await check_quota(session, tenant_id, "sources")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    if body.default_visibility is not None and body.default_visibility not in (
        "inherit", "restricted",
    ):
        raise HTTPException(
            status_code=400,
            detail="default_visibility must be 'inherit' or 'restricted'",
        )
    default_visibility = body.default_visibility or (
        "restricted" if body.source_type == "hubspot" else "inherit"
    )

    source_id = uuid.uuid4()
    from raasoa.security.crypto import encrypt_sensitive_config

    await session.execute(
        text(
            "INSERT INTO sources "
            "(id, tenant_id, source_type, name, connection_config, default_visibility) "
            "VALUES (:id, :tid, :stype, :name, CAST(:config AS jsonb), :vis)"
        ),
        {
            "id": source_id,
            "tid": tenant_id,
            "stype": body.source_type,
            "name": body.name,
            "config": json.dumps(encrypt_sensitive_config({
                **body.config,
                **(
                    {"sync_interval_minutes": body.sync_interval_minutes}
                    if body.sync_interval_minutes is not None
                    else {}
                ),
            })),
            "vis": default_visibility,
        },
    )
    await session.commit()

    if not body.auto_index:
        return SourceResponse(
            id=str(source_id),
            source_type=body.source_type,
            name=body.name,
            config_keys=list(body.config.keys()),
            document_count=0,
            last_sync=None,
            sync_status="idle",
            default_visibility=default_visibility,
        )

    try:
        stats = await _dispatch_sync(
            body.source_type, session, tenant_id, source_id,
            body.config, body.sync_query, body.sync_limit,
        )
    except Exception as e:
        logger.exception("Auto-index failed for new source %s", source_id)
        stats = {"status": "error", "message": str(e)[:300], "synced": 0}

    has_results = stats.get("synced", 0) > 0
    is_unsupported = stats.get("status") == "unsupported"
    is_error = stats.get("status") == "error"
    sync_status = "error" if is_error else (
        "completed" if has_results or is_unsupported else "empty"
    )
    await session.execute(
        text(
            "INSERT INTO sync_cursors (source_type, source_id, sync_status, items_synced) "
            "VALUES (:stype, :sid, :status, :count) "
            "ON CONFLICT (source_type, source_id) "
            "DO UPDATE SET sync_status = :status, items_synced = :count, "
            "last_sync_at = now()"
        ),
        {
            "stype": body.source_type,
            "sid": source_id,
            "status": sync_status,
            "count": stats.get("synced", 0),
        },
    )
    await session.commit()

    doc_count_result = await session.execute(
        text(
            "SELECT COUNT(*) FROM documents "
            "WHERE tenant_id = :tid AND source_id = :sid AND status != 'deleted'"
        ),
        {"tid": tenant_id, "sid": source_id},
    )
    doc_count = doc_count_result.scalar() or 0

    report = await _build_indexing_report(
        session, tenant_id, source_id, sync_status, stats,
    )

    return SourceResponse(
        id=str(source_id),
        source_type=body.source_type,
        name=body.name,
        config_keys=list(body.config.keys()),
        document_count=doc_count,
        last_sync="now" if has_results else None,
        sync_status=sync_status,
        default_visibility=default_visibility,
        indexing_report=report,
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
            "  s.default_visibility, "
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
            default_visibility=r.default_visibility or "inherit",
        )
        for r in result.fetchall()
    ]


@router.patch("/{source_id}/visibility")
async def update_source_visibility(
    request: Request,
    source_id: uuid.UUID,
    body: SourceVisibilityUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Admin-only: set a source's default visibility and optionally grant
    whole-source read access to one or more principals in the same call."""
    from raasoa.api.admin import require_admin

    principal = await require_admin(request, session)
    if body.default_visibility not in ("inherit", "restricted"):
        raise HTTPException(
            status_code=400,
            detail="default_visibility must be 'inherit' or 'restricted'",
        )

    result = await session.execute(
        text(
            "UPDATE sources SET default_visibility = :vis "
            "WHERE id = :sid AND tenant_id = :tid RETURNING id"
        ),
        {"vis": body.default_visibility, "sid": source_id, "tid": principal.tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Source not found")

    for pid in body.grant_principal_ids:
        await session.execute(
            text(
                "INSERT INTO source_acl_grants "
                "(id, tenant_id, source_id, principal_id, permission) "
                "VALUES (:id, :tid, :sid, :pid, 'read') "
                "ON CONFLICT (tenant_id, source_id, principal_id) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(), "tid": principal.tenant_id,
                "sid": source_id, "pid": pid,
            },
        )
    await session.commit()
    return {
        "status": "updated",
        "source_id": str(source_id),
        "default_visibility": body.default_visibility,
        "grants_added": body.grant_principal_ids,
    }


@router.delete("/{source_id}")
async def delete_source(
    request: Request,
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Delete a data source (does NOT delete its documents).

    documents.source_id has no ON DELETE rule, so Postgres refuses the
    delete outright (and used to surface as a bare 500) if any documents
    still reference this source. Checked explicitly here for a clear,
    actionable error instead.
    """
    tenant_id = await resolve_tenant_async(request)

    doc_count_result = await session.execute(
        text(
            "SELECT count(*) AS n FROM documents "
            "WHERE source_id = :sid AND tenant_id = :tid"
        ),
        {"sid": source_id, "tid": tenant_id},
    )
    doc_count = doc_count_result.scalar_one()
    if doc_count > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete source: {doc_count} document(s) still "
                "reference it. Delete those documents first, or use "
                "PATCH /{source_id}/visibility if you only need to "
                "change access."
            ),
        )

    try:
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
    except IntegrityError:
        # A document was inserted for this source in the window between
        # the count check above and this delete — rare, but the FK has
        # no ON DELETE rule so Postgres would otherwise surface this as
        # an unhandled 500.
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail="Cannot delete source: a document was just added to "
            "it. Delete its documents first and try again.",
        ) from None
    return {"status": "deleted", "id": str(source_id)}


async def _cascade_delete_document_data(
    session: AsyncSession,
    document_ids: list[uuid.UUID],
) -> None:
    """Delete rows that are keyed off a document but not FK-cascaded.

    ``chunks`` and ``claims`` already cascade on ``documents`` deletion via
    ``ON DELETE CASCADE`` foreign keys, and are cleaned up here as well for
    symmetry/defense-in-depth for callers that only soft-delete (set
    ``status = 'deleted'``) rather than hard-deleting the document row.
    ``acl_entries`` and ``crm_objects`` have NO foreign key to
    ``documents`` at all, so without this explicit cleanup a deleted
    document's ACL grants (e.g. a HubSpot record owner's read access) and
    its CRM object row persist forever, and orphaned chunks/claims remain
    fully queryable by any code path that doesn't filter on document
    status.
    """
    if not document_ids:
        return
    await session.execute(
        text("DELETE FROM acl_entries WHERE document_id = ANY(:dids)"),
        {"dids": document_ids},
    )
    await session.execute(
        text("DELETE FROM crm_objects WHERE document_id = ANY(:dids)"),
        {"dids": document_ids},
    )
    await session.execute(
        text("DELETE FROM chunks WHERE document_id = ANY(:dids)"),
        {"dids": document_ids},
    )
    await session.execute(
        text("DELETE FROM claims WHERE document_id = ANY(:dids)"),
        {"dids": document_ids},
    )


async def _dispatch_sync(
    source_type: str,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    config: dict[str, Any],
    query: str,
    limit: int,
) -> dict[str, Any]:
    """Route to the connector-specific sync implementation.

    Shared by the explicit POST /sources/{id}/sync endpoint and the
    auto-index-on-connect flow in create_source, so both paths behave
    identically.
    """
    if source_type == "notion":
        return await _sync_notion(session, tenant_id, source_id, config, query, limit)
    if source_type == "sharepoint":
        return await _sync_sharepoint(session, tenant_id, source_id, config, query, limit)
    if source_type == "jira":
        return await _sync_jira(session, tenant_id, source_id, config, query, limit)
    if source_type == "hubspot":
        return await _sync_hubspot(session, tenant_id, source_id, config, query, limit)
    return {
        "status": "unsupported",
        "message": f"Auto-sync not available for {source_type}. Use webhooks.",
    }


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

    from raasoa.security.crypto import decrypt_sensitive_config

    config = decrypt_sensitive_config(source.connection_config)

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
        stats = await _dispatch_sync(
            source.source_type, session, tenant_id, source_id, config,
            body.query, body.limit,
        )

        # Update sync status. A connector that reports delta_complete=False
        # (i.e. its backlog didn't fit in this one call's limit) must be
        # marked "incomplete", not "completed" — otherwise an admin sees a
        # falsely reassuring status while the sync is nowhere near caught
        # up, and the scheduler has no signal to retry it sooner than the
        # normal interval.
        has_results = stats.get("synced", 0) > 0
        is_unsupported = stats.get("status") == "unsupported"
        is_error = stats.get("status") == "error"
        delta_complete = stats.get("delta_complete", True)
        status = "error" if is_error else (
            "incomplete" if not delta_complete else
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
        raise HTTPException(
            status_code=500,
            detail="Sync failed - check server logs for details",
        ) from e


async def _mark_notion_pages_deleted(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    active_page_ids: set[str],
) -> int:
    """Soft-delete documents whose Notion page no longer appears in a
    full-workspace search — archived, moved to trash, or unshared from
    the integration.

    Unlike SharePoint's delta feed (which explicitly marks removed items
    with "@removed"/"deleted"), Notion's search API gives no equivalent
    deletion signal — it simply omits pages that no longer match. The
    only way to detect a removal is comparing what search currently
    returns against what we previously ingested. ``active_page_ids``
    must come from a full, unscoped search (see the ``query in
    ("*", "", None)`` guard at the only call site) — comparing against a
    narrower/filtered query's results would incorrectly mark everything
    outside that query's scope as deleted.
    """
    if not active_page_ids:
        # An empty active set from a genuinely full search would nuke an
        # entire workspace's worth of documents on a transient empty
        # response — never treat "found nothing" as "delete everything".
        return 0

    active_object_ids = [f"notion:{pid}" for pid in active_page_ids]
    result = await session.execute(
        text(
            "UPDATE documents SET status = 'deleted', "
            "review_status = 'rejected', last_synced_at = now() "
            "WHERE tenant_id = :tid AND source_id = :sid "
            "AND source_object_id LIKE 'notion:%' "
            "AND status != 'deleted' "
            "AND NOT (source_object_id = ANY(:active)) "
            "RETURNING id"
        ),
        {"tid": tenant_id, "sid": source_id, "active": active_object_ids},
    )
    deleted_doc_ids = [row.id for row in result.fetchall()]
    await _cascade_delete_document_data(session, deleted_doc_ids)
    await session.commit()
    return len(deleted_doc_ids)


_NOTION_MAX_RETRIES = 3
_NOTION_RETRY_DELAY = 1.0
_NOTION_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


async def _notion_request_with_retry(
    method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any,
) -> Any:
    """Call a Notion API request (``client.post``/``client.get``) with
    retry + backoff on rate limiting (429) and transient server errors
    (5xx) -- previously any of these aborted the sync immediately,
    discarding every page already fetched into ``results`` this run.

    Honors a numeric ``Retry-After`` header when Notion provides one
    (rate-limit responses commonly do); otherwise falls back to
    exponential backoff. Returns the final response as-is (even one that
    never succeeded) after exhausting retries, so callers keep their
    existing status-code handling unchanged.
    """
    resp = None
    for attempt in range(_NOTION_MAX_RETRIES):
        resp = await method(*args, **kwargs)
        if resp.status_code not in _NOTION_RETRYABLE_STATUS:
            return resp
        if attempt < _NOTION_MAX_RETRIES - 1:
            retry_after = getattr(resp, "headers", {}).get("Retry-After")
            delay = _NOTION_RETRY_DELAY * 2**attempt
            if retry_after:
                with contextlib.suppress(ValueError):
                    delay = float(retry_after)
            await asyncio.sleep(delay)
    return resp


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
        "unchanged": 0, "deleted": 0, "errors": [],
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
        # Search Notion — follow next_cursor/has_more until Notion reports
        # there are no more results, so workspaces with more than one page
        # of results (>100 items) get fully ingested instead of silently
        # truncating at the first page.
        results: list[dict[str, Any]] = []
        next_cursor: str | None = None
        # Whether this run's `results` genuinely represents the complete,
        # current search result set (Notion reported has_more=False) --
        # as opposed to being truncated early by the `limit` cap below.
        # Deletion detection further down must only trust a complete
        # listing; a `limit`-truncated partial one says nothing about
        # pages beyond what was fetched, and would otherwise wrongly mark
        # them as deleted.
        search_complete = True
        while True:
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
            if next_cursor:
                search_body["start_cursor"] = next_cursor

            resp = await _notion_request_with_retry(
                client.post,
                "https://api.notion.com/v1/search",
                headers=headers,
                json=search_body,
            )
            if resp.status_code != 200:
                return {
                    "status": "error",
                    "message": f"Notion API {resp.status_code}",
                }

            page_data = resp.json()
            results.extend(page_data.get("results", []))
            if len(results) >= limit:
                # `sync_limit` is documented as "Max records to pull in
                # the initial sync" -- previously ignored beyond setting
                # page_size, so an admin expecting a bounded test sync
                # got Notion's entire matching result set instead.
                search_complete = False
                break
            if not page_data.get("has_more"):
                break
            next_cursor = page_data.get("next_cursor")
            if not next_cursor:
                break

        stats["found"] = len(results)
        provider = get_embedding_provider()
        newest_edited_time: str | None = None
        # If ANY page in this batch fails to fetch its blocks, the cursor
        # must not advance at all this run -- not just skip crediting
        # that one page's own timestamp. Results are sorted
        # newest-last_edited_time-first once a cursor exists, so a
        # transient failure on an OLDER page sitting next to a NEWER page
        # that succeeds would otherwise still let the cursor jump past
        # the older page's timestamp (advancing to the newer success),
        # permanently classifying the older, never-actually-synced page
        # as "unchanged" on every future sync. See test_notion_delta_cursor_retry.py.
        any_block_fetch_failed = False

        for page in results:
            if page.get("object") != "page":
                stats["skipped"] += 1
                continue

            page_id = page["id"]
            title = _notion_title(page)

            # Extract rich metadata
            meta = _notion_metadata(page)

            # Normalize before comparing — Notion returns trailing-Z
            # timestamps while a previously-stored cursor may be in
            # Python's +00:00 offset form (or vice versa); comparing the
            # raw strings would misclassify changed pages as unchanged
            # (or the reverse).
            page_edited_norm = _normalize_timestamp(meta.get("last_edited_time"))
            last_sync_norm = _normalize_timestamp(last_sync_token)

            # Delta-sync: skip if not changed since last sync
            if (
                last_sync_norm
                and page_edited_norm
                and page_edited_norm <= last_sync_norm
            ):
                stats["unchanged"] += 1
                continue

            # Fetch page blocks. A transient failure here (network blip,
            # 429, 5xx) degrades to title-only content rather than
            # skipping the page outright, but must also block the delta
            # cursor from advancing this run (see any_block_fetch_failed
            # above) -- otherwise this page ends up ingested with only
            # its title, permanently marked "caught up" by a DIFFERENT,
            # newer page's success in the same batch, and never retried
            # even though a subsequent sync would likely fetch it fine.
            try:
                content = await _fetch_notion_blocks_text(client, headers, page_id)
            except Exception:
                content = title
                any_block_fetch_failed = True

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
            # Custom database properties (select/people/date/url/rich_text/
            # number/checkbox/email/phone_number/formula) -- previously
            # captured only into doc_metadata (exact-match structured
            # filtering), never into the ingested text itself, so a
            # database row's "Priority: High" or "Assignee: Alice" was
            # invisible to semantic/hybrid search and RAG answers.
            for prop_label, prop_value in (meta.get("custom_properties") or {}).items():
                meta_header += f"{prop_label}: {prop_value}\n"

            file_content = f"# {title}\n"
            if meta_header:
                file_content += f"\n{meta_header}\n"
            file_content += f"\n{content}"

            # Check the length threshold on the FULLY assembled file, not
            # just the block-derived body text -- a database row (task
            # tracker item, CRM record) often has a short title and
            # little-to-no page body, with all its real information
            # living in properties captured above in meta_header. Checking
            # `content` alone (the old behavior) silently dropped every
            # such row entirely, which for a database-heavy workspace can
            # mean most rows never make it into the corpus at all.
            if len(file_content.strip()) < 50:
                stats["skipped"] += 1
                continue

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
                if page_edited_norm and (
                    newest_edited_time is None or page_edited_norm > newest_edited_time
                ):
                    newest_edited_time = page_edited_norm
                logger.info(
                    "Synced Notion: %s (%d chunks)", title, doc.chunk_count,
                )
            except Exception as e:
                stats["errors"].append(
                    {"page": title, "error": str(e)[:200]},
                )

    # Deletion/archival detection: only safe for a full, unscoped, and
    # COMPLETE search (query in ("*", "", None) AND search_complete) --
    # `results` then represents everything Notion's search currently
    # returns for this integration, so anything previously synced but now
    # absent has been archived, trashed, or unshared. A narrower/filtered
    # query's results say nothing about pages outside that query's scope,
    # and a `limit`-truncated partial listing says nothing about pages
    # beyond what was fetched -- running this for either would wrongly
    # mark unrelated/not-yet-fetched documents as deleted.
    if (not query or query == "*") and search_complete:
        active_page_ids = {
            page["id"] for page in results if page.get("object") == "page"
        }
        deleted_count = await _mark_notion_pages_deleted(
            session, tenant_id, source_id, active_page_ids,
        )
        stats["deleted"] += deleted_count

    # Notion's search gives no per-page "did this fully succeed" signal
    # of its own, so this is the connector's honest answer to whether the
    # whole batch is safe to consider caught up (mirrors _sync_sharepoint's
    # delta_complete; the scheduler/manual-sync status computation already
    # reads this key generically via stats.get("delta_complete", True)).
    stats["delta_complete"] = not any_block_fetch_failed

    # Update delta token for next sync. Use the max last_edited_time
    # actually seen among the pages processed in this sync — NOT
    # wall-clock "now" — so that (a) edits made between the search call
    # and this write aren't silently skipped next time (their
    # last_edited_time is always <= now, but may be > the previous
    # cursor), and (b) pages beyond the first page of results that
    # weren't reached yet (e.g. sync_limit cut the run short) are never
    # wrongly treated as already-synced just because the cursor jumped to
    # "now".
    if stats["synced"] > 0 and newest_edited_time:
        if any_block_fetch_failed:
            # A transient failure fetching SOME page's blocks means we
            # can't be sure the whole batch up to newest_edited_time
            # actually completed -- advancing the cursor anyway would
            # permanently classify that page (and it alone, chronologically
            # earlier than whatever succeeded) as "unchanged" on every
            # future sync, even though a retry would likely fetch it fine.
            # Leaving delta_token untouched means every page in this batch
            # -- successes included -- gets re-examined next sync, a cheap,
            # harmless no-op for the ones already fully ingested thanks to
            # ingest_file's content-hash dedup.
            new_token = last_sync_token
        else:
            # Never move the cursor backwards relative to what was already
            # stored, in case of a mixed-format comparison edge case.
            new_token = newest_edited_time
            if last_sync_token:
                last_sync_norm_final = _normalize_timestamp(last_sync_token)
                if last_sync_norm_final and last_sync_norm_final > new_token:
                    new_token = last_sync_norm_final

        await session.execute(
            text(
                "INSERT INTO sync_cursors "
                "(source_type, source_id, delta_token, "
                " last_sync_at, sync_status, items_synced) "
                "VALUES ('notion', :sid, :token, now(), "
                " :status, :count) "
                "ON CONFLICT (source_type, source_id) "
                "DO UPDATE SET delta_token = :token, "
                "  last_sync_at = now(), "
                "  sync_status = :status, "
                "  items_synced = :count"
            ),
            {
                "sid": source_id,
                "token": new_token,
                "status": "completed" if not any_block_fetch_failed else "incomplete",
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
    - sync_lists: Optional, default False — also discover and ingest
      SharePoint Lists (a separate Graph API surface from document
      libraries; structured data like trackers/indexes/directories that
      drives never cover). Opt-in like sync_acl, so existing deployments
      aren't surprised by new content suddenly appearing.
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
        ordered_drives = _rotate_drives(
            drives, cursor_map.get("__last_first_drive__"),
        )

        if query and query != "*":
            for drive in ordered_drives:
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

        # Persist the rotation marker immediately, before any network call,
        # so it advances on every call regardless of how much of the call
        # actually completes — this is what guarantees every drive gets
        # first-in-line budget priority at least once every len(drives)
        # calls, instead of API-return order permanently starving
        # non-first drives.
        cursor_map["__last_first_drive__"] = str(ordered_drives[0]["id"])
        await _persist_sharepoint_cursor_map(
            session, source_id, cursor_map, stats["synced"], "running",
        )

        for drive in ordered_drives:
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
            # Persist THIS drive's advanced cursor immediately, not batched
            # until every drive in the call finishes — previously the
            # entire cursor_map was discarded when a later drive hit the
            # limit (the overwhelmingly common case, since that's the
            # whole point of a limit), silently re-processing every
            # already-completed drive's items again on the next sync.
            if delta_link:
                cursor_map[drive["id"]] = delta_link
                await _persist_sharepoint_cursor_map(
                    session, source_id, cursor_map, stats["synced"], "running",
                )

        # SharePoint Lists — a completely separate Graph API surface from
        # drives, opt-in via sync_lists (like sync_acl) so existing
        # deployments aren't surprised by new content suddenly appearing.
        # A full re-listing each call (not a delta sync, see
        # _sync_sharepoint_list_items), gated the same way as drives on
        # the remaining `limit` budget.
        if config.get("sync_lists", False):
            lists = await _sharepoint_lists(client, headers, site_id)
            all_lists_complete = True
            active_list_item_ids: set[str] = set()
            for list_obj in lists:
                if stats["synced"] >= limit:
                    all_lists_complete = False
                    break
                item_ids, list_complete = await _sync_sharepoint_list_items(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    client=client,
                    headers=headers,
                    site_id=site_id,
                    list_obj=list_obj,
                    limit=limit,
                    stats=stats,
                )
                active_list_item_ids |= item_ids
                all_lists_complete = all_lists_complete and list_complete

            # Deletion detection only if every list was FULLY listed this
            # call -- a limit-truncated partial listing says nothing
            # about items beyond what was fetched, and would otherwise
            # wrongly mark them as deleted (same reasoning as Notion's
            # search_complete gate).
            if all_lists_complete:
                deleted_count = await _mark_sharepoint_list_items_deleted(
                    session, tenant_id, source_id, active_list_item_ids,
                )
                stats["deleted"] += deleted_count

        # Always leave an accurate final status, not just on success — a
        # source whose backlog didn't fit in this call's limit must be
        # visibly "incomplete" (distinct from "running", which means a
        # call is actively in flight right now), so callers/schedulers
        # know to retry rather than believing the sync fully caught up.
        await _persist_sharepoint_cursor_map(
            session, source_id, cursor_map, stats["synced"],
            "completed" if stats["delta_complete"] else "incomplete",
        )

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

    try:
        validate_outbound_url(base_url, field_name="base_url")
    except UnsafeConnectorUrlError as e:
        return {"status": "error", "message": str(e)}

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


HUBSPOT_DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "deals": [
        "dealname", "amount", "dealstage", "pipeline", "closedate",
        "hubspot_owner_id", "createdate", "hs_lastmodifieddate",
        "dealtype", "description",
    ],
    "contacts": [
        "firstname", "lastname", "email", "jobtitle", "company",
        "phone", "lifecyclestage", "hubspot_owner_id",
        "createdate", "hs_lastmodifieddate",
    ],
    "companies": [
        "name", "domain", "industry", "city", "country",
        "numberofemployees", "hubspot_owner_id",
        "createdate", "hs_lastmodifieddate",
    ],
    "tickets": [
        "subject", "content", "hs_pipeline", "hs_pipeline_stage",
        "hs_ticket_priority", "hubspot_owner_id",
        "createdate", "hs_lastmodifieddate",
    ],
}

HUBSPOT_TITLE_PROPERTY: dict[str, str] = {
    "deals": "dealname",
    "contacts": "email",
    "companies": "name",
    "tickets": "subject",
}


def _hubspot_record_title(object_type: str, properties: dict[str, Any]) -> str:
    prop = HUBSPOT_TITLE_PROPERTY.get(object_type, "name")
    title = properties.get(prop)
    if title:
        return str(title)
    if object_type == "contacts":
        name = " ".join(
            p for p in (properties.get("firstname"), properties.get("lastname")) if p
        )
        if name:
            return name
    singular = object_type[:-1] if object_type.endswith("s") else object_type
    return f"{singular} {properties.get('hs_object_id', '?')}"


def _hubspot_record_to_markdown(
    object_type: str, record_id: str, properties: dict[str, Any],
) -> str:
    title = _hubspot_record_title(object_type, properties)
    singular = object_type[:-1] if object_type.endswith("s") else object_type
    lines = [f"# {title}", "", f"HubSpot object type: {singular}"]
    for key, value in properties.items():
        if value in (None, ""):
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


async def _sync_hubspot(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    config: dict[str, Any],
    query: str,
    limit: int,
) -> dict[str, Any]:
    """Sync HubSpot CRM objects (deals, contacts, companies, tickets).

    Config requires:
    - token: HubSpot private-app access token (Bearer)
    - objects: optional list, subset of ["deals","contacts","companies","tickets"].
      Defaults to all four.
    - properties: optional dict[object_type -> list[str]] to override the
      default property set fetched per object type.

    Each record is ingested as a document (so RAG/hybrid search can find it),
    with its raw CRM properties preserved in doc_metadata for structured
    filtering. Delta-synced via hs_lastmodifieddate. Each record's
    hubspot_owner_id (if present) becomes an ACL grant — HubSpot CRM data is
    per-owner sensitive by nature, so access is inherited from the source's
    own ownership model rather than left open by default.
    """
    import httpx

    token = config.get("token", "")
    if not token:
        return {
            "status": "error",
            "message": "No HubSpot token configured. Set config.token to a "
            "private-app access token.",
        }

    object_types = config.get("objects") or list(HUBSPOT_DEFAULT_PROPERTIES.keys())
    object_types = [o for o in object_types if o in HUBSPOT_DEFAULT_PROPERTIES]
    if not object_types:
        return {
            "status": "error",
            "message": (
                "No valid HubSpot object types configured. "
                f"Choose from: {list(HUBSPOT_DEFAULT_PROPERTIES.keys())}"
            ),
        }

    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    stats: dict[str, Any] = {
        "found": 0, "synced": 0, "skipped": 0,
        "unchanged": 0, "errors": [], "by_object_type": {},
    }
    provider = get_embedding_provider()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Per-object-type delta cursor, stored as JSON in the shared delta_token column
    cursor_result = await session.execute(
        text(
            "SELECT delta_token FROM sync_cursors "
            "WHERE source_id = :sid AND source_type = 'hubspot'"
        ),
        {"sid": source_id},
    )
    cursor_row = cursor_result.first()
    cursors: dict[str, str] = {}
    if cursor_row and cursor_row.delta_token:
        try:
            parsed = json.loads(cursor_row.delta_token)
            if isinstance(parsed, dict):
                cursors = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass

    new_cursors: dict[str, str] = dict(cursors)

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        for object_type in object_types:
            per_type_synced = 0
            props = (config.get("properties") or {}).get(
                object_type, HUBSPOT_DEFAULT_PROPERTIES[object_type],
            )
            last_sync = cursors.get(object_type)
            search_body: dict[str, Any] = {
                "limit": min(100, limit),
                "properties": props,
                "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            }
            if query and query != "*":
                search_body["query"] = query
            if last_sync:
                search_body["filterGroups"] = [{
                    "filters": [{
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GT",
                        "value": last_sync,
                    }],
                }]

            after: str | None = None
            newest_seen = last_sync

            while per_type_synced < limit:
                body = dict(search_body)
                if after:
                    body["after"] = after

                resp = await client.post(
                    f"https://api.hubapi.com/crm/v3/objects/{object_type}/search",
                    json=body,
                )
                if resp.status_code != 200:
                    stats["errors"].append({
                        "object_type": object_type,
                        "error": f"HubSpot API {resp.status_code}: {resp.text[:200]}",
                    })
                    break

                data = resp.json()
                results = data.get("results", [])
                stats["found"] += len(results)
                if not results:
                    break

                for record in results:
                    record_id = record.get("id", "")
                    properties = record.get("properties") or {}
                    modified = properties.get("hs_lastmodifieddate")
                    if modified and (newest_seen is None or modified > newest_seen):
                        newest_seen = modified

                    content = _hubspot_record_to_markdown(object_type, record_id, properties)
                    if len(content.strip()) < 20:
                        stats["skipped"] += 1
                        continue

                    title = _hubspot_record_title(object_type, properties)
                    owner_id = properties.get("hubspot_owner_id")
                    ingest_meta = {
                        **properties,
                        "connector": "hubspot",
                        "crm_object_type": object_type,
                        "crm_object_id": record_id,
                        "hubspot_owner_id": owner_id,
                        "folder_path": f"HubSpot/{object_type}",
                    }
                    try:
                        doc, _assessment = await ingest_file(
                            session=session,
                            tenant_id=tenant_id,
                            source_id=source_id,
                            file_data=content.encode("utf-8"),
                            filename=f"hubspot-{object_type}-{record_id}.md",
                            embedding_provider=provider,
                            source_object_id=f"hubspot:{object_type}:{record_id}",
                            source_url=(
                                f"https://app.hubspot.com/contacts/{object_type}/{record_id}"
                            ),
                            source_metadata=ingest_meta,
                            last_modified=_parse_datetime(modified),
                        )
                        await session.refresh(doc)

                        # Owner-based ACL grant: CRM records are sensitive by
                        # default (see default_visibility on the source);
                        # each record grants read access to its own owner.
                        # Always clear any PRIOR owner-based grant on this
                        # document first — the old delete only matched
                        # today's owner_id, so a record whose owner changed
                        # (or was unassigned) since the last sync kept the
                        # previous owner's access forever.
                        await session.execute(
                            text(
                                "DELETE FROM acl_entries "
                                "WHERE document_id = :did "
                                "AND source_acl_id LIKE 'hubspot_owner:%'"
                            ),
                            {"did": doc.id},
                        )
                        if owner_id:
                            await session.execute(
                                text(
                                    "INSERT INTO acl_entries "
                                    "(id, document_id, principal_type, principal_id, "
                                    " permission, source_acl_id) "
                                    "VALUES (:id, :did, 'user', :pid, 'read', :said)"
                                ),
                                {
                                    "id": uuid.uuid4(),
                                    "did": doc.id,
                                    "pid": f"hubspot:owner:{owner_id}",
                                    "said": f"hubspot_owner:{owner_id}",
                                },
                            )

                        owner_principal_id = (
                            f"hubspot:owner:{owner_id}" if owner_id else None
                        )
                        await session.execute(
                            text(
                                "INSERT INTO crm_objects "
                                "(id, tenant_id, source_id, document_id, object_type, "
                                " external_id, owner_principal_id, properties, updated_at) "
                                "VALUES (:id, :tid, :sid, :did, :otype, :extid, :owner, "
                                " CAST(:props AS jsonb), now()) "
                                "ON CONFLICT (tenant_id, source_id, object_type, external_id) "
                                "DO UPDATE SET document_id = :did, "
                                "  owner_principal_id = :owner, "
                                "  properties = CAST(:props AS jsonb), updated_at = now()"
                            ),
                            {
                                "id": uuid.uuid4(), "tid": tenant_id, "sid": source_id,
                                "did": doc.id, "otype": object_type, "extid": record_id,
                                "owner": owner_principal_id,
                                "props": json.dumps(properties),
                            },
                        )

                        stats["synced"] += 1
                        per_type_synced += 1
                        logger.info(
                            "Synced HubSpot %s: %s (%d chunks)",
                            object_type, title, doc.chunk_count,
                        )
                    except Exception as e:
                        stats["errors"].append({
                            "record": f"{object_type}:{record_id}",
                            "error": str(e)[:200],
                        })

                stats["by_object_type"][object_type] = per_type_synced
                after = (data.get("paging") or {}).get("next", {}).get("after")
                if not after or per_type_synced >= limit:
                    break

            if newest_seen:
                new_cursors[object_type] = newest_seen

    if stats["synced"] > 0 or new_cursors != cursors:
        await session.execute(
            text(
                "INSERT INTO sync_cursors "
                "(source_type, source_id, delta_token, "
                " last_sync_at, sync_status, items_synced) "
                "VALUES ('hubspot', :sid, :token, now(), 'completed', :count) "
                "ON CONFLICT (source_type, source_id) "
                "DO UPDATE SET delta_token = :token, "
                "  last_sync_at = now(), "
                "  sync_status = 'completed', "
                "  items_synced = :count"
            ),
            {
                "sid": source_id,
                "token": json.dumps(new_cursors),
                "count": stats["synced"],
            },
        )
    await session.commit()

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


def _normalize_timestamp(value: str | None) -> str | None:
    """Normalize an ISO-8601 timestamp for string comparison/storage.

    Notion emits trailing-Z timestamps (``...Z``); tokens previously stored
    by this codebase may use Python's ``+00:00`` offset form instead.
    Comparing/max()-ing the raw strings across these two formats is
    unreliable (e.g. "2026-01-01T00:00:00.000Z" vs
    "2026-01-01T00:00:00+00:00" don't compare as equal or consistently
    ordered as plain strings). Parse and re-render in a single canonical
    form (UTC, ``+00:00`` offset) before comparing or persisting.
    """
    dt = _parse_datetime(value)
    if dt is None:
        return None
    return dt.isoformat()


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


async def _sharepoint_lists(
    client: Any,
    headers: dict[str, str],
    site_id: str,
) -> list[dict[str, Any]]:
    """Discover SharePoint Lists for a site -- a completely separate
    Graph API surface (``/sites/{id}/lists``) from document libraries
    (``/drives``). Previously nothing in this connector ever called
    this endpoint, so structured internal data kept in Lists (trackers,
    indexes, directories -- a common pattern for exactly the kind of
    company knowledge a "primary source" needs to cover) was entirely
    undiscoverable, not just unparsed.

    Document libraries are themselves represented as list resources
    with ``list.template == "documentLibrary"`` -- excluded here since
    they're already covered via ``_sharepoint_drives``/the delta-sync
    path. Hidden system lists are excluded too.
    """
    lists: list[dict[str, Any]] = []
    url = f"{GRAPH_BASE}/sites/{site_id}/lists"
    while url:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            if item.get("hidden"):
                continue
            if (item.get("list") or {}).get("template") == "documentLibrary":
                continue
            lists.append(item)
        url = data.get("@odata.nextLink")
    return lists


def _sharepoint_list_field_value_to_text(value: Any) -> str:
    """Render a single SharePoint list column value as plain text.

    List item fields (``item["fields"]``) are a flat dict of column
    internal name -> value, but the value's shape varies by column
    type: scalars (str/int/float/bool) render directly; lookup/person
    columns commonly carry a dict with a "LookupValue"/"Title"/
    "DisplayName" key; multi-value columns (multi-lookup, multi-choice)
    carry a list, rendered as a comma-joined string.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(
            _sharepoint_list_field_value_to_text(v) for v in value if v is not None
        )
    if isinstance(value, dict):
        for key in ("LookupValue", "Title", "DisplayName", "Email"):
            if value.get(key):
                return str(value[key])
        return ""
    return str(value)


# Internal column names present on essentially every SharePoint list item
# that are metadata about the row itself, not user-authored content --
# excluded from the rendered body text (still fine to leave in raw
# doc_metadata, just not worth surfacing as prose).
_SHAREPOINT_LIST_SYSTEM_FIELDS = frozenset({
    "id", "ContentType", "Modified", "Created", "AuthorLookupId",
    "EditorLookupId", "_UIVersionString", "Attachments", "Edit",
    "LinkTitleNoMenu", "LinkTitle", "LinkTitle2", "ItemChildCount",
    "FolderChildCount", "_ComplianceFlags", "_ComplianceTag",
    "_ComplianceTagWrittenTime", "_ComplianceTagUserId", "AppAuthorLookupId",
    "AppEditorLookupId",
})


def _sharepoint_list_item_title(fields: dict[str, Any], item_id: str) -> str:
    for key in ("Title", "LinkTitle", "Name"):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return f"Item {item_id}"


def _sharepoint_list_item_to_markdown(
    list_name: str, fields: dict[str, Any], item_id: str,
) -> tuple[str, str]:
    """Render one list item's fields as (title, markdown body)."""
    title = _sharepoint_list_item_title(fields, item_id)
    lines = [f"# {title}", "", f"List: {list_name}"]
    for key, value in fields.items():
        if key in _SHAREPOINT_LIST_SYSTEM_FIELDS or key == "Title":
            continue
        text_val = _sharepoint_list_field_value_to_text(value)
        if text_val:
            lines.append(f"{key}: {text_val}")
    return title, "\n".join(lines)


async def _sync_sharepoint_list_items(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    client: Any,
    headers: dict[str, str],
    site_id: str,
    list_obj: dict[str, Any],
    limit: int,
    stats: dict[str, Any],
) -> tuple[set[str], bool]:
    """Sync items in one SharePoint List up to ``limit`` new ingests,
    returning (source_object_ids seen so far, whether this list was
    FULLY listed).

    A full re-listing each call, not a delta sync -- this is a new,
    opt-in (``sync_lists``) feature, and ingest_file's content-hash
    dedup already makes re-processing an unchanged item a cheap no-op.
    The completeness flag matters for the caller's deletion detection:
    a listing truncated early by ``limit`` says nothing about items
    beyond what was fetched, so it must not be treated as authoritative
    for "anything absent was deleted".
    """
    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    list_id = str(list_obj["id"])
    list_name = list_obj.get("displayName") or list_obj.get("name") or list_id
    provider = get_embedding_provider()
    active_object_ids: set[str] = set()
    complete = True

    url = f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/items"
    params: dict[str, Any] | None = {"$expand": "fields", "$top": 100}
    while url:
        if stats["synced"] >= limit:
            complete = False
            break

        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        params = None  # only needed on the first request; nextLink carries it

        for item in data.get("value", []):
            if stats["synced"] >= limit:
                complete = False
                break

            item_id = str(item.get("id", ""))
            if not item_id:
                continue
            fields = item.get("fields") or {}
            source_object_id = f"sharepoint:list:{list_id}:{item_id}"
            active_object_ids.add(source_object_id)
            stats["found"] += 1

            title, body = _sharepoint_list_item_to_markdown(list_name, fields, item_id)
            if len(body.strip()) < 20:
                stats["skipped"] += 1
                continue

            try:
                doc, _assessment = await ingest_file(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    file_data=body.encode("utf-8"),
                    filename=f"sharepoint-list-{list_id}-{item_id}",
                    embedding_provider=provider,
                    source_object_id=source_object_id,
                    source_url=item.get("webUrl"),
                    source_metadata={
                        "connector": "sharepoint",
                        "content_type": "list_item",
                        "list_id": list_id,
                        "list_name": list_name,
                        "folder_path": f"Lists/{list_name}",
                    },
                    last_modified=_parse_datetime(
                        (item.get("lastModifiedDateTime") or fields.get("Modified")),
                    ),
                )
                await session.refresh(doc)
                stats["synced"] += 1
                logger.info(
                    "Synced SharePoint list item: %s / %s (%d chunks)",
                    list_name, title, doc.chunk_count,
                )
            except Exception as e:
                stats["errors"].append(
                    {"list_item": f"{list_name}/{title}", "error": str(e)[:200]},
                )

        if stats["synced"] >= limit:
            break
        url = data.get("@odata.nextLink")

    return active_object_ids, complete


async def _mark_sharepoint_list_items_deleted(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    active_object_ids: set[str],
) -> int:
    """Soft-delete list-item documents no longer present in a full
    re-listing of all of this source's Lists -- mirrors
    _mark_notion_pages_deleted's same-shaped problem (no delta/removal
    signal from a full-refresh style sync) and its same empty-set guard
    against ever mistaking "found nothing" for "delete everything"."""
    if not active_object_ids:
        return 0

    result = await session.execute(
        text(
            "UPDATE documents SET status = 'deleted', "
            "review_status = 'rejected', last_synced_at = now() "
            "WHERE tenant_id = :tid AND source_id = :sid "
            "AND source_object_id LIKE 'sharepoint:list:%' "
            "AND status != 'deleted' "
            "AND NOT (source_object_id = ANY(:active)) "
            "RETURNING id"
        ),
        {"tid": tenant_id, "sid": source_id, "active": list(active_object_ids)},
    )
    deleted_doc_ids = [row.id for row in result.fetchall()]
    await _cascade_delete_document_data(session, deleted_doc_ids)
    await session.commit()
    return len(deleted_doc_ids)


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


def _rotate_drives(
    drives: list[dict[str, Any]], last_first_id: str | None,
) -> list[dict[str, Any]]:
    """Rotate ``drives`` so a different drive gets first-in-line budget
    priority each sync call.

    Without this, iterating drives in the same fixed (API-return) order
    every call means a single busy drive that alone consumes the whole
    ``limit`` permanently starves every other drive of any budget at
    all — not just once, but forever, since the order never changes.
    Rotating guarantees every drive gets to go first at least once every
    ``len(drives)`` calls.
    """
    if not drives or not last_first_id:
        return drives
    ids = [str(d["id"]) for d in drives]
    if last_first_id not in ids:
        return drives
    start = (ids.index(last_first_id) + 1) % len(drives)
    return drives[start:] + drives[:start]


async def _persist_sharepoint_cursor_map(
    session: AsyncSession,
    source_id: uuid.UUID,
    cursor_map: dict[str, str],
    items_synced: int,
    sync_status: str,
) -> None:
    """Upsert the full per-drive cursor map (plus the ``__last_first_drive__``
    rotation marker it may contain) into ``sync_cursors``.

    Called progressively — once per drive as soon as that drive's delta
    walk completes, not batched until the whole sync call finishes — so a
    drive's advanced cursor is never lost just because a LATER drive in
    the same call hit the sync limit.
    """
    await session.execute(
        text(
            "INSERT INTO sync_cursors "
            "(source_type, source_id, delta_token, last_sync_at, "
            " sync_status, items_synced) "
            "VALUES ('sharepoint', :sid, :token, now(), :status, :count) "
            "ON CONFLICT (source_type, source_id) "
            "DO UPDATE SET delta_token = :token, "
            "  last_sync_at = now(), "
            "  sync_status = :status, "
            "  items_synced = :count"
        ),
        {
            "sid": source_id,
            "token": json.dumps(cursor_map),
            "status": sync_status,
            "count": items_synced,
        },
    )
    await session.commit()


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
        # Check the limit BETWEEN pages, never mid-page: Graph's delta feed
        # only supports resuming from a page boundary (@odata.nextLink /
        # @odata.deltaLink) -- there is no way to ask it to resume "partway
        # through the page we were just processing". Stopping mid-page and
        # returning some hand-rolled position (the previous bug: returning
        # the original `cursor_url` argument unchanged) is not a valid
        # resume point, so the next call always restarted from the exact
        # same place -- permanently re-processing the same items and never
        # advancing, for any drive whose backlog exceeds one call's limit.
        # Checking here means a call may process a bit more than `limit`
        # items if the current page is large, but every returned cursor is
        # a real Graph-provided pointer that guarantees actual progress.
        if stats["synced"] >= limit:
            stats["delta_complete"] = False
            return url

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
            "AND source_object_id = :soid AND status != 'deleted' "
            "RETURNING id"
        ),
        {
            "tid": tenant_id,
            "sid": source_id,
            "soid": _sharepoint_source_object_id(drive_id, item_id),
        },
    )
    deleted_doc_ids = [row.id for row in result.fetchall()]
    await _cascade_delete_document_data(session, deleted_doc_ids)
    await session.commit()
    return len(deleted_doc_ids)


def _record_sharepoint_skip_reason(stats: dict[str, Any], reason: str) -> None:
    """Track WHY an item was skipped, not just that it was.

    Previously every skip reason (unsupported extension, OneNote/package
    items, oversized files, non-file items) collapsed into a single
    opaque `skipped` counter -- an admin watching sync stats had no way
    to tell "12 modern .aspx pages aren't covered" apart from "12 video
    files aren't covered" apart from "12 items failed for some other
    reason". `skip_reasons` breaks this down so the gap is actually
    visible instead of silently invisible.

    Known, deliberately out-of-scope content types surfaced this way:
    - "aspx_modern_page": modern SharePoint communication-site pages
      (news posts, wiki-style pages) -- their real content lives in the
      Graph Pages API's canvas/web-part model, a materially different
      integration from downloading a file's raw bytes.
    - "onenote_or_package_item": OneNote notebooks -- Graph exposes
      these via a completely separate OneNote API
      (/sites/{id}/onenote), not the drives API this connector uses.
    Both are real, known coverage gaps for a company that keeps
    knowledge in pages or notebooks rather than files -- documented here
    (and in DEPLOYMENT.md) as deliberately deferred rather than silently
    dropped, since properly supporting either is a comparably-sized
    integration to what SharePoint Lists support required, not a small
    addition to the existing file-download path.
    """
    reasons = stats.setdefault("skip_reasons", {})
    reasons[reason] = reasons.get(reason, 0) + 1


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
    if "folder" in item:
        # Structural, not content -- no signal needed, unlike the cases
        # below where something was actually skipped over.
        return
    if "package" in item:
        # OneNote notebooks (and other Graph "package" driveItems) have
        # no supported content-extraction path -- there is a completely
        # separate Graph API for OneNote (/sites/{id}/onenote) that this
        # connector doesn't call. Previously this returned silently,
        # without even incrementing stats["skipped"] -- indistinguishable
        # from a page that was never discovered at all. Counted with a
        # distinguishable reason now so an admin can actually see this
        # content exists and isn't covered, instead of it looking like it
        # was never there.
        stats["skipped"] += 1
        _record_sharepoint_skip_reason(stats, "onenote_or_package_item")
        return
    if "file" not in item:
        stats["skipped"] += 1
        _record_sharepoint_skip_reason(stats, "not_a_file")
        return

    name = item.get("name", "")
    item_id = item.get("id", "")
    drive_id = str(drive["id"])
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in SUPPORTED_SYNC_EXTENSIONS:
        stats["skipped"] += 1
        _record_sharepoint_skip_reason(
            stats,
            "aspx_modern_page" if ext == "aspx" else f"unsupported_extension:{ext or 'none'}",
        )
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
            _record_sharepoint_skip_reason(stats, "file_too_large")
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

    Also collects every other captured property type (select, people,
    date, url, rich_text, number, checkbox, email, phone_number, and
    simple scalar formulas) into ``meta["custom_properties"]`` as
    "Label: value" pairs, in ADDITION to the existing flattened
    per-type keys (prop_name.lower(), property_*, date_*, etc.) kept
    unchanged for structured-filter compatibility
    (doc_metadata ->> 'key' = 'val'). Previously only status/tags/
    author/last_edited_by/last_edited_time/parent_path ever reached the
    ingested file's searchable text (see the meta_header building code
    in _sync_notion) -- a database row's custom "Priority" select,
    "Assignee" people property, or "Due Date" would be captured here but
    never surface in semantic/hybrid search or RAG answers, only via
    exact-match structured filtering. relation and rollup properties are
    deliberately NOT captured here: relation values are bare page UUIDs
    with no human-readable text without an extra API call per
    reference, and rollup requires recursively unwrapping a nested,
    possibly-array-typed value -- both a meaningfully bigger lift than
    the scalar property types below for comparatively little value.
    """
    meta: dict[str, Any] = {}
    custom_properties: dict[str, str] = {}

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
                custom_properties[prop_name] = select_obj["name"]

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
                custom_properties[prop_name] = ", ".join(names)

        elif ptype == "date":
            date_obj = prop.get("date")
            if date_obj and date_obj.get("start"):
                meta[f"date_{prop_name.lower()}"] = date_obj["start"]
                custom_properties[prop_name] = date_obj["start"]

        elif ptype == "url":
            url_val = prop.get("url")
            if url_val:
                meta[f"url_{prop_name.lower()}"] = url_val
                custom_properties[prop_name] = url_val

        elif ptype == "rich_text":
            texts = prop.get("rich_text", [])
            text_val = "".join(t.get("plain_text", "") for t in texts)
            if text_val and len(text_val) < 200:
                meta[prop_name.lower()] = text_val
                custom_properties[prop_name] = text_val

        elif ptype == "number":
            number_val = prop.get("number")
            if number_val is not None:
                meta[f"number_{prop_name.lower()}"] = number_val
                custom_properties[prop_name] = str(number_val)

        elif ptype == "checkbox":
            checkbox_val = bool(prop.get("checkbox"))
            meta[f"checkbox_{prop_name.lower()}"] = checkbox_val
            custom_properties[prop_name] = "Yes" if checkbox_val else "No"

        elif ptype == "email":
            email_val = prop.get("email")
            if email_val:
                meta[f"email_{prop_name.lower()}"] = email_val
                custom_properties[prop_name] = email_val

        elif ptype == "phone_number":
            phone_val = prop.get("phone_number")
            if phone_val:
                meta[f"phone_{prop_name.lower()}"] = phone_val
                custom_properties[prop_name] = phone_val

        elif ptype == "formula":
            formula_obj = prop.get("formula") or {}
            formula_type = formula_obj.get("type", "")
            formula_val = formula_obj.get(formula_type) if formula_type else None
            if formula_val is not None:
                meta[f"formula_{prop_name.lower()}"] = formula_val
                custom_properties[prop_name] = str(formula_val)

    if tags:
        meta["tags"] = tags
    if custom_properties:
        meta["custom_properties"] = custom_properties

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
        resp = await _notion_request_with_retry(
            client.get,
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


# Block types whose content lives under "caption"/"external"/"file"/"url"
# rather than "rich_text" -- see _notion_block_to_text.
_NOTION_ATTACHMENT_BLOCK_TYPES: dict[str, str] = {
    "image": "Image",
    "file": "File",
    "pdf": "PDF",
    "video": "Video",
    "audio": "Audio",
    "bookmark": "Bookmark",
    "embed": "Embed",
    "link_preview": "Link",
}


def _notion_block_to_text(block: dict[str, Any]) -> str:
    bt = block.get("type", "")
    content = block.get(bt, {})

    # table_row's cell content lives under "cells" (a list of lists of
    # rich-text objects, one inner list per column), not "rich_text" like
    # every other block type -- handled separately before the generic
    # rich_text extraction below, which would otherwise always see an
    # empty list and silently drop every row's data. The parent "table"
    # block itself carries no cell data (just table_width/has_*_header
    # config) -- its rows arrive as children blocks via the recursive
    # fetch in _fetch_notion_blocks_text, so it needs no special case.
    if bt == "table_row":
        cells = content.get("cells", [])
        cell_texts = [
            "".join(rt.get("plain_text", "") for rt in cell) for cell in cells
        ]
        if not any(cell_texts):
            return ""
        return "| " + " | ".join(cell_texts) + " |"

    # Attachment-like blocks carry no "rich_text" key at all -- their
    # description lives under "caption" (a rich-text array, same shape
    # as everywhere else) and their target under "external.url" or
    # "file.url" (internal upload; expiring, so kept only as a
    # point-in-time reference) or a bare "url" for bookmark/embed/
    # link_preview. Before this, these blocks contributed nothing --
    # not even a filename, caption, or URL -- so an attachment's
    # existence was completely invisible to RAG.
    if bt in _NOTION_ATTACHMENT_BLOCK_TYPES:
        caption_rt = content.get("caption", [])
        caption = "".join(rt.get("plain_text", "") for rt in caption_rt)
        url = (
            (content.get("external") or {}).get("url")
            or (content.get("file") or {}).get("url")
            or content.get("url")
        )
        if not caption and not url:
            return ""
        label = _NOTION_ATTACHMENT_BLOCK_TYPES[bt]
        parts = [f"[{label}]"]
        if caption:
            parts.append(caption)
        if url:
            parts.append(f"({url})")
        return " ".join(parts)

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
