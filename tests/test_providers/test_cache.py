"""Tests for embedding cache."""

import pytest

from raasoa.providers.cache import EmbeddingCache


class FakeProvider:
    """Mock embedding provider that counts calls."""

    model_id = "fake/test"
    dimensions = 3
    _current_tenant_id: str | None = None
    call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += len(texts)
        return [[float(i), 0.0, 0.0] for i in range(len(texts))]


@pytest.mark.asyncio
async def test_cache_deduplicates() -> None:
    """Same text should only be embedded once."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider, max_size=100)

    # Embed same text twice
    r1 = await cache.embed(["hello world"])
    r2 = await cache.embed(["hello world"])

    assert r1 == r2
    assert provider.call_count == 1  # Only 1 actual API call
    assert cache.stats["hits"] == 1
    assert cache.stats["misses"] == 1


@pytest.mark.asyncio
async def test_cache_different_texts() -> None:
    """Different texts should be embedded separately."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider, max_size=100)

    await cache.embed(["text a", "text b"])
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_cache_mixed_hit_miss() -> None:
    """Mix of cached and uncached texts in one call."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider, max_size=100)

    # First call: both new
    await cache.embed(["cached", "also cached"])
    assert provider.call_count == 2

    # Second call: one cached, one new
    results = await cache.embed(["cached", "brand new"])
    assert len(results) == 2
    assert provider.call_count == 3  # Only 1 new embed call


@pytest.mark.asyncio
async def test_cache_eviction() -> None:
    """LRU eviction when cache is full."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider, max_size=2)

    await cache.embed(["a"])
    await cache.embed(["b"])
    await cache.embed(["c"])  # Should evict "a"

    assert cache.stats["cache_size"] == 2

    # "a" should be evicted, requires re-embed
    old_count = provider.call_count
    await cache.embed(["a"])
    assert provider.call_count == old_count + 1  # Had to re-embed


@pytest.mark.asyncio
async def test_cache_model_id_passthrough() -> None:
    """Cache should forward model_id from wrapped provider."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider)
    assert cache.model_id == "fake/test"
    assert cache.dimensions == 3


@pytest.mark.asyncio
async def test_cache_tenant_tracking() -> None:
    """Cache should forward tenant_id to wrapped provider."""
    provider = FakeProvider()
    cache = EmbeddingCache(provider)
    cache._current_tenant_id = "test-tenant"
    assert provider._current_tenant_id == "test-tenant"
