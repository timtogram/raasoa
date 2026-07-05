"""Tests for the MCP Policy-Gate (raasoa.mcp.policy).

Covers the clearance-clamp fix (F-005): previously
``arguments.get("agent_clearance") or env_default_clearance()`` let any
caller-supplied value override the server-side ceiling outright — a
request for ``secret`` clearance saw everything regardless of the
configured default. ``effective_clearance`` must clamp a request for a
*higher* clearance down to the ceiling and only allow *lower* requests
through.
"""
from __future__ import annotations

import pytest

from raasoa.mcp.policy import (
    apply_policy_gate,
    effective_clearance,
    hit_is_allowed,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAASOA_MCP_DEFAULT_CLEARANCE", raising=False)


class TestEffectiveClearance:
    def test_no_request_uses_ceiling(self) -> None:
        assert effective_clearance(None) == "public"

    def test_request_below_ceiling_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAASOA_MCP_DEFAULT_CLEARANCE", "confidential")
        assert effective_clearance("public") == "public"
        assert effective_clearance("internal") == "internal"

    def test_request_above_ceiling_is_clamped_down(self) -> None:
        """The exact escalation from F-005: requesting 'secret' when the
        ceiling is the default 'public' must not be honored as-is."""
        assert effective_clearance("secret") == "public"
        assert effective_clearance("confidential") == "public"
        assert effective_clearance("internal") == "public"

    def test_request_equal_to_ceiling_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAASOA_MCP_DEFAULT_CLEARANCE", "internal")
        assert effective_clearance("internal") == "internal"

    def test_unknown_clearance_string_ranks_as_public(self) -> None:
        # An unrecognized value ranks as public (0) — the lowest rank, so
        # it never exceeds any ceiling and passes through unclamped.
        assert effective_clearance("nonsense") == "nonsense"


class TestHitIsAllowed:
    def test_no_classification_defaults_to_public(self) -> None:
        assert hit_is_allowed({}, "public") is True

    def test_matching_classification_allowed(self) -> None:
        assert hit_is_allowed({"doc_metadata": {"classification": "internal"}}, "internal") is True

    def test_exceeding_classification_denied(self) -> None:
        assert hit_is_allowed({"doc_metadata": {"classification": "secret"}}, "public") is False

    def test_reads_metadata_key_too(self) -> None:
        assert hit_is_allowed({"metadata": {"classification": "confidential"}}, "public") is False

    def test_reads_flat_classification_key(self) -> None:
        assert hit_is_allowed({"classification": "restricted"}, "public") is False


class TestApplyPolicyGate:
    def test_splits_allowed_and_denied(self) -> None:
        hits = [
            {"id": "1", "doc_metadata": {"classification": "public"}},
            {"id": "2", "doc_metadata": {"classification": "secret"}},
        ]
        allowed, denied = apply_policy_gate(hits, clearance="public")
        assert [h["id"] for h in allowed] == ["1"]
        assert [h["id"] for h in denied] == ["2"]
        assert "policy_reason" in denied[0]

    def test_empty_hits(self) -> None:
        allowed, denied = apply_policy_gate([], clearance="public")
        assert allowed == []
        assert denied == []
