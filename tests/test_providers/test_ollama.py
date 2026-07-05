"""Tests for the Ollama embedding provider's outage handling (F-008).

Before this fix, only httpx.HTTPStatusError was caught, so a total
connection failure (Ollama container down) bypassed all retries and the
zero-vector fallback and propagated as an unhandled exception into
/v1/retrieve and /v1/answer, which had no try/except around it either —
a total outage 500'd instead of degrading or refusing.
"""
from __future__ import annotations

import httpx
import pytest

from raasoa.providers.base import EmbeddingProviderUnavailableError
from raasoa.providers.ollama import MAX_RETRIES, OllamaEmbeddingProvider


def _connect_error_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def _flaky_then_ok_transport(fail_times: int) -> httpx.MockTransport:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    return httpx.MockTransport(handler)


def _http_500_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    # Don't actually sleep through the retry backoff in tests.
    import raasoa.providers.ollama as ollama_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ollama_mod.asyncio, "sleep", _no_sleep)


class TestConnectionOutage:
    async def test_total_outage_raises_unavailable_not_generic_exception(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = OllamaEmbeddingProvider(
            base_url="http://ollama.test", model="nomic-embed-text", dimensions=2,
        )
        transport = _connect_error_transport()

        real_client = httpx.AsyncClient

        def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = transport  # type: ignore[assignment]
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

        with pytest.raises(EmbeddingProviderUnavailableError):
            await provider.embed(["hello world"])

    async def test_transient_outage_recovers_after_retry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blip that clears within the retry budget must not raise —
        this proves the fix doesn't make retries pointless."""
        provider = OllamaEmbeddingProvider(
            base_url="http://ollama.test", model="nomic-embed-text", dimensions=2,
        )
        transport = _flaky_then_ok_transport(fail_times=MAX_RETRIES - 1)

        real_client = httpx.AsyncClient

        def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = transport  # type: ignore[assignment]
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

        result = await provider.embed(["hello world"])
        assert result == [[0.1, 0.2]]

    async def test_http_status_errors_still_degrade_to_zero_vector(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unchanged behavior: a per-request HTTP error (Ollama up but
        rejecting this request) still falls back to a zero vector rather
        than raising — only a total connection outage should raise."""
        provider = OllamaEmbeddingProvider(
            base_url="http://ollama.test", model="nomic-embed-text", dimensions=3,
        )
        transport = _http_500_transport()

        real_client = httpx.AsyncClient

        def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = transport  # type: ignore[assignment]
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

        result = await provider.embed(["hello world"])
        assert result == [[0.0, 0.0, 0.0]]
