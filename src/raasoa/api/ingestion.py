import uuid

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.models.source import Source
from raasoa.models.tenant import Tenant
from raasoa.providers.factory import get_embedding_provider
from raasoa.schemas.ingestion import IngestResponse, QualityFindingSummary

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
            "SELECT id FROM sources WHERE tenant_id = :tid AND source_type = 'file_upload'"
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
    file: UploadFile = File(...),
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    session: AsyncSession = Depends(get_session),
) -> IngestResponse:
    """Upload and ingest a document with quality assessment."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    file_data = await file.read()
    if not file_data:
        raise HTTPException(status_code=400, detail="Empty file")

    max_size = settings.max_file_size_mb * 1024 * 1024
    if len(file_data) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(file_data)} bytes). Max: {settings.max_file_size_mb}MB",
        )

    tenant_id, source_id = await _ensure_default_tenant_and_source(session, tenant_id)

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
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}") from e

    # Refresh doc from DB (session may have expired after commits in pipeline)
    await session.refresh(doc)

    # Build quality findings summary
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
