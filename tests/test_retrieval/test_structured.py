"""Tests for structured query module — import and type tests."""

from raasoa.retrieval.structured import StructuredResult


def test_structured_result_creation() -> None:
    result = StructuredResult(
        answer="Total: 5 documents",
        data=[{"total": 5}],
        query_type="document_count",
    )
    assert result.answer == "Total: 5 documents"
    assert result.query_type == "document_count"
    assert len(result.data) == 1
