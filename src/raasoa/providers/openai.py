"""OpenAI-compatible embedding provider.

Supports:
- OpenAI API (api.openai.com)
- Azure OpenAI (*.openai.azure.com)
- Any OpenAI-compatible endpoint (vLLM, LiteLLM, LocalAI, Ollama in OpenAI mode)

Configuration:
  EMBEDDING_PROVIDER=openai
  OPENAI_API_KEY=sk-xxx
  OPENAI_EMBEDDING_MODEL=text-embedding-3-small

For Azure:
  EMBEDDING_PROVIDER=openai
  OPENAI_BASE_URL=https://your-resource.openai.azure.com
  OPENAI_API_KEY=your-azure-key
  OPENAI_API_VERSION=2024-02-01
  OPENAI_EMBEDDING_MODEL=text-embedding-3-small

For custom endpoints (vLLM, LiteLLM, etc.):
  EMBEDDING_PROVIDER=openai
  OPENAI_BASE_URL=http://localhost:8080/v1
  OPENAI_API_KEY=dummy
  OPENAI_EMBEDDING_MODEL=your-model
"""

import logging
from typing import Any

import httpx

from raasoa.config import settings

logger = logging.getLogger(__name__)


class OpenAIEmbeddingProvider:
    """Embedding provider for OpenAI and all OpenAI-compatible APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        api_version: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._base_url = (base_url or settings.openai_base_url).rstrip("/")
        self._model = model or settings.openai_embedding_model
        self._dimensions = dimensions or settings.embedding_dimensions
        self._api_version = api_version or settings.openai_api_version
        self._is_azure = "azure" in self._base_url.lower()

    @property
    def model_id(self) -> str:
        prefix = "azure" if self._is_azure else "openai"
        return f"{prefix}/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _build_url(self) -> str:
        """Build the embeddings API URL (differs for Azure vs standard)."""
        if self._is_azure:
            return (
                f"{self._base_url}/openai/deployments/{self._model}"
                f"/embeddings?api-version={self._api_version}"
            )
        return f"{self._base_url}/embeddings"

    def _build_headers(self) -> dict[str, str]:
        """Build auth headers (differs for Azure vs standard)."""
        if self._is_azure:
            return {"api-key": self._api_key}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the configured OpenAI-compatible endpoint."""
        if not texts:
            return []

        url = self._build_url()
        headers = self._build_headers()

        body: dict[str, Any] = {"input": texts}
        if not self._is_azure:
            body["model"] = self._model
        # Only send dimensions if not Azure (Azure uses deployment config)
        if not self._is_azure and self._dimensions:
            body["dimensions"] = self._dimensions

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
