"""Factory for creating reranker instances based on configuration."""

from raasoa.config import settings
from raasoa.retrieval.reranker import (
    CrossEncoderReranker,
    OllamaReranker,
    PassthroughReranker,
)


def get_reranker() -> PassthroughReranker | OllamaReranker | CrossEncoderReranker:
    """Create a reranker based on the current configuration."""
    reranker_type = settings.reranker.lower()

    if reranker_type == "ollama":
        return OllamaReranker(
            base_url=settings.ollama_base_url,
            model=settings.ollama_chat_model,
        )

    if reranker_type == "cohere":
        # Imported lazily so raasoa.providers.cohere (and its httpx
        # dependency on a Cohere API key being configured) isn't pulled
        # in for the common ollama/passthrough paths.
        from raasoa.providers.cohere import CohereRerankProvider

        return CrossEncoderReranker(CohereRerankProvider())

    # Default: passthrough
    return PassthroughReranker()
