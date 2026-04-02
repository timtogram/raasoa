"""HTMX + Jinja2 Dashboard routes for quality governance."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session

templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


@router.get("", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tid = DEFAULT_TENANT

    # Gather stats
    stats_result = await session.execute(text(
        "SELECT "
        "(SELECT COUNT(*) FROM documents WHERE tenant_id = :tid) as total_documents, "
        "(SELECT COUNT(*) FROM documents WHERE tenant_id = :tid AND status = 'indexed') as indexed_documents, "
        "(SELECT COALESCE(AVG(quality_score), 0) FROM documents WHERE tenant_id = :tid AND quality_score IS NOT NULL) as avg_quality, "
        "(SELECT COUNT(*) FROM conflict_candidates WHERE tenant_id = :tid AND status = 'new') as open_conflicts, "
        "(SELECT COUNT(*) FROM review_tasks WHERE tenant_id = :tid AND status = 'new') as pending_reviews"
    ), {"tid": tid})
    stats = stats_result.first()

    # Recent documents with claim counts
    docs_result = await session.execute(text(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM claims c WHERE c.document_id = d.id) as claim_count "
        "FROM documents d WHERE d.tenant_id = :tid "
        "ORDER BY d.created_at DESC LIMIT 10"
    ), {"tid": tid})
    recent_docs = docs_result.fetchall()

    return templates.TemplateResponse(
        request, "dashboard.html",
        {"active": "home", "stats": stats, "recent_docs": recent_docs},
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request) -> HTMLResponse:
    """Search playground for testing retrieval."""
    return templates.TemplateResponse(
        request, "search.html",
        {"active": "search", "tenant_id": DEFAULT_TENANT},
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> HTMLResponse:
    """File upload page with drag & drop."""
    return templates.TemplateResponse(
        request, "upload.html", {"active": "upload"},
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Data sources and connector management page."""
    tid = DEFAULT_TENANT

    # Get active sources with document counts
    result = await session.execute(text(
        "SELECT s.id, s.name, s.source_type, s.connection_config, "
        "(SELECT COUNT(*) FROM documents d WHERE d.source_id = s.id "
        " AND d.status != 'deleted') as doc_count "
        "FROM sources s WHERE s.tenant_id = :tid ORDER BY s.name"
    ), {"tid": tid})
    sources = result.fetchall()

    # Determine webhook URL from request
    webhook_url = str(request.base_url).rstrip("/")

    return templates.TemplateResponse(
        request, "sources.html",
        {
            "active": "sources",
            "sources": sources,
            "webhook_url": webhook_url,
            "tenant_id": tid,
        },
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
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
) -> HTMLResponse:
    doc_result = await session.execute(
        text("SELECT * FROM documents WHERE id = :did"), {"did": document_id}
    )
    doc = doc_result.first()

    findings_result = await session.execute(
        text(
            "SELECT * FROM quality_findings WHERE document_id = :did "
            "ORDER BY created_at"
        ),
        {"did": document_id},
    )

    claims_result = await session.execute(
        text(
            "SELECT * FROM claims WHERE document_id = :did "
            "ORDER BY created_at"
        ),
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
        {"active": "documents", "doc": doc, "findings": findings_result.fetchall(),
         "claims": claims_result.fetchall(), "chunks": chunks_result.fetchall()},
    )


@router.get("/conflicts", response_class=HTMLResponse)
async def conflicts_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
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


@router.post("/api/conflicts/{conflict_id}/resolve", response_class=HTMLResponse)
async def resolve_conflict_htmx(
    request: Request,
    conflict_id: uuid.UUID,
    resolution: str = Form(...),
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: resolve a conflict and return updated HTML."""
    # Get conflict details
    result = await session.execute(
        text(
            "SELECT id, document_a_id, document_b_id, conflict_type, "
            "confidence, details, status, created_at "
            "FROM conflict_candidates WHERE id = :cid"
        ),
        {"cid": conflict_id},
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
            text("UPDATE documents SET review_status = 'superseded' WHERE id = :did"),
            {"did": superseded_doc_id},
        )
        await session.execute(
            text("UPDATE claims SET status = 'superseded' WHERE document_id = :did"),
            {"did": superseded_doc_id},
        )

    await session.execute(
        text(
            "UPDATE conflict_candidates SET status = 'resolved', "
            "details = details || :res WHERE id = :cid"
        ),
        {
            "cid": conflict_id,
            "res": {"resolution": resolution, "comment": comment},
        },
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


@router.get("/reviews", response_class=HTMLResponse)
async def reviews_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
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


@router.post("/api/reviews/{review_id}/approve", response_class=HTMLResponse)
async def approve_review_htmx(
    review_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    result = await session.execute(
        text("SELECT id, document_id FROM review_tasks WHERE id = :rid"),
        {"rid": review_id},
    )
    review = result.first()
    if not review:
        return HTMLResponse("<div class='text-red-600'>Not found</div>")

    await session.execute(
        text(
            "UPDATE review_tasks SET status = 'approved', "
            "completed_at = now() WHERE id = :rid"
        ),
        {"rid": review_id},
    )
    if review.document_id:
        await session.execute(
            text(
                "UPDATE documents SET review_status = 'published' "
                "WHERE id = :did"
            ),
            {"did": review.document_id},
        )
    await session.commit()

    return HTMLResponse(
        f'<div id="review-{review_id}" class="bg-green-50 rounded border '
        f'border-green-200 p-3 text-green-800 text-sm">Approved</div>'
    )


@router.post("/api/reviews/{review_id}/reject", response_class=HTMLResponse)
async def reject_review_htmx(
    review_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    result = await session.execute(
        text("SELECT id, document_id FROM review_tasks WHERE id = :rid"),
        {"rid": review_id},
    )
    review = result.first()
    if not review:
        return HTMLResponse("<div class='text-red-600'>Not found</div>")

    await session.execute(
        text(
            "UPDATE review_tasks SET status = 'rejected', "
            "completed_at = now() WHERE id = :rid"
        ),
        {"rid": review_id},
    )
    if review.document_id:
        await session.execute(
            text(
                "UPDATE documents SET review_status = 'rejected' "
                "WHERE id = :did"
            ),
            {"did": review.document_id},
        )
    await session.commit()

    return HTMLResponse(
        f'<div id="review-{review_id}" class="bg-red-50 rounded border '
        f'border-red-200 p-3 text-red-800 text-sm">Rejected</div>'
    )
