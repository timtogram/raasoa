"""Provider factory — creates embedding providers with optional cache.

Supported providers:
  - ollama: Local inference via Ollama (default, no data leaves your infra)
  - openai: OpenAI API, Azure OpenAI, or any OpenAI-compatible endpoint
  - cohere: Cohere API or compatible endpoint

Switch via: EMBEDDING_PROVIDER=ollama|openai|cohere

The EmbeddingCache wraps any provider transparently, saving 30-50%
of API calls by caching embeddings for identical texts.
"""

from __future__ import annotations

from typing import Any

from raasoa.config import settings
from raasoa.providers.base import EmbeddingProvider

_SUPPORTED = {"ollama", "openai", "cohere"}

# Singleton cached provider — shared across requests
_cached_provider: Any | None = None


def get_embedding_provider() -> Any:
    """Create an embedding provider with cache.

    Returns EmbeddingCache wrapping the configured provider.
    The cache is shared across all requests (singleton).
    """
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider

    raw_provider = _create_raw_provider()

    from raasoa.providers.cache import EmbeddingCache

    _cached_provider = EmbeddingCache(raw_provider, max_size=10000)
    return _cached_provider


def _create_raw_provider() -> EmbeddingProvider:
    """Create the raw embedding provider (no cache)."""
    provider = settings.embedding_provider.lower()

    if provider == "ollama":
        from raasoa.providers.ollama import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider()

    if provider == "openai":
        from raasoa.providers.openai import OpenAIEmbeddingProvider

        if not settings.openai_api_key:
            msg = (
                "OPENAI_API_KEY not set. Required for provider='openai'. "
                "For Azure: also set OPENAI_BASE_URL. "
                "For local: use EMBEDDING_PROVIDER=ollama."
            )
            raise ValueError(msg)
        return OpenAIEmbeddingProvider()

    if provider == "cohere":
        from raasoa.providers.cohere import CohereEmbeddingProvider

        if not settings.cohere_api_key:
            msg = (
                "COHERE_API_KEY not set. Required for provider='cohere'. "
                "For local: use EMBEDDING_PROVIDER=ollama."
            )
            raise ValueError(msg)
        return CohereEmbeddingProvider()

    supported = ", ".join(sorted(_SUPPORTED))
    raise ValueError(
        f"Unknown embedding provider: '{provider}'. "
        f"Supported: {supported}"
    )
