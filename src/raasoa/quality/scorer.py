"""Aggregate quality scoring and publish decision logic."""

from __future__ import annotations

from dataclasses import dataclass, field

from raasoa.config import settings
from raasoa.quality.checks import FindingResult


@dataclass
class QualityAssessment:
    quality_score: float
    # "published" | "published_with_warnings" | "quarantined" | "needs_review"
    publish_decision: str
    findings: list[FindingResult] = field(default_factory=list)


def compute_quality_score(findings: list[FindingResult]) -> QualityAssessment:
    """Compute aggregate quality score and publish decision from findings."""
    score = 1.0
    has_critical = False

    for f in findings:
        score -= f.score_penalty
        if f.severity == "critical":
            has_critical = True

    score = max(0.0, min(1.0, score))

    # Decision logic
    if has_critical and score < 0.3:
        decision = "quarantined"
    elif has_critical:
        decision = "needs_review"
    elif score >= settings.quality_publish_threshold:
        decision = "published"
    elif score >= settings.quality_review_threshold:
        decision = "published_with_warnings"
    else:
        decision = "needs_review"

    return QualityAssessment(
        quality_score=round(score, 3),
        publish_decision=decision,
        findings=findings,
    )
