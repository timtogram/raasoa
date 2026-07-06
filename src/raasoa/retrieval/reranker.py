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
        try:
            scored = await self._provider.rerank(query, documents, top_k)
        except Exception:
            # A rerank-provider outage (e.g. Cohere API down/rate-limited)
            # must degrade to unreranked results, not 500 /v1/retrieve or
            # /v1/answer — reranking is an enhancement on top of hybrid
            # search's own ranking, not something retrieval depends on to
            # function at all (unlike embeddings, where an outage means
            # honest refusal). Matches OllamaReranker's per-item
            # resilience, just at the whole-batch granularity since
            # cross-encoder providers score in one call.
            #
            # Score is set to a neutral 0.5 (not the original RRF-scale
            # score) because SCORE_SCALE=1.0 is a fixed class attribute
            # read by the caller's compute_confidence() independently of
            # this call — returning an RRF-scale score (~0.033 max) under
            # a 1.0-scale assumption would silently under-report
            # confidence by ~30x instead of giving an honest "degraded,
            # moderate confidence" signal. Order is preserved (a stable
            # sort of all-equal scores keeps hybrid search's own ranking)
            # so this is otherwise identical to PassthroughReranker.
            logger.warning(
                "Rerank provider %s failed; falling back to unreranked "
                "results",
                type(self._provider).__name__,
                exc_info=True,
            )
            return [replace(r, score=0.5) for r in results[:top_k]]

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
        self,
        client: httpx.AsyncClient,
        query: str,
        passage: str,
        failures: list[int],
    ) -> float:
        """Score a single query-passage pair.

        ``failures`` is a shared list used as a mutable counter — every
        exception appends one entry so ``rerank()`` can tell, after all
        items finish, whether this was an isolated glitch (fine to stay
        at debug level, per-item detail matters less) or the whole
        service is down (worth a single aggregate warning instead of one
        debug line per item that operators would never see at INFO+).
        """
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
                failures.append(1)
                return 0.5

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not results:
            return []

        failures: list[int] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = [
                self._score_one(client, query, r.chunk_text, failures)
                for r in results
            ]
            scores = await asyncio.gather(*tasks)

        if failures:
            # Individual scoring failures already degrade gracefully to a
            # neutral 0.5 (see _score_one), so this never breaks the
            # request — but if the Ollama reranker is entirely down, every
            # item silently getting the same neutral score would
            # otherwise be invisible at any log level operators actually
            # watch in production.
            logger.warning(
                "Ollama reranker: %d/%d items failed scoring and fell "
                "back to a neutral score",
                len(failures), len(results),
            )

        # Pair results with scores and sort
        scored = sorted(
            zip(results, scores, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )

        # dataclasses.replace preserves every field (see CrossEncoderReranker
        # for why rebuilding by hand previously dropped source provenance).
        return [replace(r, score=s) for r, s in scored[:top_k]]
