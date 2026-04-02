"""Cohere embedding and reranking provider.

Configuration:
  EMBEDDING_PROVIDER=cohere
  COHERE_API_KEY=xxx
  COHERE_BASE_URL=https://api.cohere.com  (default, or custom endpoint)
  COHERE_EMBEDDING_MODEL=embed-v4.0
"""

import httpx

from raasoa.config import settings
from raasoa.providers.base import ScoredDocument


class CohereEmbeddingProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        self._api_key = api_key or settings.cohere_api_key
        self._base_url = (base_url or settings.cohere_base_url).rstrip("/")
        self._model = model or settings.cohere_embedding_model
        self._dimensions = dimensions or settings.embedding_dimensions

    @property
    def model_id(self) -> str:
        return f"cohere/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self._base_url}/v2/embed",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "texts": texts,
                    "input_type": "search_document",
                    "embedding_types": ["float"],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]["float"]


class CohereRerankProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "rerank-v3.5",
    ) -> None:
        self._api_key = api_key or settings.cohere_api_key
        self._base_url = (base_url or settings.cohere_base_url).rstrip("/")
        self._model = model

    async def rerank(
        self, query: str, documents: list[str], top_k: int,
    ) -> list[ScoredDocument]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/v2/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                },
            )
            response.raise_for_status()
            data = response.json()
            return [
                ScoredDocument(
                    index=r["index"],
                    score=r["relevance_score"],
                    text=documents[r["index"]],
                )
                for r in data["results"]
            ]
