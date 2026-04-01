from raasoa.config import settings
from raasoa.providers.base import EmbeddingProvider


def get_embedding_provider() -> EmbeddingProvider:
    provider = settings.embedding_provider.lower()

    if provider == "ollama":
        from raasoa.providers.ollama import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider()

    if provider == "openai":
        from raasoa.providers.openai import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider()

    if provider == "cohere":
        from raasoa.providers.cohere import CohereEmbeddingProvider

        return CohereEmbeddingProvider()

    raise ValueError(f"Unknown embedding provider: {provider}")
