"""Tests for telemetry module — verifies no-op behavior when OTel not installed."""

from raasoa.telemetry import record_metric, trace_span


def test_trace_span_noop_without_otel() -> None:
    """trace_span should be a no-op when OTel is not configured."""
    with trace_span("test.span", {"key": "value"}) as span:
        assert span is None  # No OTel configured in test env


def test_record_metric_noop_without_otel() -> None:
    """record_metric should silently do nothing without OTel."""
    record_metric("test.counter", 1.0, {"tag": "value"})  # Should not raise
