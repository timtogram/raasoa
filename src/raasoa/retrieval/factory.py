"""Factory for creating reranker instances based on configuration."""

from raasoa.config import settings
from raasoa.retrieval.reranker import (
    OllamaReranker,
    PassthroughReranker,
)


def get_reranker() -> PassthroughReranker | OllamaReranker:
    """Create a reranker based on the current configuration."""
    reranker_type = settings.reranker.lower()

    if reranker_type == "ollama":
        return OllamaReranker(
            base_url=settings.ollama_base_url,
            model=settings.ollama_chat_model,
        )

    # Default: passthrough
    return PassthroughReranker()
