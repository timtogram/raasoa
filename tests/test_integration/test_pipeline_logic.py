"""Integration tests for pipeline logic — tests the full chain without DB.

Verifies that the modules work together correctly:
- Parser → Chunker → Hasher chain
- Quality checks → scorer chain
- Query router → structured/RAG routing
"""

from raasoa.ingestion.chunker import ChunkResult, chunk_document
from raasoa.ingestion.hasher import content_hash, file_hash
from raasoa.ingestion.parser import ParsedDocument, parse_file
from raasoa.quality.checks import run_all_checks
from raasoa.quality.scorer import compute_quality_score
from raasoa.retrieval.query_router import QueryType, route_query


class TestParseChunkHashChain:
    """Test the full parse → chunk → hash pipeline."""

    def test_txt_parse_chunk_hash(self) -> None:
        content = (
            b"# My Document\n\n"
            + b"This is a test document with enough content to generate chunks. " * 10
        )
        parsed = parse_file(content, "test.txt")

        assert parsed.title == "My Document"
        assert len(parsed.full_text) > 100

        chunks = chunk_document(parsed.full_text, title=parsed.title)
        assert len(chunks) >= 1

        # All chunks should have content hashes
        hashes = [content_hash(c.text) for c in chunks]
        assert all(isinstance(h, bytes) for h in hashes)
        assert len(set(hashes)) == len(hashes)

    def test_md_parse_sections(self) -> None:
        md = (
            b"# Title\n\n## Section 1\n\nContent of section 1."
            b"\n\n## Section 2\n\nContent of section 2."
        )
        parsed = parse_file(md, "doc.md")
        assert parsed.title == "Title"
        assert "Section 1" in parsed.full_text

    def test_file_hash_deterministic(self) -> None:
        data = b"Hello World"
        assert file_hash(data) == file_hash(data)

    def test_file_hash_different_for_different_content(self) -> None:
        assert file_hash(b"Content A") != file_hash(b"Content B")

    def test_chunk_hash_deterministic(self) -> None:
        text = "This is a test chunk."
        assert content_hash(text) == content_hash(text)


class TestQualityPipeline:
    """Test quality checks → scorer chain."""

    def test_good_document_gets_high_score(self) -> None:
        text = "Substantial content about data visualization. " * 50
        parsed = ParsedDocument(
            full_text=text, title="Good Document", metadata={"format": "txt"}
        )
        chunks = [
            ChunkResult(
                index=i,
                text=f"This is a substantial chunk about topic {i}. " * 5,
                section_title=f"Section {i}",
                chunk_type="text",
                token_count=80,
            )
            for i in range(5)
        ]

        findings = run_all_checks(parsed=parsed, chunks=chunks, embedded_count=5)
        assessment = compute_quality_score(findings)

        assert assessment.quality_score >= 0.8
        assert assessment.publish_decision in ("published", "published_with_warnings")

    def test_empty_document_gets_quarantined(self) -> None:
        parsed = ParsedDocument(full_text="", title=None, metadata={})
        findings = run_all_checks(parsed=parsed, chunks=[], embedded_count=0)
        assessment = compute_quality_score(findings)

        assert assessment.quality_score < 0.5
        assert assessment.publish_decision in ("quarantined", "needs_review")

    def test_short_document_gets_warning(self) -> None:
        parsed = ParsedDocument(
            full_text="Short text.", title="Test", metadata={}
        )
        chunks = [
            ChunkResult(
                index=0, text="Short text.", section_title=None,
                chunk_type="text", token_count=5,
            )
        ]
        findings = run_all_checks(parsed=parsed, chunks=chunks, embedded_count=1)
        assert len(findings) >= 1

    def test_no_title_creates_finding(self) -> None:
        text = "Content without title " * 20
        parsed = ParsedDocument(full_text=text, title=None, metadata={})
        chunks = [
            ChunkResult(
                index=0, text=text, section_title=None,
                chunk_type="text", token_count=40,
            )
        ]
        findings = run_all_checks(parsed=parsed, chunks=chunks, embedded_count=1)
        finding_types = [f.finding_type for f in findings]
        assert "no_title" in finding_types

    def test_embedding_failures_detected(self) -> None:
        text = "x" * 500
        parsed = ParsedDocument(full_text=text, title="Test", metadata={})
        chunks = [
            ChunkResult(
                index=i, text=f"Chunk {i} content " * 10,
                section_title=None, chunk_type="text", token_count=30,
            )
            for i in range(10)
        ]
        findings = run_all_checks(
            parsed=parsed, chunks=chunks, embedded_count=2,
        )
        finding_types = [f.finding_type for f in findings]
        assert "embedding_failures" in finding_types


class TestQueryRouterIntegration:
    """Test query router with realistic queries."""

    def test_knowledge_questions_route_to_rag(self) -> None:
        queries = [
            "What is our company's data strategy?",
            "How does the quality gate work?",
            "Explain the tiered indexing approach",
            "Why did we choose PostgreSQL?",
        ]
        for q in queries:
            result = route_query(q)
            assert result.query_type == QueryType.RAG, (
                f"Expected RAG for: {q}"
            )

    def test_metadata_questions_route_to_structured(self) -> None:
        queries = [
            "How many documents do we have?",
            "List all documents about finance",
            "What is the quality score overview?",
            "Show conflicts between our documents",
        ]
        for q in queries:
            result = route_query(q)
            assert result.query_type == QueryType.STRUCTURED, (
                f"Expected STRUCTURED for: {q}"
            )

    def test_ambiguous_queries_default_to_rag(self) -> None:
        result = route_query("Power BI SAP visualization")
        assert result.query_type == QueryType.RAG
        assert result.confidence <= 0.5
