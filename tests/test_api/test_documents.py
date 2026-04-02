"""Tests for document API cursor-based pagination helpers."""


from raasoa.api.documents import _decode_cursor, _encode_cursor


def test_cursor_encode_decode_roundtrip() -> None:
    ts = "2026-04-01T12:00:00+00:00"
    doc_id = "12345678-1234-1234-1234-123456789abc"
    cursor = _encode_cursor(ts, doc_id)
    decoded_ts, decoded_id = _decode_cursor(cursor)
    assert decoded_ts == ts
    assert decoded_id == doc_id


def test_cursor_is_url_safe_base64() -> None:
    cursor = _encode_cursor("2026-01-01T00:00:00+00:00", "abc-123")
    # Should not contain +, /, or =
    # urlsafe_b64 uses - and _ instead
    assert "+" not in cursor.rstrip("=")
    assert "/" not in cursor.rstrip("=")
