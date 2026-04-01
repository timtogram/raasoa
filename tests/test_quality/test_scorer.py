from raasoa.quality.checks import FindingResult
from raasoa.quality.scorer import compute_quality_score


def test_no_findings() -> None:
    result = compute_quality_score([])
    assert result.quality_score == 1.0
    assert result.publish_decision == "published"


def test_single_warning() -> None:
    findings = [FindingResult("test", "warning", {}, 0.1)]
    result = compute_quality_score(findings)
    assert result.quality_score == 0.9
    assert result.publish_decision == "published"


def test_multiple_warnings() -> None:
    findings = [
        FindingResult("a", "warning", {}, 0.15),
        FindingResult("b", "warning", {}, 0.15),
        FindingResult("c", "warning", {}, 0.15),
    ]
    result = compute_quality_score(findings)
    assert result.quality_score == 0.55
    assert result.publish_decision == "published_with_warnings"


def test_critical_finding() -> None:
    findings = [FindingResult("bad", "critical", {}, 0.5)]
    result = compute_quality_score(findings)
    assert result.quality_score == 0.5
    assert result.publish_decision == "needs_review"


def test_critical_very_low_score() -> None:
    findings = [
        FindingResult("a", "critical", {}, 0.5),
        FindingResult("b", "critical", {}, 0.3),
    ]
    result = compute_quality_score(findings)
    assert result.quality_score == 0.2
    assert result.publish_decision == "quarantined"


def test_score_never_negative() -> None:
    findings = [FindingResult("x", "critical", {}, 2.0)]
    result = compute_quality_score(findings)
    assert result.quality_score == 0.0
