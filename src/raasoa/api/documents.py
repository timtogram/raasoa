import base64
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant
from raasoa.schemas.document import (
    ChunkDetail,
    DocumentSummary,
    DocumentWithChunks,
    PaginatedDocuments,
)

router = APIRouter(prefix="/v1", tags=["documents"])


def _encode_cursor(created_at: str, doc_id: str) -> str:
    return base64.urlsafe_b64encode(f"{created_at}|{doc_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    parts = decoded.split("|", 1)
    if len(parts) != 2:
        raise ValueError("Invalid cursor format")
    return parts[0], parts[1]


@router.get("/documents", response_model=PaginatedDocuments)
async def list_documents(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> PaginatedDocuments:
    """List documents with cursor-based pagination."""
    tenant_id = resolve_tenant(request)
    params: dict = {"tid": tenant_id, "lim": limit + 1}

    if cursor:
        try:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            cursor_uuid = uuid.UUID(cursor_id)
        except (ValueError, Exception) as err:
            raise HTTPException(
                status_code=400, detail="Invalid cursor"
            ) from err

        sql = text(
            "SELECT id, title, source_object_id, doc_type, status, "
            "chunk_count, version, index_tier, quality_score, "
            "last_synced_at, last_embedded_at, created_at "
            "FROM documents WHERE tenant_id = :tid "
            "AND status != 'deleted' "
            "AND (created_at, id) < "
            "  (CAST(:cursor_ts AS timestamptz), :cursor_id) "
            "ORDER BY created_at DESC, id DESC LIMIT :lim"
        )
        params["cursor_ts"] = cursor_ts
        params["cursor_id"] = cursor_uuid
    else:
        sql = text(
            "SELECT id, title, source_object_id, doc_type, status, "
            "chunk_count, version, index_tier, quality_score, "
            "last_synced_at, last_embedded_at, created_at "
            "FROM documents WHERE tenant_id = :tid "
            "AND status != 'deleted' "
            "ORDER BY created_at DESC, id DESC LIMIT :lim"
        )

    result = await session.execute(sql, params)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(str(last.created_at), str(last.id))

    return PaginatedDocuments(
        items=[
            DocumentSummary(
                id=r.id, title=r.title,
                source_object_id=r.source_object_id,
                doc_type=r.doc_type, status=r.status,
                chunk_count=r.chunk_count, version=r.version,
                index_tier=r.index_tier,
                quality_score=r.quality_score,
                last_synced_at=r.last_synced_at,
                last_embedded_at=r.last_embedded_at,
                created_at=r.created_at,
            )
            for r in items
        ],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/documents/{document_id}", response_model=DocumentWithChunks)
async def get_document(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> DocumentWithChunks:
    """Get document details with all chunks (tenant-scoped)."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT id, title, source_object_id, doc_type, status, "
            "chunk_count, version, index_tier, quality_score, "
            "last_synced_at, last_embedded_at, created_at, "
            "embedding_model, review_status, conflict_status, "
            "access_count "
            "FROM documents WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    doc = result.first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    chunk_result = await session.execute(
        text(
            "SELECT id, chunk_index, chunk_text, section_title, "
            "chunk_type, token_count, embedding_model, embedded_at "
            "FROM chunks WHERE document_id = :did ORDER BY chunk_index"
        ),
        {"did": document_id},
    )
    chunks = chunk_result.fetchall()

    await session.execute(
        text(
            "UPDATE documents SET access_count = access_count + 1, "
            "last_accessed_at = now() WHERE id = :did"
        ),
        {"did": document_id},
    )
    await session.commit()

    return DocumentWithChunks(
        id=doc.id, title=doc.title,
        source_object_id=doc.source_object_id,
        doc_type=doc.doc_type, status=doc.status,
        chunk_count=doc.chunk_count, version=doc.version,
        index_tier=doc.index_tier,
        quality_score=doc.quality_score,
        last_synced_at=doc.last_synced_at,
        last_embedded_at=doc.last_embedded_at,
        created_at=doc.created_at,
        embedding_model=doc.embedding_model,
        review_status=doc.review_status,
        conflict_status=doc.conflict_status,
        access_count=doc.access_count,
        chunks=[
            ChunkDetail(
                id=c.id, chunk_index=c.chunk_index,
                chunk_text=c.chunk_text,
                section_title=c.section_title,
                chunk_type=c.chunk_type,
                token_count=c.token_count,
                embedding_model=c.embedding_model,
                embedded_at=c.embedded_at,
            )
            for c in chunks
        ],
    )


@router.delete("/documents/{document_id}")
async def delete_document(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Soft-delete a document (tenant-scoped)."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Document not found")

    await session.execute(
        text(
            "UPDATE documents SET status = 'deleted', "
            "review_status = 'rejected' WHERE id = :did"
        ),
        {"did": document_id},
    )
    await session.execute(
        text(
            "UPDATE claims SET status = 'rejected' "
            "WHERE document_id = :did"
        ),
        {"did": document_id},
    )
    await session.commit()
    return {"status": "deleted", "document_id": str(document_id)}
