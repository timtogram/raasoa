from dataclasses import dataclass
from typing import Protocol


@dataclass
class ScoredDocument:
    index: int
    score: float
    text: str


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...


class RerankProvider(Protocol):
    async def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[ScoredDocument]: ...
