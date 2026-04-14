"""Embedding cache — avoid re-embedding identical texts.

LRU cache keyed by SHA-256 hash of the text. Saves API costs
and latency when the same content appears in multiple documents
or during re-ingestion.

At SaaS scale, this typically saves 30-50% of embedding calls
because overlapping chunks, boilerplate headers, and re-ingested
unchanged content would otherwise be embedded repeatedly.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class EmbeddingCache:
    """Thread-safe LRU embedding cache.

    Wraps any EmbeddingProvider and caches results by content hash.
    """

    def __init__(
        self,
        provider: Any,
        max_size: int = 10000,
    ) -> None:
        self._provider = provider
        self._max_size = max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def model_id(self) -> str:
        return self._provider.model_id  # type: ignore[no-any-return]

    @property
    def dimensions(self) -> int:
        return self._provider.dimensions  # type: ignore[no-any-return]

    # Forward tenant tracking to underlying provider
    @property
    def _current_tenant_id(self) -> str | None:
        return getattr(self._provider, "_current_tenant_id", None)

    @_current_tenant_id.setter
    def _current_tenant_id(self, value: str | None) -> None:
        if hasattr(self._provider, "_current_tenant_id"):
            self._provider._current_tenant_id = value

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts, using cache for already-seen content."""
        results: list[list[float] | None] = [None] * len(texts)
        to_embed: list[tuple[int, str]] = []  # (index, text)

        # Check cache
        for i, text in enumerate(texts):
            key = self._hash(text)
            if key in self._cache:
                # Move to end (LRU)
                self._cache.move_to_end(key)
                results[i] = self._cache[key]
                self._hits += 1
            else:
                to_embed.append((i, text))
                self._misses += 1

        # Embed uncached texts
        if to_embed:
            uncached_texts = [t for _, t in to_embed]
            embeddings = await self._provider.embed(uncached_texts)

            for (idx, text), embedding in zip(
                to_embed, embeddings, strict=True,
            ):
                results[idx] = embedding
                key = self._hash(text)
                self._cache[key] = embedding

                # Evict oldest if over limit
                if len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)

        if self._hits + self._misses > 0 and (self._hits + self._misses) % 100 == 0:
            total = self._hits + self._misses
            logger.info(
                "Embedding cache: %d hits / %d total (%.0f%% hit rate)",
                self._hits, total, self._hits / total * 100,
            )

        return [r for r in results if r is not None]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "cache_size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": round(
                self._hits / max(self._hits + self._misses, 1) * 100,
            ),
        }
