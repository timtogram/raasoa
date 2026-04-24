import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.ingestion.chunker import chunk_document
from raasoa.ingestion.hasher import content_hash, file_hash
from raasoa.ingestion.parser import parse_file
from raasoa.models.chunk import Chunk
from raasoa.models.document import Document, DocumentVersion
from raasoa.providers.base import EmbeddingProvider
from raasoa.quality.gate import run_quality_gate
from raasoa.quality.scorer import QualityAssessment


async def ingest_file(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    file_data: bytes,
    filename: str,
    embedding_provider: EmbeddingProvider,
    source_object_id: str | None = None,
    source_url: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    last_modified: datetime | None = None,
) -> tuple[Document, QualityAssessment | None]:
    """Full ingestion pipeline: Parse → Chunk → Hash → Embed → Quality Gate → Store.

    Returns the document and its quality assessment (None if quality gates disabled).
    """

    # 1. Parse
    parsed = parse_file(file_data, filename)

    # 2. Content hash (document level)
    doc_hash = file_hash(file_data)

    # 3. Check if document already exists
    source_object_id = source_object_id or filename
    doc_metadata: dict[str, Any] | None = None
    if source_metadata or parsed.frontmatter:
        doc_metadata = dict(source_metadata or {})
        doc_metadata.update(parsed.frontmatter or {})
    existing = await session.execute(
        text(
            "SELECT id, content_hash, version FROM documents "
            "WHERE tenant_id = :tid AND source_id = :sid AND source_object_id = :soid"
        ),
        {"tid": tenant_id, "sid": source_id, "soid": source_object_id},
    )
    row = existing.first()

    if row and row.content_hash == doc_hash:
        doc = await session.get(Document, row.id)
        if doc is None:
            raise ValueError(f"Document {row.id} not found after existence check")
        doc.last_synced_at = datetime.now(UTC)
        if source_url is not None:
            doc.source_url = source_url
        if last_modified is not None:
            doc.last_modified = last_modified
        if doc_metadata is not None:
            doc.doc_metadata = doc_metadata
        await session.commit()
        return doc, None

    now = datetime.now(UTC)
    is_version_update = False  # Track if this is an update, not a new doc

    if row:
        is_version_update = True
        doc = await session.get(Document, row.id)
        if doc is None:
            raise ValueError(f"Document {row.id} not found after existence check")
        doc.content_hash = doc_hash
        doc.version = row.version + 1
        doc.title = parsed.title
        doc.doc_type = parsed.metadata.get("format", "unknown")
        doc.source_url = source_url
        doc.last_modified = last_modified
        doc.last_synced_at = now
        doc.status = "processing"
        doc.doc_metadata = doc_metadata

        # Delete old chunks and quality findings
        await session.execute(
            text("DELETE FROM chunks WHERE document_id = :did"), {"did": doc.id}
        )
        await session.execute(
            text("DELETE FROM quality_findings WHERE document_id = :did"), {"did": doc.id}
        )
    else:
        doc = Document(
            tenant_id=tenant_id,
            source_id=source_id,
            source_object_id=source_object_id,
            content_hash=doc_hash,
            title=parsed.title,
            doc_type=parsed.metadata.get("format", "unknown"),
            source_url=source_url,
            last_modified=last_modified,
            last_synced_at=now,
            status="processing",
            embedding_model=embedding_provider.model_id,
            version=1,
            doc_metadata=doc_metadata,
        )
        session.add(doc)
        await session.flush()

    # 4. Create document version (with content snapshot for diffs)
    doc_version = DocumentVersion(
        document_id=doc.id,
        version=doc.version,
        content_hash=doc_hash,
        parser_version="v1",
        chunking_strategy_version="recursive-512-80",
        content_snapshot=parsed.full_text,
    )
    session.add(doc_version)

    # 5. Chunk (pass sections for page/location tracking)
    chunk_results = chunk_document(
        parsed.full_text, title=parsed.title, sections=parsed.sections,
    )

    if not chunk_results:
        doc.chunk_count = 0
        # Run quality gate even for empty results
        if settings.quality_gate_enabled:
            assessment = await run_quality_gate(
                session=session, doc=doc, parsed=parsed,
                chunks=chunk_results, embedded_count=0,
            )
            if assessment.publish_decision == "quarantined":
                doc.status = "quarantined"
            else:
                doc.status = "indexed"
        else:
            doc.status = "indexed"
            doc.review_status = "auto_published"
            assessment = None
        await session.commit()
        return doc, assessment

    # 6. Compute chunk hashes
    chunk_hashes = [content_hash(c.text) for c in chunk_results]

    # 7. Embed all chunks (set tenant for metering)
    if hasattr(embedding_provider, "_current_tenant_id"):
        embedding_provider._current_tenant_id = str(tenant_id)
    texts = [c.text for c in chunk_results]
    embeddings = await embedding_provider.embed(texts)

    # Count successful embeddings (non-zero vectors)
    embedded_count = sum(1 for e in embeddings if any(v != 0.0 for v in e))

    # 8. Create chunk records
    for cr, emb, ch in zip(chunk_results, embeddings, chunk_hashes, strict=True):
        chunk = Chunk(
            document_id=doc.id,
            chunk_index=cr.index,
            content_hash=ch,
            chunk_text=cr.text,
            section_title=cr.section_title,
            chunk_type=cr.chunk_type,
            token_count=cr.token_count,
            page_number=cr.page_number,
            source_location=cr.source_location,
            embedding=emb,
            embedding_model=embedding_provider.model_id,
            embedded_at=now,
        )
        session.add(chunk)

    await session.flush()

    # Update tsvector
    await session.execute(
        text(
            "UPDATE chunks SET tsv = to_tsvector('simple', chunk_text) "
            "WHERE document_id = :did AND tsv IS NULL"
        ),
        {"did": doc.id},
    )

    # 9. Update document metadata
    doc.chunk_count = len(chunk_results)
    doc.last_embedded_at = now

    # 10. Quality Gate + Schema Check
    final_assessment: QualityAssessment | None = None
    if settings.quality_gate_enabled:
        final_assessment = await run_quality_gate(
            session=session,
            doc=doc,
            parsed=parsed,
            chunks=chunk_results,
            embedded_count=embedded_count,
            chunk_hashes=chunk_hashes,
        )

        # Schema check (pluggable, type-specific quality)
        try:
            from raasoa.models.governance import QualityFinding
            from raasoa.quality.schema_checks import run_schema_check

            # doc type comes from frontmatter (type: policy/skill/...)
            fm = parsed.frontmatter or {}
            doc_type_hint = fm.get("type")

            schema_result = run_schema_check(
                doc_type_hint=doc_type_hint,
                frontmatter=fm,
                content=parsed.full_text,
            )
            if schema_result:
                from raasoa.quality.checks import FindingResult

                # Persist findings so they show in UI/API
                for finding in schema_result.findings:
                    session.add(QualityFinding(
                        document_id=doc.id,
                        finding_type=f"schema_{finding.check}",
                        severity=finding.severity,
                        details={
                            "message": finding.message,
                            "doc_type": schema_result.doc_type,
                        },
                    ))
                    # Also add to assessment.findings so the API response
                    # reflects them (they were missing before the post-commit
                    # refresh because the assessment was built earlier).
                    if final_assessment is not None:
                        final_assessment.findings.append(FindingResult(
                            finding_type=f"schema_{finding.check}",
                            severity=finding.severity,
                            details={
                                "message": finding.message,
                                "doc_type": schema_result.doc_type,
                            },
                        ))
                if (
                    schema_result.score_penalty > 0
                    and doc.quality_score is not None
                ):
                    doc.quality_score = max(
                        0.0,
                        doc.quality_score - schema_result.score_penalty,
                    )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Schema check failed")

        is_quarantined = final_assessment.publish_decision == "quarantined"
        doc.status = "quarantined" if is_quarantined else "indexed"
    else:
        doc.status = "indexed"
        doc.review_status = "auto_published"

    await session.commit()

    # 11. Conflict Detection (skip for version updates — those are supersessions, not conflicts)
    if settings.conflict_detection_enabled and embeddings and not is_version_update:
        try:
            from raasoa.quality.conflicts import detect_conflicts

            await detect_conflicts(
                session=session,
                doc=doc,
                tenant_id=tenant_id,
                chunk_hashes=chunk_hashes,
                chunk_embeddings=embeddings,
            )
            await session.commit()
        except Exception:
            await session.rollback()

    # 12. Claim Extraction + Claim-based Conflict Detection
    if settings.claim_extraction_enabled:
        try:
            from raasoa.quality.claim_conflicts import detect_claim_conflicts
            from raasoa.quality.claims import extract_and_store_claims

            # Get chunk IDs from DB (they were flushed earlier)
            chunk_rows = await session.execute(
                text(
                    "SELECT id, chunk_text FROM chunks "
                    "WHERE document_id = :did ORDER BY chunk_index"
                ),
                {"did": doc.id},
            )
            chunk_pairs = [(r.id, r.chunk_text) for r in chunk_rows.fetchall()]

            claims = await extract_and_store_claims(
                session=session,
                tenant_id=tenant_id,
                document_id=doc.id,
                chunks=chunk_pairs,
            )
            await session.commit()

            if claims:
                await detect_claim_conflicts(
                    session=session,
                    document_id=doc.id,
                    tenant_id=tenant_id,
                    new_claims=claims,
                    embedding_provider=embedding_provider,
                )
                await session.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Claim extraction failed: %s", e)
            await session.rollback()

    # 13. Auto-curate: rebuild index + enqueue full curation job
    if settings.claim_extraction_enabled:
        try:
            from raasoa.retrieval.knowledge_index import build_index

            await build_index(session, tenant_id)
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "Auto index rebuild failed", exc_info=True,
            )

        # Enqueue async curation (normalize predicates, lint)
        try:
            from raasoa.worker.queue import enqueue

            await enqueue(session, tenant_id, "curate", priority=-1)
            await session.commit()
        except Exception:
            await session.rollback()  # Clean up failed transaction

    # 14. LLM Judge: auto-resolve high-confidence conflicts
    if (
        settings.llm_judge_enabled
        and settings.conflict_detection_enabled
        and doc.conflict_status == "conflicts_detected"
    ):
        try:
            from raasoa.quality.judge import auto_resolve_conflicts

            judge_stats = await auto_resolve_conflicts(session, tenant_id)
            if judge_stats.get("auto_resolved", 0) > 0:
                import logging
                logging.getLogger(__name__).info(
                    "LLM Judge auto-resolved %d conflicts for doc %s",
                    judge_stats["auto_resolved"], doc.id,
                )
        except Exception:
            pass  # Judge is best-effort

    return doc, final_assessment
