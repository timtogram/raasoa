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
