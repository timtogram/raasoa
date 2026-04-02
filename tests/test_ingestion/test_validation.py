"""Tests for source-level data contract validation."""

from raasoa.ingestion.validation import validate_webhook_payload


def test_valid_content_passes() -> None:
    result = validate_webhook_payload(
        source="notion",
        content="This is a substantial piece of content " * 5,
        metadata={},
    )
    assert result.valid


def test_empty_content_rejected() -> None:
    result = validate_webhook_payload(
        source="notion", content="", metadata={},
    )
    assert not result.valid
    assert "Empty" in (result.reason or "")


def test_none_content_rejected() -> None:
    result = validate_webhook_payload(
        source="notion", content=None, metadata={},
    )
    assert not result.valid


def test_short_content_rejected() -> None:
    result = validate_webhook_payload(
        source="notion", content="Hi", metadata={},
    )
    assert not result.valid
    assert "too short" in (result.reason or "")


def test_lorem_ipsum_blocked() -> None:
    result = validate_webhook_payload(
        source="custom",
        content="Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
        metadata={},
    )
    assert not result.valid
    assert "blocklist" in (result.reason or "")


def test_auto_generated_blocked() -> None:
    result = validate_webhook_payload(
        source="confluence",
        content="This page is auto-generated and should not be edited manually.",
        metadata={},
    )
    assert not result.valid
