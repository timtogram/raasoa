"""Protocol interfaces for future AI-assisted quality assessment.

These are NOT called in Phase 1 (rule-based only). They exist so that
when AI-backed implementations are added later, the pipeline can call
through these protocols without structural changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from raasoa.quality.checks import FindingResult


@dataclass
class ContradictionResult:
    is_contradiction: bool
    confidence: float
    explanation: str = ""


@dataclass
class Claim:
    subject: str
    predicate: str
    object_value: str
    unit: str | None = None
    source_chunk_id: str | None = None
    evidence_span: str = ""
    confidence: float = 0.0


class QualityAssessor(Protocol):
    """AI-based quality assessment of document content."""

    async def assess(self, text: str, metadata: dict[str, Any]) -> list[FindingResult]: ...


class ContradictionDetector(Protocol):
    """AI-based contradiction detection between two text passages."""

    async def check_contradiction(
        self, chunk_a_text: str, chunk_b_text: str
    ) -> ContradictionResult: ...


class ClaimExtractor(Protocol):
    """Extract factual claims from text for conflict detection."""

    async def extract_claims(self, text: str) -> list[Claim]: ...
