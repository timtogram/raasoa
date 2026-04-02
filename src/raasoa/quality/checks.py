"""Rule-based quality checks for ingested documents.

Each check receives a ParsedDocument and/or list of ChunkResults and returns
a FindingResult if a quality issue is detected, or None if the check passes.
No database access, no async — pure functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raasoa.config import settings
from raasoa.ingestion.chunker import ChunkResult
from raasoa.ingestion.parser import ParsedDocument


@dataclass
class FindingResult:
    finding_type: str
    severity: str  # "critical", "warning", "info"
    details: dict[str, Any] = field(default_factory=dict)
    score_penalty: float = 0.0


def check_parse_success(parsed: ParsedDocument) -> FindingResult | None:
    """Check if parser extracted any meaningful text."""
    text = parsed.full_text.strip()
    if not text:
        return FindingResult(
            finding_type="empty_content",
            severity="critical",
            details={"message": "Parser extracted no text content"},
            score_penalty=0.5,
        )
    return None


def check_minimum_length(parsed: ParsedDocument) -> FindingResult | None:
    """Check if document meets minimum text length."""
    text_len = len(parsed.full_text.strip())
    if text_len < 10:
        return FindingResult(
            finding_type="extremely_short_content",
            severity="critical",
            details={"text_length": text_len, "minimum": 10},
            score_penalty=0.4,
        )
    if text_len < settings.quality_min_text_length:
        return FindingResult(
            finding_type="short_content",
            severity="warning",
            details={
                "text_length": text_len,
                "minimum": settings.quality_min_text_length,
            },
            score_penalty=0.15,
        )
    return None


def check_title_present(parsed: ParsedDocument) -> FindingResult | None:
    """Check if a meaningful title was extracted."""
    if not parsed.title or parsed.title == parsed.metadata.get("filename"):
        return FindingResult(
            finding_type="no_title",
            severity="warning",
            details={
                "title": parsed.title,
                "message": "Title is missing or equals filename",
            },
            score_penalty=0.1,
        )
    return None


def check_boilerplate_ratio(parsed: ParsedDocument) -> FindingResult | None:
    """Check for high boilerplate content (many duplicate lines)."""
    lines = [ln.strip() for ln in parsed.full_text.split("\n") if ln.strip()]
    if len(lines) < 5:
        return None  # Too few lines to judge

    unique_lines = set(lines)
    ratio = len(unique_lines) / len(lines)
    if ratio < 0.5:
        return FindingResult(
            finding_type="high_boilerplate",
            severity="warning",
            details={
                "total_lines": len(lines),
                "unique_lines": len(unique_lines),
                "unique_ratio": round(ratio, 3),
            },
            score_penalty=0.15,
        )
    return None


def check_embedding_success(
    chunks: list[ChunkResult], embedded_count: int
) -> FindingResult | None:
    """Check if all chunks were successfully embedded."""
    total = len(chunks)
    if total == 0:
        return None

    if embedded_count < total:
        failed = total - embedded_count
        return FindingResult(
            finding_type="embedding_failures",
            severity="critical",
            details={
                "total_chunks": total,
                "embedded_chunks": embedded_count,
                "failed_chunks": failed,
            },
            score_penalty=0.3,
        )
    return None


def check_chunk_size_distribution(chunks: list[ChunkResult]) -> FindingResult | None:
    """Check if too many chunks are unusually small."""
    if not chunks:
        return None

    tiny_threshold = settings.quality_tiny_chunk_tokens
    tiny_count = sum(1 for c in chunks if c.token_count < tiny_threshold)
    ratio = tiny_count / len(chunks)

    if ratio > settings.quality_max_tiny_chunk_ratio:
        return FindingResult(
            finding_type="too_many_tiny_chunks",
            severity="warning",
            details={
                "tiny_count": tiny_count,
                "total_chunks": len(chunks),
                "tiny_ratio": round(ratio, 3),
                "threshold_tokens": tiny_threshold,
            },
            score_penalty=0.1,
        )
    return None


def check_chunk_count_range(
    parsed: ParsedDocument, chunks: list[ChunkResult]
) -> FindingResult | None:
    """Check if chunk count is reasonable for the text length."""
    text_len = len(parsed.full_text.strip())
    if text_len > 100 and len(chunks) == 0:
        return FindingResult(
            finding_type="no_chunks_from_content",
            severity="warning",
            details={
                "text_length": text_len,
                "chunk_count": 0,
                "message": "Document has content but produced no chunks",
            },
            score_penalty=0.1,
        )
    return None


def run_all_checks(
    parsed: ParsedDocument,
    chunks: list[ChunkResult],
    embedded_count: int,
) -> list[FindingResult]:
    """Run all quality checks and return list of findings."""
    findings: list[FindingResult] = []

    checks = [
        check_parse_success(parsed),
        check_minimum_length(parsed),
        check_title_present(parsed),
        check_boilerplate_ratio(parsed),
        check_embedding_success(chunks, embedded_count),
        check_chunk_size_distribution(chunks),
        check_chunk_count_range(parsed, chunks),
    ]

    for result in checks:
        if result is not None:
            findings.append(result)

    return findings
