import httpx

from raasoa.config import settings
from raasoa.providers.base import ScoredDocument


class CohereEmbeddingProvider:
    def __init__(
        self,
        api_key: str = settings.cohere_api_key,
        model: str = settings.cohere_embedding_model,
        dimensions: int = settings.embedding_dimensions,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"cohere/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.cohere.com/v2/embed",
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
        api_key: str = settings.cohere_api_key,
        model: str = "rerank-v3.5",
    ) -> None:
        self._api_key = api_key
        self._model = model

    async def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[ScoredDocument]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.cohere.com/v2/rerank",
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
