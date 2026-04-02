"""Tests for the query router."""

from raasoa.retrieval.query_router import QueryType, route_query


def test_routes_rag_for_how_to_questions() -> None:
    result = route_query("How do I configure embedding models?")
    assert result.query_type == QueryType.RAG


def test_routes_rag_for_what_is_questions() -> None:
    result = route_query("What is the difference between hot and cold indexing?")
    assert result.query_type == QueryType.RAG


def test_routes_rag_for_explain_questions() -> None:
    result = route_query("Explain the quality gate scoring mechanism")
    assert result.query_type == QueryType.RAG


def test_routes_structured_for_count_questions() -> None:
    result = route_query("How many documents are in the system?")
    assert result.query_type == QueryType.STRUCTURED


def test_routes_structured_for_list_all() -> None:
    result = route_query("List all documents about finance")
    assert result.query_type == QueryType.STRUCTURED


def test_routes_structured_for_quality_score() -> None:
    result = route_query("What is the average quality score?")
    assert result.query_type == QueryType.STRUCTURED


def test_routes_structured_for_conflicts() -> None:
    result = route_query("Show conflicts between documents")
    assert result.query_type == QueryType.STRUCTURED


def test_routes_structured_for_latest() -> None:
    result = route_query("Show latest documents uploaded")
    assert result.query_type == QueryType.STRUCTURED


def test_default_to_rag_for_ambiguous_queries() -> None:
    result = route_query("Power BI visualization")
    assert result.query_type == QueryType.RAG
    assert result.confidence == 0.5


def test_confidence_is_higher_for_strong_matches() -> None:
    rag = route_query("What is our data strategy?")
    ambiguous = route_query("data strategy")
    assert rag.confidence > ambiguous.confidence
