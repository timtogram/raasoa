"""Tests for reranker strategies."""

import uuid

import httpx
import pytest

from raasoa.providers.base import ScoredDocument
from raasoa.retrieval.hybrid_search import SearchResult
from raasoa.retrieval.reranker import (
    CrossEncoderReranker,
    OllamaReranker,
    PassthroughReranker,
)


def _make_result(score: float, text: str = "test") -> SearchResult:
    return SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        chunk_text=text,
        section_title="Intro",
        chunk_type="text",
        score=score,
        semantic_rank=1,
        lexical_rank=2,
        document_title="A Document",
        source_url="https://example.com/doc",
        source_type="notion",
        source_name="Company Wiki",
        page_number=3,
        source_location="Page 3",
        doc_metadata={"classification": "internal"},
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


def test_score_scale_attributes() -> None:
    """Confidence scoring depends on these being accurate (F-018)."""
    assert PassthroughReranker.SCORE_SCALE == 0.033
    assert CrossEncoderReranker.SCORE_SCALE == 1.0
    assert OllamaReranker.SCORE_SCALE == 1.0


class _FakeRerankProvider:
    async def rerank(
        self, query: str, documents: list[str], top_k: int,
    ) -> list[ScoredDocument]:
        # Reverse the order and assign descending scores.
        return [
            ScoredDocument(index=i, score=1.0 - i * 0.1, text=documents[i])
            for i in reversed(range(len(documents)))
        ]


@pytest.mark.asyncio
async def test_cross_encoder_reranker_preserves_all_fields() -> None:
    """Regression for F-019: rebuilding SearchResult by hand previously
    dropped document_title, source_url, source_type, source_name,
    page_number, source_location, and doc_metadata — every citation and
    hit lost its provenance under a non-default reranker."""
    reranker = CrossEncoderReranker(_FakeRerankProvider())
    original = _make_result(0.5)
    reranked = await reranker.rerank("query", [original], top_k=1)

    assert len(reranked) == 1
    r = reranked[0]
    assert r.chunk_id == original.chunk_id
    assert r.document_id == original.document_id
    assert r.section_title == original.section_title
    assert r.document_title == original.document_title
    assert r.source_url == original.source_url
    assert r.source_type == original.source_type
    assert r.source_name == original.source_name
    assert r.page_number == original.page_number
    assert r.source_location == original.source_location
    assert r.doc_metadata == original.doc_metadata
    # Score IS replaced with the provider's relevance score.
    assert r.score == 1.0


@pytest.mark.asyncio
async def test_ollama_reranker_preserves_all_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression as above, for the Ollama reranker's rebuild."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "0.75"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    reranker = OllamaReranker(base_url="http://ollama.test", model="qwen3:8b")
    original = _make_result(0.02)
    reranked = await reranker.rerank("query", [original], top_k=1)

    assert len(reranked) == 1
    r = reranked[0]
    assert r.document_title == original.document_title
    assert r.source_url == original.source_url
    assert r.doc_metadata == original.doc_metadata
    assert r.score == 0.75
