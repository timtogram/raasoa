"""Tests for the Cohere embedding provider fixes (F-021).

Before this fix: (1) embed() never sent an output-dimension parameter,
so embed-v4.0 defaulted to its native 1536-dim output while the chunks
column is Vector(settings.embedding_dimensions) (768 by default) —
every ingest failed at insert with EMBEDDING_PROVIDER=cohere; (2)
input_type was hardcoded to "search_document" even for query
embeddings, degrading ranking under Cohere's asymmetric embedding model.
"""
from __future__ import annotations

import json

import httpx
import pytest

from raasoa.providers.cohere import CohereEmbeddingProvider


def _capturing_transport(calls: list[dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        n = len(body["texts"])
        dim = body.get("output_dimension", 1536)
        return httpx.Response(200, json={
            "embeddings": {"float": [[0.1] * dim for _ in range(n)]},
        })

    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    transport = _capturing_transport(calls)
    real_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return calls


class TestOutputDimension:
    async def test_sends_configured_output_dimension(
        self, patched_client: list[dict[str, object]],
    ) -> None:
        provider = CohereEmbeddingProvider(
            api_key="k", base_url="https://api.cohere.test",
            model="embed-v4.0", dimensions=768,
        )
        result = await provider.embed(["hello"])
        assert patched_client[0]["output_dimension"] == 768
        assert len(result[0]) == 768

    async def test_respects_non_default_dimensions(
        self, patched_client: list[dict[str, object]],
    ) -> None:
        provider = CohereEmbeddingProvider(
            api_key="k", base_url="https://api.cohere.test",
            model="embed-v4.0", dimensions=1024,
        )
        await provider.embed(["hello"])
        assert patched_client[0]["output_dimension"] == 1024


class TestInputType:
    async def test_defaults_to_search_document(
        self, patched_client: list[dict[str, object]],
    ) -> None:
        provider = CohereEmbeddingProvider(
            api_key="k", base_url="https://api.cohere.test",
            model="embed-v4.0", dimensions=768,
        )
        await provider.embed(["a document chunk"])
        assert patched_client[0]["input_type"] == "search_document"

    async def test_honors_search_query_for_queries(
        self, patched_client: list[dict[str, object]],
    ) -> None:
        provider = CohereEmbeddingProvider(
            api_key="k", base_url="https://api.cohere.test",
            model="embed-v4.0", dimensions=768,
        )
        await provider.embed(["what is the meal allowance?"], input_type="search_query")
        assert patched_client[0]["input_type"] == "search_query"


class TestEmptyInput:
    async def test_empty_texts_short_circuits(self) -> None:
        provider = CohereEmbeddingProvider(
            api_key="k", base_url="https://api.cohere.test",
            model="embed-v4.0", dimensions=768,
        )
        assert await provider.embed([]) == []
