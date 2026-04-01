from raasoa.providers.base import RerankProvider
from raasoa.retrieval.hybrid_search import SearchResult


class PassthroughReranker:
    """No-op reranker: returns results as-is. Placeholder for cross-encoder."""

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
