import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.middleware.auth import resolve_tenant
from raasoa.middleware.rate_limit import get_ingest_limiter
from raasoa.models.source import Source
from raasoa.models.tenant import Tenant
from raasoa.providers.factory import get_embedding_provider
from raasoa.schemas.ingestion import IngestResponse, QualityFindingSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["ingestion"])


async def _ensure_default_tenant_and_source(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure a default tenant and file-upload source exist."""
    result = await session.execute(
        text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tenant_id}
    )
    if not result.first():
        tenant = Tenant(id=tenant_id, name="Default Tenant")
        session.add(tenant)
        await session.flush()

    result = await session.execute(
        text(
            "SELECT id FROM sources WHERE tenant_id = :tid "
            "AND source_type = 'file_upload'"
        ),
        {"tid": tenant_id},
    )
    row = result.first()
    if row:
        return tenant_id, row.id

    source = Source(
        tenant_id=tenant_id,
        source_type="file_upload",
        name="File Upload",
        connection_config={},
    )
    session.add(source)
    await session.flush()
    return tenant_id, source.id


@router.post("/ingest", response_model=IngestResponse)
async def ingest_document(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> IngestResponse:
    """Upload and ingest a document with quality assessment."""
    tenant_id = resolve_tenant(request)
    get_ingest_limiter().check(str(tenant_id))

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    file_data = await file.read()
    if not file_data:
        raise HTTPException(status_code=400, detail="Empty file")

    max_size = settings.max_file_size_mb * 1024 * 1024
    if len(file_data) > max_size:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large ({len(file_data)} bytes). "
                f"Max: {settings.max_file_size_mb}MB"
            ),
        )

    tenant_id, source_id = await _ensure_default_tenant_and_source(
        session, tenant_id
    )

    provider = get_embedding_provider()

    try:
        doc, assessment = await ingest_file(
            session=session,
            tenant_id=tenant_id,
            source_id=source_id,
            file_data=file_data,
            filename=file.filename,
            embedding_provider=provider,
        )
    except Exception as e:
        logger.exception("Ingestion failed for file '%s'", file.filename)
        raise HTTPException(
            status_code=500,
            detail="Ingestion failed. Check server logs for details.",
        ) from e

    await session.refresh(doc)

    # Audit log
    from raasoa.middleware.audit import audit
    await audit(
        session, tenant_id, request, "document.ingest",
        "document", str(doc.id),
        {"title": doc.title, "chunks": doc.chunk_count,
         "quality": doc.quality_score},
    )

    findings: list[QualityFindingSummary] = []
    if assessment:
        findings = [
            QualityFindingSummary(
                finding_type=f.finding_type,
                severity=f.severity,
                details=f.details,
            )
            for f in assessment.findings
        ]

    return IngestResponse(
        document_id=doc.id,
        title=doc.title,
        status=doc.status,
        chunk_count=doc.chunk_count,
        version=doc.version,
        embedding_model=doc.embedding_model,
        quality_score=doc.quality_score,
        review_status=doc.review_status,
        conflict_status=doc.conflict_status,
        quality_findings=findings,
        message=f"Document '{doc.title}' ingested with {doc.chunk_count} chunks",
    )
