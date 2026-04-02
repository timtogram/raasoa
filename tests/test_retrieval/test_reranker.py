"""Tests for reranker strategies."""

import uuid

import pytest

from raasoa.retrieval.hybrid_search import SearchResult
from raasoa.retrieval.reranker import PassthroughReranker


def _make_result(score: float, text: str = "test") -> SearchResult:
    return SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        chunk_text=text,
        section_title=None,
        chunk_type="text",
        score=score,
    )


@pytest.mark.asyncio
async def test_passthrough_reranker_returns_top_k() -> None:
    reranker = PassthroughReranker()
    results = [_make_result(0.9), _make_result(0.8), _make_result(0.7)]
    reranked = await reranker.rerank("test query", results, top_k=2)
    assert len(reranked) == 2
    assert reranked[0].score == 0.9


@pytest.mark.asyncio
async def test_passthrough_reranker_handles_empty() -> None:
    reranker = PassthroughReranker()
    reranked = await reranker.rerank("test", [], top_k=5)
    assert reranked == []
