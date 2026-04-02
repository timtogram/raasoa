"""Provider factory — creates embedding providers from configuration.

Supported providers:
  - ollama: Local inference via Ollama (default, no data leaves your infra)
  - openai: OpenAI API, Azure OpenAI, or any OpenAI-compatible endpoint
  - cohere: Cohere API or compatible endpoint

Switch via: EMBEDDING_PROVIDER=ollama|openai|cohere
"""

from raasoa.config import settings
from raasoa.providers.base import EmbeddingProvider

_SUPPORTED = {"ollama", "openai", "cohere"}


def get_embedding_provider() -> EmbeddingProvider:
    """Create an embedding provider based on the current configuration."""
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
