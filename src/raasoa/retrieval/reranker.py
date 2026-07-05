"""Reranking strategies for search results.

- PassthroughReranker: No-op, returns results as-is (default)
- CrossEncoderReranker: Uses an external reranking provider (Cohere, etc.)
- OllamaReranker: Uses Ollama's chat API to score query-document relevance
"""

import asyncio
import logging
from dataclasses import replace

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

    # Confidence scoring needs to know the scale of `.score` to normalize
    # it — RRF (hybrid_search's own ranking, unchanged by this reranker)
    # tops out around 1/(60+1) per signal. See raasoa.retrieval.confidence.
    SCORE_SCALE = 0.033

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        return results[:top_k]


class CrossEncoderReranker:
    """Reranks using an external reranking provider (e.g. Cohere Rerank)."""

    # Relevance scores from cross-encoder rerankers are already in [0, 1].
    SCORE_SCALE = 1.0

    def __init__(self, provider: RerankProvider) -> None:
        self._provider = provider

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not results:
            return []

        documents = [r.chunk_text for r in results]
        scored = await self._provider.rerank(query, documents, top_k)

        # dataclasses.replace preserves every field of `original`
        # (document_title, source_url, doc_metadata, etc.) — rebuilding
        # SearchResult by hand here previously dropped 7 of them, so
        # every /v1/answer citation and /v1/retrieve hit lost its
        # provenance under a non-default reranker.
        return [
            replace(results[sd.index], score=sd.score) for sd in scored
        ]


class OllamaReranker:
    """Reranks using Ollama's chat API to score query-document relevance.

    Each candidate is scored with a simple relevance prompt. Results
    are sorted by LLM-assigned relevance score.
    """

    # _score_one always clamps to [0, 1] (or defaults to 0.5 on failure).
    SCORE_SCALE = 1.0

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
                raw = resp.json().get("response", "").strip()
                # Strip thinking tags
                import re as _re
                raw = _re.sub(
                    r"<think>.*?</think>", "", raw, flags=_re.DOTALL,
                ).strip()
                # Extract any float (handles "Score: 0.85", "0.7", etc.)
                match = _re.search(r"(\d+\.?\d*)", raw)
                if match:
                    score = float(match.group(1))
                    return max(0.0, min(1.0, score))
                return 0.5
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

        # dataclasses.replace preserves every field (see CrossEncoderReranker
        # for why rebuilding by hand previously dropped source provenance).
        return [replace(r, score=s) for r, s in scored[:top_k]]
