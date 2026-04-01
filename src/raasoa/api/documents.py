import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.schemas.document import (
    ChunkDetail,
    DocumentSummary,
    DocumentWithChunks,
)

router = APIRouter(prefix="/v1", tags=["documents"])


@router.get("/documents", response_model=list[DocumentSummary])
async def list_documents(
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[DocumentSummary]:
    """List all documents for a tenant."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    result = await session.execute(
        text(
            "SELECT id, title, source_object_id, doc_type, status, chunk_count, "
            "version, index_tier, quality_score, last_synced_at, last_embedded_at, created_at "
            "FROM documents WHERE tenant_id = :tid "
            "ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        ),
        {"tid": tenant_id, "lim": limit, "off": offset},
    )
    rows = result.fetchall()

    return [
        DocumentSummary(
            id=r.id,
            title=r.title,
            source_object_id=r.source_object_id,
            doc_type=r.doc_type,
            status=r.status,
            chunk_count=r.chunk_count,
            version=r.version,
            index_tier=r.index_tier,
            quality_score=r.quality_score,
            last_synced_at=r.last_synced_at,
            last_embedded_at=r.last_embedded_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/documents/{document_id}", response_model=DocumentWithChunks)
async def get_document(
    document_id: uuid.UUID,
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    session: AsyncSession = Depends(get_session),
) -> DocumentWithChunks:
    """Get document details with all chunks."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    result = await session.execute(
        text(
            "SELECT id, title, source_object_id, doc_type, status, chunk_count, "
            "version, index_tier, quality_score, last_synced_at, last_embedded_at, "
            "created_at, embedding_model, review_status, conflict_status, access_count "
            "FROM documents WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    doc = result.first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get chunks
    chunk_result = await session.execute(
        text(
            "SELECT id, chunk_index, chunk_text, section_title, chunk_type, "
            "token_count, embedding_model, embedded_at "
            "FROM chunks WHERE document_id = :did ORDER BY chunk_index"
        ),
        {"did": document_id},
    )
    chunks = chunk_result.fetchall()

    # Increment access count
    await session.execute(
        text(
            "UPDATE documents SET access_count = access_count + 1, "
            "last_accessed_at = now() WHERE id = :did"
        ),
        {"did": document_id},
    )
    await session.commit()

    return DocumentWithChunks(
        id=doc.id,
        title=doc.title,
        source_object_id=doc.source_object_id,
        doc_type=doc.doc_type,
        status=doc.status,
        chunk_count=doc.chunk_count,
        version=doc.version,
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
                id=c.id,
                chunk_index=c.chunk_index,
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
