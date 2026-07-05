import asyncio

import httpx

from raasoa.config import settings
from raasoa.providers.base import EmbeddingProviderUnavailableError

BATCH_SIZE = 5
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class OllamaEmbeddingProvider:
    def __init__(
        self,
        base_url: str = settings.ollama_base_url,
        model: str = settings.ollama_embedding_model,
        dimensions: int = settings.embedding_dimensions,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"ollama/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _embed_batch(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> list[list[float]]:
        """Embed a batch with retries. Falls back to one-by-one on failure."""
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self._model, "input": texts},
                )
                response.raise_for_status()
                result: list[list[float]] = response.json()["embeddings"]
                return result
            except httpx.HTTPStatusError:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * 2**attempt)
                    continue
                # Last resort: embed one by one
                return await self._embed_one_by_one(client, texts)
            except httpx.TransportError:
                # Connection refused/timed out — Ollama itself is
                # unreachable, not just this one request. Retrying the
                # exact same broken connection a few times is still
                # worthwhile (transient blips), but exhausting retries
                # here means a systemic outage: don't silently degrade
                # the whole batch to zero vectors (that would corrupt
                # search/embeddings for every text without anyone
                # noticing) — raise so the caller can refuse cleanly.
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * 2**attempt)
                    continue
                raise EmbeddingProviderUnavailableError(
                    f"Ollama unreachable at {self._base_url}"
                ) from None

        return await self._embed_one_by_one(client, texts)

    async def _embed_one_by_one(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> list[list[float]]:
        """Fallback: embed texts individually."""
        results: list[list[float]] = []
        for text in texts:
            for attempt in range(MAX_RETRIES):
                try:
                    response = await client.post(
                        f"{self._base_url}/api/embed",
                        json={"model": self._model, "input": [text]},
                    )
                    response.raise_for_status()
                    results.extend(response.json()["embeddings"])
                    break
                except httpx.HTTPStatusError:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * 2**attempt)
                    else:
                        import logging

                        logging.getLogger(__name__).warning(
                            "Embedding failed after %d retries for text: %s...",
                            MAX_RETRIES, text[:80],
                        )
                        # Zero vector fallback — tracked by quality gate
                        results.append([0.0] * self._dimensions)
                except httpx.TransportError:
                    # Same reasoning as _embed_batch: a total outage must
                    # not degrade to zero vectors — raise instead.
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * 2**attempt)
                    else:
                        raise EmbeddingProviderUnavailableError(
                            f"Ollama unreachable at {self._base_url}"
                        ) from None
        return results

    # Track total calls for metering (set by pipeline before calling embed)
    _current_tenant_id: str | None = None

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        # input_type distinguishes asymmetric embedding models (Cohere);
        # Ollama's embedding models don't have that concept, so it's
        # accepted for interface compatibility with EmbeddingProvider and
        # otherwise unused.
        del input_type
        all_embeddings: list[list[float]] = []
        texts = [t[:8000] if len(t) > 8000 else t for t in texts]

        async with httpx.AsyncClient(timeout=300.0) as client:
            for i in range(0, len(texts), BATCH_SIZE):
                batch = texts[i : i + BATCH_SIZE]
                embeddings = await self._embed_batch(client, batch)
                all_embeddings.extend(embeddings)

        # Metering: track embedding API calls (best-effort)
        if self._current_tenant_id:
            try:
                import uuid

                from raasoa.db import async_session
                from raasoa.middleware.metering import track_usage

                async with async_session() as session:
                    await track_usage(
                        session,
                        uuid.UUID(self._current_tenant_id),
                        "embedding_call",
                        len(texts),
                        {"model": self._model},
                    )
                    await session.commit()
            except Exception:
                pass  # Best-effort

        return all_embeddings
