import uuid

from raasoa.retrieval.confidence import compute_confidence
from raasoa.retrieval.hybrid_search import SearchResult


def _make_result(score: float, doc_id: uuid.UUID | None = None) -> SearchResult:
    return SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=doc_id or uuid.uuid4(),
        chunk_text="test",
        section_title=None,
        chunk_type="text",
        score=score,
    )


def test_empty_results() -> None:
    c = compute_confidence([])
    assert c.retrieval_confidence == 0.0
    assert c.answerable is False
    assert c.source_count == 0


def test_high_score_results() -> None:
    results = [_make_result(0.03), _make_result(0.02), _make_result(0.01)]
    c = compute_confidence(results)
    assert c.retrieval_confidence > 0.5
    assert c.answerable is True
    assert c.source_count == 3


def test_low_score_single_result() -> None:
    results = [_make_result(0.001)]
    c = compute_confidence(results)
    assert c.answerable is False


def test_same_document_sources() -> None:
    doc_id = uuid.uuid4()
    results = [_make_result(0.03, doc_id), _make_result(0.02, doc_id)]
    c = compute_confidence(results)
    assert c.source_count == 1


def test_reranked_scores_saturate_confidence_without_correct_max_score() -> None:
    """Regression guard documenting F-018's failure mode: a [0,1]-scale
    reranker score run through the RRF-scale default saturates confidence
    to ~1.0 regardless of quality, defeating min_confidence refusal."""
    weak_reranked_results = [_make_result(0.15)]  # a mediocre [0,1] relevance score
    c_wrong_scale = compute_confidence(weak_reranked_results)
    # A mediocre 0.15 relevance score reads as near-maximum confidence
    # under the RRF-scale default (0.15 / 0.033 saturates past 1.0) —
    # comfortably clearing the default 0.3 min_confidence refusal gate.
    assert c_wrong_scale.retrieval_confidence > 0.8
    assert c_wrong_scale.answerable is False  # needs >= 2 results too


def test_max_score_normalizes_reranked_scores_correctly() -> None:
    """The fix: passing the reranker's own SCORE_SCALE keeps confidence
    meaningful for [0,1]-scale scores instead of always saturating."""
    weak_reranked_results = [_make_result(0.15)]
    c_correct_scale = compute_confidence(weak_reranked_results, max_score=1.0)
    assert c_correct_scale.retrieval_confidence < 0.3
    assert c_correct_scale.answerable is False

    strong_reranked_results = [_make_result(0.95), _make_result(0.9), _make_result(0.85)]
    c_strong = compute_confidence(strong_reranked_results, max_score=1.0)
    assert c_strong.retrieval_confidence > 0.7
    assert c_strong.answerable is True
