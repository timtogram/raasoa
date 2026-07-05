from dataclasses import dataclass
from typing import Protocol


class EmbeddingProviderUnavailableError(Exception):
    """Raised when an embedding provider's backing service is completely
    unreachable (connection refused/timed out) — as opposed to an
    individual bad request, which providers already retry and degrade
    per-text. Callers should surface this as a clean 503 or an honest
    refusal, never let it bubble up as an unhandled 500."""


@dataclass
class ScoredDocument:
    index: int
    score: float
    text: str


class EmbeddingProvider(Protocol):
    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        """Embed texts. ``input_type`` distinguishes documents being
        indexed ("search_document", the default) from a query being
        searched ("search_query") — only meaningful for providers with
        asymmetric embedding models (Cohere); others ignore it."""
        ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...


class RerankProvider(Protocol):
    async def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[ScoredDocument]: ...
