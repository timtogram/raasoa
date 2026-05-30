"""Deterministic tests for the claim-response parser.

These exercise the messy real-world LLM output shapes (qwen3 thinking
blocks, markdown fences, truncated arrays) without needing a model.
"""
from raasoa.quality.claims import parse_claim_response

_VALID = (
    '[{"subject": "meal allowance", "predicate": "amount per day", '
    '"object_value": "28 EUR", "confidence": 0.9}]'
)


def test_plain_json_array() -> None:
    claims = parse_claim_response(_VALID)
    assert len(claims) == 1
    assert claims[0]["subject"] == "meal allowance"
    assert claims[0]["object_value"] == "28 EUR"
    assert claims[0]["confidence"] == 0.9


def test_closed_think_block_is_stripped() -> None:
    raw = f"<think>Let me reason about this carefully...</think>\n{_VALID}"
    claims = parse_claim_response(raw)
    assert len(claims) == 1
    assert claims[0]["predicate"] == "amount per day"


def test_unclosed_think_block_then_json() -> None:
    # qwen3:4b failure mode: opens <think>, never closes, then emits JSON.
    raw = f"<think>Okay, the text says the allowance is... {_VALID}"
    claims = parse_claim_response(raw)
    assert len(claims) == 1
    assert claims[0]["object_value"] == "28 EUR"


def test_unclosed_think_block_no_json_returns_empty() -> None:
    # Model spent its whole budget thinking and never produced JSON.
    raw = "<think>Hmm, let me think about what counts as a claim here..."
    assert parse_claim_response(raw) == []


def test_markdown_json_fence() -> None:
    raw = f"Here are the claims:\n```json\n{_VALID}\n```\nDone."
    claims = parse_claim_response(raw)
    assert len(claims) == 1


def test_truncated_array_is_repaired() -> None:
    # num_predict cut the array off mid-stream after one complete object.
    raw = (
        '[{"subject": "a", "predicate": "p", "object_value": "v"}, '
        '{"subject": "b", "predicate": "q", "object_v'
    )
    claims = parse_claim_response(raw)
    assert len(claims) == 1
    assert claims[0]["subject"] == "a"


def test_missing_required_fields_filtered() -> None:
    raw = '[{"subject": "a"}, {"subject": "b", "predicate": "p", "object_value": "v"}]'
    claims = parse_claim_response(raw)
    assert len(claims) == 1
    assert claims[0]["subject"] == "b"


def test_garbage_returns_empty() -> None:
    assert parse_claim_response("I could not find any claims.") == []
    assert parse_claim_response("") == []
    assert parse_claim_response("<think>only thinking, no answer") == []


def test_confidence_defaults_when_absent() -> None:
    raw = '[{"subject": "a", "predicate": "p", "object_value": "v"}]'
    claims = parse_claim_response(raw)
    assert claims[0]["confidence"] == 0.5
