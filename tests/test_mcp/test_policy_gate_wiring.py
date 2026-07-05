"""Tests that the MCP policy-gate is actually wired into the tools it
claims to protect (F-006), the audit call hits the real endpoint
(F-007), and two previously-broken tools now behave correctly (F-029,
F-043).

Uses httpx.MockTransport to fake the backend REST API — no live server,
no network, no new test dependency (MockTransport ships in httpx, an
existing dependency). raasoa.mcp.server and raasoa.mcp.policy each do
``import httpx`` and call ``httpx.AsyncClient(...)`` directly, so
patching the shared ``httpx.AsyncClient`` attribute covers both call
sites (the tool-call client in server.py and the audit-log client in
policy.py).
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import raasoa.mcp.server as server

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _json_response(status_code: int, body: Any) -> httpx.Response:
    return httpx.Response(status_code, json=body)


class FakeBackend:
    """Records every request it receives and replays canned responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}

    def on(self, method: str, path: str, response: httpx.Response) -> None:
        self.responses[(method.upper(), path)] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.responses:
            return self.responses[key]
        return httpx.Response(404, json={"detail": "not found in fake backend"})


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fb = FakeBackend()
    transport = httpx.MockTransport(fb.handler)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.setattr(server, "BASE_URL", "http://backend.test")
    monkeypatch.delenv("RAASOA_MCP_DEFAULT_CLEARANCE", raising=False)
    return fb


async def _audit_call_bodies(fb: FakeBackend) -> list[dict[str, Any]]:
    return [
        json.loads(r.content)
        for r in fb.requests
        if r.method == "POST" and r.url.path == "/v1/analytics/audit"
    ]


class TestListDocumentsGate:
    async def test_filters_over_clearance_documents(self, backend: FakeBackend) -> None:
        backend.on("GET", "/v1/documents", _json_response(200, {
            "items": [
                {"id": "1", "title": "Public Doc", "status": "indexed",
                 "chunk_count": 1, "doc_metadata": {}},
                {"id": "2", "title": "Secret Doc", "status": "indexed",
                 "chunk_count": 1, "doc_metadata": {"classification": "secret"}},
            ],
        }))
        result = await server._handle_tool_call("raasoa_list_documents", {"limit": 20})
        text = result[0]["text"]
        assert "Public Doc" in text
        assert "Secret Doc" not in text
        assert "1 documents" in text

        audits = await _audit_call_bodies(backend)
        assert len(audits) == 1
        assert audits[0]["action"] == "mcp.policy_denied"
        assert audits[0]["details"]["tool"] == "raasoa_list_documents"
        assert audits[0]["details"]["doc_ids"] == ["2"]

    async def test_higher_clearance_request_is_clamped(self, backend: FakeBackend) -> None:
        """Requesting 'secret' with no server-side ceiling override must
        still be clamped to the 'public' default (F-005)."""
        backend.on("GET", "/v1/documents", _json_response(200, {
            "items": [
                {"id": "1", "title": "Internal Doc", "status": "indexed",
                 "chunk_count": 1, "doc_metadata": {"classification": "internal"}},
            ],
        }))
        result = await server._handle_tool_call(
            "raasoa_list_documents", {"limit": 20, "agent_clearance": "secret"},
        )
        assert "No documents" in result[0]["text"]


class TestGetDocumentGate:
    async def test_denies_over_clearance_document(self, backend: FakeBackend) -> None:
        backend.on("GET", "/v1/documents/doc-1", _json_response(200, {
            "id": "doc-1", "title": "Confidential Plan", "status": "indexed",
            "chunk_count": 1, "version": 1, "doc_metadata": {"classification": "confidential"},
            "chunks": [],
        }))
        result = await server._handle_tool_call(
            "raasoa_get_document", {"document_id": "doc-1"},
        )
        text = result[0]["text"]
        assert "access denied" in text
        assert "Confidential Plan" in text
        # The full chunk content must never appear in a denial.
        audits = await _audit_call_bodies(backend)
        assert len(audits) == 1
        assert audits[0]["details"]["tool"] == "raasoa_get_document"

    async def test_allows_matching_clearance_document(self, backend: FakeBackend) -> None:
        backend.on("GET", "/v1/documents/doc-2", _json_response(200, {
            "id": "doc-2", "title": "Public Handbook", "status": "indexed",
            "chunk_count": 0, "version": 1, "doc_metadata": {},
            "chunks": [],
        }))
        result = await server._handle_tool_call(
            "raasoa_get_document", {"document_id": "doc-2"},
        )
        text = result[0]["text"]
        assert "access denied" not in text
        assert "Public Handbook" in text


class TestAnswerGate:
    async def test_refuses_when_a_citation_exceeds_clearance(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("POST", "/v1/answer", _json_response(200, {
            "query": "q", "answered": True, "answer": "The answer is X [1].",
            "citations": [
                {"n": 1, "document_id": "d1", "document_title": "Secret Memo",
                 "chunk_id": "c1", "quote": "X", "doc_metadata": {"classification": "secret"}},
            ],
            "confidence": {"retrieval_confidence": 0.9, "source_count": 1,
                            "top_score": 0.03, "answerable": True},
        }))
        result = await server._handle_tool_call("raasoa_answer", {"query": "q"})
        text = result[0]["text"]
        assert "cannot be returned" in text
        assert "Secret Memo" not in text
        assert "The answer is X" not in text

        audits = await _audit_call_bodies(backend)
        assert len(audits) == 1
        assert audits[0]["details"]["tool"] == "raasoa_answer"

    async def test_answers_normally_when_all_citations_allowed(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("POST", "/v1/answer", _json_response(200, {
            "query": "q", "answered": True, "answer": "The answer is X [1].",
            "citations": [
                {"n": 1, "document_id": "d1", "document_title": "Public Handbook",
                 "chunk_id": "c1", "quote": "X", "doc_metadata": {}},
            ],
            "confidence": {"retrieval_confidence": 0.9, "source_count": 1,
                            "top_score": 0.03, "answerable": True},
        }))
        result = await server._handle_tool_call("raasoa_answer", {"query": "q"})
        text = result[0]["text"]
        assert "The answer is X" in text
        assert "Public Handbook" in text


class TestDocDependenciesAndDiffGate:
    async def test_doc_dependencies_denied_for_over_clearance_root(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("GET", "/v1/documents/doc-3", _json_response(200, {
            "id": "doc-3", "title": "Restricted Root", "status": "indexed",
            "chunk_count": 1, "version": 1,
            "doc_metadata": {"classification": "restricted"}, "chunks": [],
        }))
        # Dependencies endpoint should never even be reached.
        backend.on("GET", "/v1/documents/doc-3/dependencies", _json_response(
            200, {"title": "Restricted Root", "dependencies": {"total": 5}},
        ))
        result = await server._handle_tool_call(
            "raasoa_doc_dependencies", {"document_id": "doc-3"},
        )
        text = result[0]["text"]
        assert "access denied" in text
        assert not any(
            r.url.path == "/v1/documents/doc-3/dependencies" for r in backend.requests
        )

    async def test_doc_diff_denied_for_over_clearance_root(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("GET", "/v1/documents/doc-4", _json_response(200, {
            "id": "doc-4", "title": "Restricted Root 2", "status": "indexed",
            "chunk_count": 1, "version": 2,
            "doc_metadata": {"classification": "confidential"}, "chunks": [],
        }))
        backend.on("GET", "/v1/documents/doc-4/diff", _json_response(
            200, {"title": "Restricted Root 2", "claim_changes": []},
        ))
        result = await server._handle_tool_call(
            "raasoa_doc_diff", {"document_id": "doc-4"},
        )
        text = result[0]["text"]
        assert "access denied" in text
        assert not any(
            r.url.path == "/v1/documents/doc-4/diff" for r in backend.requests
        )

    async def test_doc_dependencies_allowed_for_matching_clearance(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("GET", "/v1/documents/doc-5", _json_response(200, {
            "id": "doc-5", "title": "Open Doc", "status": "indexed",
            "chunk_count": 1, "version": 1, "doc_metadata": {}, "chunks": [],
        }))
        backend.on("GET", "/v1/documents/doc-5/dependencies", _json_response(200, {
            "title": "Open Doc", "dependencies": {"total": 0},
        }))
        result = await server._handle_tool_call(
            "raasoa_doc_dependencies", {"document_id": "doc-5"},
        )
        text = result[0]["text"]
        assert "access denied" not in text
        assert "Open Doc" in text


class TestFindByMetadataFix:
    """F-029: the tool used to ignore its `metadata` argument entirely
    and list unfiltered documents. It must now call the real
    server-side filter endpoint."""

    async def test_calls_real_find_by_metadata_endpoint(
        self, backend: FakeBackend,
    ) -> None:
        backend.on("POST", "/v1/find_by_metadata", _json_response(200, {
            "documents": [
                {"id": "1", "title": "Policy A", "quality_score": 0.9,
                 "status": "indexed", "doc_metadata": {"type": "policy"}},
            ],
            "matched_filter": {"type": "policy"},
        }))
        result = await server._handle_tool_call(
            "raasoa_find_by_metadata", {"metadata": {"type": "policy"}},
        )
        text = result[0]["text"]
        assert "Policy A" in text

        find_calls = [
            r for r in backend.requests
            if r.method == "POST" and r.url.path == "/v1/find_by_metadata"
        ]
        assert len(find_calls) == 1
        assert json.loads(find_calls[0].content)["metadata"] == {"type": "policy"}

    async def test_filters_over_clearance_matches(self, backend: FakeBackend) -> None:
        backend.on("POST", "/v1/find_by_metadata", _json_response(200, {
            "documents": [
                {"id": "1", "title": "Public Policy", "quality_score": 0.9,
                 "status": "indexed", "doc_metadata": {}},
                {"id": "2", "title": "Secret Policy", "quality_score": 0.9,
                 "status": "indexed", "doc_metadata": {"classification": "secret"}},
            ],
            "matched_filter": {},
        }))
        result = await server._handle_tool_call(
            "raasoa_find_by_metadata", {"metadata": {}},
        )
        text = result[0]["text"]
        assert "Public Policy" in text
        assert "Secret Policy" not in text


class TestQualityReportAuthHeader:
    """F-043: the quality-report tool was the only call site missing
    headers=_headers(), so it always 401'd under AUTH_ENABLED=true."""

    async def test_sends_authorization_header(
        self, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(server, "_request_api_key", server._request_api_key)
        token = server._request_api_key.set("sk-test-key")
        try:
            backend.on("GET", "/v1/documents/doc-9/quality", _json_response(200, {
                "title": "Doc", "quality_score": 0.9, "review_status": "published",
                "findings": [],
            }))
            await server._handle_tool_call(
                "raasoa_quality_report", {"document_id": "doc-9"},
            )
            calls = [
                r for r in backend.requests
                if r.url.path == "/v1/documents/doc-9/quality"
            ]
            assert len(calls) == 1
            assert calls[0].headers.get("authorization") == "Bearer sk-test-key"
        finally:
            server._request_api_key.reset(token)
