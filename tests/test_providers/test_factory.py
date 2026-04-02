"""Tests for provider factory and configuration."""

import pytest

from raasoa.providers.factory import get_embedding_provider


def test_factory_returns_ollama_by_default() -> None:
    """Ollama is the default provider (local-first)."""
    provider = get_embedding_provider()
    assert "ollama" in provider.model_id


def test_factory_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown providers produce clear error messages."""
    from raasoa import config
    monkeypatch.setattr(config.settings, "embedding_provider", "magic-cloud-ai")
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        get_embedding_provider()


def test_factory_openai_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI provider requires API key to be set."""
    from raasoa import config
    monkeypatch.setattr(config.settings, "embedding_provider", "openai")
    monkeypatch.setattr(config.settings, "openai_api_key", "")
    with pytest.raises(ValueError, match="OPENAI_API_KEY not set"):
        get_embedding_provider()


def test_factory_cohere_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cohere provider requires API key to be set."""
    from raasoa import config
    monkeypatch.setattr(config.settings, "embedding_provider", "cohere")
    monkeypatch.setattr(config.settings, "cohere_api_key", "")
    with pytest.raises(ValueError, match="COHERE_API_KEY not set"):
        get_embedding_provider()


def test_openai_provider_detects_azure() -> None:
    """Azure endpoint is detected from base URL."""
    from raasoa.providers.openai import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(
        api_key="test",
        base_url="https://myresource.openai.azure.com",
        model="text-embedding-3-small",
        dimensions=1536,
    )
    assert "azure" in provider.model_id
    assert provider._is_azure is True


def test_openai_provider_standard_url() -> None:
    """Standard OpenAI URL is not detected as Azure."""
    from raasoa.providers.openai import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        dimensions=1536,
    )
    assert "openai" in provider.model_id
    assert provider._is_azure is False


def test_openai_provider_custom_endpoint() -> None:
    """Custom endpoints (vLLM etc.) work with standard OpenAI protocol."""
    from raasoa.providers.openai import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(
        api_key="dummy",
        base_url="http://localhost:8080/v1",
        model="my-local-model",
        dimensions=768,
    )
    assert provider.model_id == "openai/my-local-model"
    assert provider.dimensions == 768
