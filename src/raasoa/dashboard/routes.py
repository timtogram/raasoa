"""HTMX + Jinja2 Dashboard routes for quality governance.

Protected by password when DASHBOARD_PASSWORD is set.
"""

import json as _json
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session

templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"

_valid_sessions: set[str] = set()


def _check_auth(request: Request) -> RedirectResponse | None:
    """Return redirect if dashboard auth required, None if ok."""
    if not settings.dashboard_password:
        return None
    token = request.cookies.get("raasoa_session", "")
    if token in _valid_sessions:
        return None
    return RedirectResponse("/dashboard/login", status_code=302)


# ── Auth ────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if not settings.dashboard_password:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"active": "login", "error": None},
    )


@router.post("/login", response_model=None)
async def login_post(
    request: Request, password: str = Form(...),
) -> Response:
    if password == settings.dashboard_password:
        token = secrets.token_urlsafe(32)
        _valid_sessions.add(token)
        resp = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie(
            "raasoa_session", token,
            httponly=True, samesite="lax", max_age=86400,
        )
        return resp
    return templates.TemplateResponse(
        request, "login.html",
        {"active": "login", "error": "Invalid password."},
    )


# ── Pages ───────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT

    stats_result = await session.execute(text(
        "SELECT "
        "(SELECT COUNT(*) FROM documents WHERE tenant_id = :tid) as total_documents, "
        "(SELECT COUNT(*) FROM documents WHERE tenant_id = :tid AND status = 'indexed') as indexed_documents, "
        "(SELECT COALESCE(AVG(quality_score), 0) FROM documents WHERE tenant_id = :tid AND quality_score IS NOT NULL) as avg_quality, "
        "(SELECT COUNT(*) FROM conflict_candidates WHERE tenant_id = :tid AND status = 'new') as open_conflicts, "
        "(SELECT COUNT(*) FROM review_tasks WHERE tenant_id = :tid AND status = 'new') as pending_reviews"
    ), {"tid": tid})
    stats = stats_result.first()

    docs_result = await session.execute(text(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM claims c WHERE c.document_id = d.id) as claim_count "
        "FROM documents d WHERE d.tenant_id = :tid "
        "ORDER BY d.created_at DESC LIMIT 10"
    ), {"tid": tid})

    return templates.TemplateResponse(
        request, "dashboard.html",
        {"active": "home", "stats": stats, "recent_docs": docs_result.fetchall()},
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request) -> Response:
    if redir := _check_auth(request):
        return redir
    return templates.TemplateResponse(
        request, "search.html",
        {"active": "search", "tenant_id": DEFAULT_TENANT},
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> Response:
    if redir := _check_auth(request):
        return redir
    return templates.TemplateResponse(
        request, "upload.html", {"active": "upload"},
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT

    result = await session.execute(text(
        "SELECT s.id, s.name, s.source_type, s.connection_config, "
        "(SELECT COUNT(*) FROM documents d WHERE d.source_id = s.id "
        " AND d.status != 'deleted') as doc_count "
        "FROM sources s WHERE s.tenant_id = :tid ORDER BY s.name"
    ), {"tid": tid})

    webhook_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request, "sources.html",
        {"active": "sources", "sources": result.fetchall(),
         "webhook_url": webhook_url, "tenant_id": tid},
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT
    result = await session.execute(text(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM claims c WHERE c.document_id = d.id) as claim_count "
        "FROM documents d WHERE d.tenant_id = :tid "
        "ORDER BY d.created_at DESC LIMIT 100"
    ), {"tid": tid})
    return templates.TemplateResponse(
        request, "documents.html",
        {"active": "documents", "documents": result.fetchall()},
    )


@router.get("/documents/{document_id}", response_class=HTMLResponse)
async def document_detail(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir

    # Tenant-scoped document fetch
    tid = DEFAULT_TENANT
    doc_result = await session.execute(
        text("SELECT * FROM documents WHERE id = :did AND tenant_id = :tid"),
        {"did": document_id, "tid": tid},
    )
    doc = doc_result.first()
    if not doc:
        return HTMLResponse("<h1>Document not found</h1>", status_code=404)

    findings_result = await session.execute(
        text("SELECT * FROM quality_findings WHERE document_id = :did ORDER BY created_at"),
        {"did": document_id},
    )
    claims_result = await session.execute(
        text("SELECT * FROM claims WHERE document_id = :did ORDER BY created_at"),
        {"did": document_id},
    )
    chunks_result = await session.execute(
        text(
            "SELECT id, chunk_index, chunk_text, section_title, "
            "chunk_type, token_count FROM chunks "
            "WHERE document_id = :did ORDER BY chunk_index"
        ),
        {"did": document_id},
    )
    return templates.TemplateResponse(
        request, "document_detail.html",
        {"active": "documents", "doc": doc,
         "findings": findings_result.fetchall(),
         "claims": claims_result.fetchall(),
         "chunks": chunks_result.fetchall()},
    )


@router.get("/conflicts", response_class=HTMLResponse)
async def conflicts_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT
    result = await session.execute(text(
        "SELECT * FROM conflict_candidates "
        "WHERE tenant_id = :tid "
        "ORDER BY CASE WHEN status = 'new' THEN 0 ELSE 1 END, "
        "confidence DESC NULLS LAST, created_at DESC "
        "LIMIT 100"
    ), {"tid": tid})
    return templates.TemplateResponse(
        request, "conflicts.html",
        {"active": "conflicts", "conflicts": result.fetchall()},
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT

    # Quality by source
    qbs_result = await session.execute(text(
        "SELECT s.name as source_name, s.source_type, "
        "COUNT(d.id) as document_count, "
        "ROUND(AVG(d.quality_score)::numeric, 3) as avg_quality, "
        "ROUND(MIN(d.quality_score)::numeric, 3) as min_quality, "
        "COUNT(*) FILTER (WHERE d.quality_score < 0.5) as low_quality_count, "
        "COUNT(*) FILTER (WHERE d.review_status = 'quarantined') as quarantined_count "
        "FROM documents d "
        "JOIN sources s ON d.source_id = s.id "
        "WHERE d.tenant_id = :tid AND d.status != 'deleted' "
        "GROUP BY s.id, s.name, s.source_type "
        "ORDER BY avg_quality ASC NULLS LAST"
    ), {"tid": tid})

    # Contradiction hotspots
    hotspots_result = await session.execute(text(
        "SELECT d.title as document_title, d.id as document_id, "
        "s.name as source_name, "
        "COUNT(cc.id) as conflict_count, "
        "COUNT(*) FILTER (WHERE cc.status = 'new') as unresolved_count, "
        "ROUND(AVG(cc.confidence)::numeric, 3) as avg_confidence "
        "FROM conflict_candidates cc "
        "JOIN documents d ON (cc.document_a_id = d.id OR cc.document_b_id = d.id) "
        "JOIN sources s ON d.source_id = s.id "
        "WHERE cc.tenant_id = :tid "
        "GROUP BY d.id, d.title, s.name "
        "ORDER BY unresolved_count DESC, conflict_count DESC LIMIT 20"
    ), {"tid": tid})

    # Claim stability
    stab_result = await session.execute(text(
        "SELECT "
        "COUNT(*) as total_claims, "
        "COUNT(*) FILTER (WHERE c.status = 'active') as active_claims, "
        "COUNT(*) FILTER (WHERE c.status = 'superseded') as superseded_claims, "
        "COUNT(*) FILTER (WHERE c.status = 'rejected') as rejected_claims, "
        "COUNT(DISTINCT c.document_id) as documents_with_claims "
        "FROM claims c "
        "JOIN documents d ON c.document_id = d.id "
        "WHERE d.tenant_id = :tid"
    ), {"tid": tid})
    stab_row = stab_result.first()
    total = stab_row.total_claims if stab_row else 0
    superseded = stab_row.superseded_claims if stab_row else 0
    stability = {
        "total_claims": total,
        "active_claims": stab_row.active_claims if stab_row else 0,
        "superseded_claims": superseded,
        "rejected_claims": stab_row.rejected_claims if stab_row else 0,
        "stability_rate": round(1.0 - (superseded / total) if total > 0 else 1.0, 3),
    }

    qbs_rows = qbs_result.fetchall()
    quality_by_source = [
        {
            "source_name": r.source_name, "source_type": r.source_type,
            "document_count": r.document_count,
            "avg_quality": float(r.avg_quality) if r.avg_quality else None,
            "min_quality": float(r.min_quality) if r.min_quality else None,
            "low_quality_count": r.low_quality_count,
            "quarantined_count": r.quarantined_count,
        }
        for r in qbs_rows
    ]
    hotspots = [
        {
            "document_title": r.document_title, "document_id": str(r.document_id),
            "source_name": r.source_name, "conflict_count": r.conflict_count,
            "unresolved_count": r.unresolved_count,
            "avg_confidence": float(r.avg_confidence) if r.avg_confidence else None,
        }
        for r in hotspots_result.fetchall()
    ]

    return templates.TemplateResponse(
        request, "analytics.html",
        {"active": "analytics", "quality_by_source": quality_by_source,
         "hotspots": hotspots, "stability": stability},
    )


@router.get("/reviews", response_class=HTMLResponse)
async def reviews_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir
    tid = DEFAULT_TENANT
    result = await session.execute(text(
        "SELECT r.*, d.title as doc_title "
        "FROM review_tasks r "
        "LEFT JOIN documents d ON r.document_id = d.id "
        "WHERE r.tenant_id = :tid "
        "ORDER BY CASE WHEN r.status = 'new' THEN 0 ELSE 1 END, "
        "r.created_at DESC LIMIT 100"
    ), {"tid": tid})
    return templates.TemplateResponse(
        request, "reviews.html",
        {"active": "reviews", "reviews": result.fetchall()},
    )


# ── Dashboard API (cookie-authed proxies) ──────────────


@router.post("/api/ingest")
async def dashboard_ingest_proxy(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Proxy file upload from dashboard (cookie auth)."""
    if _check_auth(request):
        return JSONResponse(
            status_code=401, content={"detail": "Not authenticated"},
        )

    import uuid as _uuid

    from raasoa.api.ingestion import _ensure_default_tenant_and_source
    from raasoa.ingestion.pipeline import ingest_file
    from raasoa.providers.factory import get_embedding_provider

    form = await request.form()
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        return JSONResponse(
            status_code=400, content={"detail": "No file provided"},
        )

    file_data = await upload.read()
    filename = getattr(upload, "filename", "upload.txt") or "upload.txt"

    if not file_data:
        return JSONResponse(
            status_code=400, content={"detail": "Empty file"},
        )

    tid = _uuid.UUID(DEFAULT_TENANT)
    tid, source_id = await _ensure_default_tenant_and_source(session, tid)
    provider = get_embedding_provider()

    try:
        doc, assessment = await ingest_file(
            session=session, tenant_id=tid, source_id=source_id,
            file_data=file_data, filename=filename,
            embedding_provider=provider,
        )
        await session.refresh(doc)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Dashboard ingest failed")
        return JSONResponse(
            status_code=500,
            content={"detail": "Ingestion failed"},
        )

    findings = []
    if assessment:
        findings = [
            {"finding_type": f.finding_type, "severity": f.severity, "details": f.details}
            for f in assessment.findings
        ]

    return JSONResponse(content={
        "document_id": str(doc.id),
        "title": doc.title,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "quality_score": doc.quality_score,
        "review_status": doc.review_status,
        "conflict_status": doc.conflict_status,
        "quality_findings": findings,
    })


@router.post("/api/search")
async def dashboard_search_proxy(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Proxy search from dashboard (uses cookie auth, not API key).

    The dashboard JS calls this instead of /v1/retrieve directly,
    so it works with the dashboard's session cookie.
    """
    if _check_auth(request):
        return JSONResponse(
            status_code=401, content={"detail": "Not authenticated"},
        )

    import uuid as _uuid

    from raasoa.providers.factory import get_embedding_provider
    from raasoa.retrieval.confidence import compute_confidence
    from raasoa.retrieval.factory import get_reranker
    from raasoa.retrieval.hybrid_search import search
    from raasoa.retrieval.query_router import QueryType, route_query
    from raasoa.retrieval.structured import structured_query

    body = await request.json()
    query = body.get("query", "")
    top_k = body.get("top_k", 5)
    tid = _uuid.UUID(body.get("tenant_id", DEFAULT_TENANT))

    routing = route_query(query)

    result_data: dict[str, Any] = {
        "query": query,
        "routed_to": routing.query_type.value,
        "routing_reason": routing.reason,
        "results": [],
        "structured": None,
        "confidence": {
            "retrieval_confidence": 0.0,
            "source_count": 0,
            "top_score": 0.0,
            "answerable": False,
        },
    }

    if routing.query_type == QueryType.STRUCTURED:
        try:
            sq = await structured_query(session, query, tid)
            result_data["structured"] = {
                "answer": sq.answer,
                "data": sq.data,
                "query_type": sq.query_type,
            }
        except Exception:
            routing = routing.__class__(
                query_type=QueryType.RAG, confidence=0.5,
                reason="structured_fallback",
            )
            result_data["routed_to"] = "rag"
            result_data["routing_reason"] = "structured_fallback"

    if routing.query_type == QueryType.RAG:
        provider = get_embedding_provider()
        reranker = get_reranker()
        search_results = await search(
            session=session, query=query, tenant_id=tid,
            embedding_provider=provider, top_k=top_k * 3,
        )
        search_results = await reranker.rerank(query, search_results, top_k)
        confidence = compute_confidence(search_results)

        result_data["results"] = [
            {
                "chunk_id": str(r.chunk_id),
                "document_id": str(r.document_id),
                "text": r.chunk_text,
                "section_title": r.section_title,
                "chunk_type": r.chunk_type,
                "score": r.score,
                "semantic_rank": r.semantic_rank,
                "lexical_rank": r.lexical_rank,
            }
            for r in search_results
        ]
        result_data["confidence"] = {
            "retrieval_confidence": confidence.retrieval_confidence,
            "source_count": confidence.source_count,
            "top_score": confidence.top_score,
            "answerable": confidence.answerable,
        }

    return JSONResponse(content=result_data)


# ── HTMX Mutations ─────────────────────────────────────


@router.post("/api/conflicts/{conflict_id}/resolve", response_class=HTMLResponse)
async def resolve_conflict_htmx(
    request: Request,
    conflict_id: uuid.UUID,
    resolution: str = Form(...),
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir

    tid = DEFAULT_TENANT
    result = await session.execute(
        text(
            "SELECT id, document_a_id, document_b_id "
            "FROM conflict_candidates "
            "WHERE id = :cid AND tenant_id = :tid"
        ),
        {"cid": conflict_id, "tid": tid},
    )
    conflict = result.first()
    if not conflict:
        return HTMLResponse("<div class='text-red-600'>Not found</div>")

    superseded_doc_id = None
    if resolution == "keep_a":
        superseded_doc_id = conflict.document_b_id
    elif resolution == "keep_b":
        superseded_doc_id = conflict.document_a_id

    if superseded_doc_id:
        await session.execute(
            text(
                "UPDATE documents SET review_status = 'superseded' "
                "WHERE id = :did AND tenant_id = :tid"
            ),
            {"did": superseded_doc_id, "tid": tid},
        )
        await session.execute(
            text("UPDATE claims SET status = 'superseded' WHERE document_id = :did"),
            {"did": superseded_doc_id},
        )

    resolution_data = _json.dumps({"resolution": resolution, "comment": comment})
    await session.execute(
        text(
            "UPDATE conflict_candidates SET status = 'resolved', "
            "details = COALESCE(details, CAST('{}' AS jsonb)) "
            "|| CAST(:res AS jsonb) WHERE id = :cid"
        ),
        {"cid": conflict_id, "res": resolution_data},
    )
    await session.execute(
        text(
            "UPDATE review_tasks SET status = 'approved', completed_at = now() "
            "WHERE conflict_id = :cid AND status = 'new'"
        ),
        {"cid": conflict_id},
    )
    await session.commit()

    return HTMLResponse(
        f'<div id="conflict-{conflict_id}" class="bg-green-50 rounded-lg border '
        f'border-green-200 p-4 text-green-800 font-medium">'
        f'Resolved: {resolution}</div>'
    )


@router.post("/api/reviews/{review_id}/approve", response_class=HTMLResponse)
async def approve_review_htmx(
    request: Request,
    review_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir

    tid = DEFAULT_TENANT
    result = await session.execute(
        text(
            "SELECT id, document_id FROM review_tasks "
            "WHERE id = :rid AND tenant_id = :tid"
        ),
        {"rid": review_id, "tid": tid},
    )
    review = result.first()
    if not review:
        return HTMLResponse("<div class='text-red-600'>Not found</div>")

    await session.execute(
        text("UPDATE review_tasks SET status = 'approved', completed_at = now() WHERE id = :rid"),
        {"rid": review_id},
    )
    if review.document_id:
        await session.execute(
            text("UPDATE documents SET review_status = 'published' WHERE id = :did AND tenant_id = :tid"),
            {"did": review.document_id, "tid": tid},
        )
    await session.commit()
    return HTMLResponse(
        f'<div id="review-{review_id}" class="bg-green-50 rounded border '
        f'border-green-200 p-3 text-green-800 text-sm">Approved</div>'
    )


@router.post("/api/reviews/{review_id}/reject", response_class=HTMLResponse)
async def reject_review_htmx(
    request: Request,
    review_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if redir := _check_auth(request):
        return redir

    tid = DEFAULT_TENANT
    result = await session.execute(
        text(
            "SELECT id, document_id FROM review_tasks "
            "WHERE id = :rid AND tenant_id = :tid"
        ),
        {"rid": review_id, "tid": tid},
    )
    review = result.first()
    if not review:
        return HTMLResponse("<div class='text-red-600'>Not found</div>")

    await session.execute(
        text("UPDATE review_tasks SET status = 'rejected', completed_at = now() WHERE id = :rid"),
        {"rid": review_id},
    )
    if review.document_id:
        await session.execute(
            text("UPDATE documents SET review_status = 'rejected' WHERE id = :did AND tenant_id = :tid"),
            {"did": review.document_id, "tid": tid},
        )
    await session.commit()
    return HTMLResponse(
        f'<div id="review-{review_id}" class="bg-red-50 rounded border '
        f'border-red-200 p-3 text-red-800 text-sm">Rejected</div>'
    )
