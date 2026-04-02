"""Reranking strategies for search results.

- PassthroughReranker: No-op, returns results as-is (default)
- CrossEncoderReranker: Uses an external reranking provider (Cohere, etc.)
- OllamaReranker: Uses Ollama's chat API to score query-document relevance
"""

import asyncio
import logging

import httpx

from raasoa.providers.base import RerankProvider
from raasoa.retrieval.hybrid_search import SearchResult

logger = logging.getLogger(__name__)

RERANK_PROMPT = """Rate the relevance of the following text passage to the query.
Return ONLY a number between 0.0 and 1.0, where:
- 0.0 = completely irrelevant
- 1.0 = perfectly relevant

Query: {query}

Passage: {passage}

Relevance score:"""


class PassthroughReranker:
    """No-op reranker: returns results as-is."""

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        return results[:top_k]


class CrossEncoderReranker:
    """Reranks using an external reranking provider (e.g. Cohere Rerank)."""

    def __init__(self, provider: RerankProvider) -> None:
        self._provider = provider

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not results:
            return []

        documents = [r.chunk_text for r in results]
        scored = await self._provider.rerank(query, documents, top_k)

        reranked: list[SearchResult] = []
        for sd in scored:
            original = results[sd.index]
            reranked.append(
                SearchResult(
                    chunk_id=original.chunk_id,
                    document_id=original.document_id,
                    chunk_text=original.chunk_text,
                    section_title=original.section_title,
                    chunk_type=original.chunk_type,
                    score=sd.score,
                    semantic_rank=original.semantic_rank,
                    lexical_rank=original.lexical_rank,
                )
            )
        return reranked


class OllamaReranker:
    """Reranks using Ollama's chat API to score query-document relevance.

    Each candidate is scored with a simple relevance prompt. Results
    are sorted by LLM-assigned relevance score.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        max_concurrent: int = 5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def _score_one(
        self, client: httpx.AsyncClient, query: str, passage: str
    ) -> float:
        """Score a single query-passage pair."""
        async with self._semaphore:
            try:
                prompt = RERANK_PROMPT.format(
                    query=query, passage=passage[:500]
                )
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": f"/no_think\n{prompt}",
                        "stream": False,
                        "options": {"num_predict": 16},
                    },
                )
                resp.raise_for_status()
                text = resp.json().get("response", "").strip()
                # Extract first float from response
                for token in text.split():
                    try:
                        score = float(token)
                        return max(0.0, min(1.0, score))
                    except ValueError:
                        continue
                return 0.5  # Default if no float found
            except Exception:
                logger.debug(
                    "Ollama rerank scoring failed", exc_info=True
                )
                return 0.5

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not results:
            return []

        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = [
                self._score_one(client, query, r.chunk_text)
                for r in results
            ]
            scores = await asyncio.gather(*tasks)

        # Pair results with scores and sort
        scored = sorted(
            zip(results, scores, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )

        return [
            SearchResult(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                chunk_text=r.chunk_text,
                section_title=r.section_title,
                chunk_type=r.chunk_type,
                score=s,
                semantic_rank=r.semantic_rank,
                lexical_rank=r.lexical_rank,
            )
            for r, s in scored[:top_k]
        ]
