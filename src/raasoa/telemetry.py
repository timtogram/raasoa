"""OpenTelemetry instrumentation for RAASOA.

Provides tracing for ingestion, retrieval, and quality checks.
Requires the `opentelemetry` optional dependency group.

Usage:
    # Install with tracing support
    uv sync --extra tracing

    # Enable via environment
    OTEL_ENABLED=true
    OTEL_SERVICE_NAME=raasoa
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
"""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Flag to track whether OTel is available and configured
_otel_available = False
_tracer = None


def _init_otel() -> None:
    """Initialize OpenTelemetry if available and enabled."""
    global _otel_available, _tracer

    if os.environ.get("OTEL_ENABLED", "").lower() not in ("true", "1", "yes"):
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "raasoa"),
            "service.version": "0.2.0",
        })

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _tracer = trace.get_tracer("raasoa")
        _otel_available = True
        logger.info("OpenTelemetry initialized")

    except ImportError:
        logger.debug("OpenTelemetry not installed — tracing disabled")
    except Exception:
        logger.warning("OpenTelemetry init failed", exc_info=True)


# Initialize on module load
_init_otel()


@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Generator[Any, None, None]:
    """Create a trace span if OTel is available, otherwise no-op.

    Usage:
        with trace_span("ingestion.parse", {"filename": "doc.pdf"}):
            parsed = parse_file(data, filename)
    """
    if _otel_available and _tracer:
        with _tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span
    else:
        yield None


def record_metric(name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
    """Record a metric if OTel is available."""
    if not _otel_available:
        return

    try:
        from opentelemetry import metrics

        meter = metrics.get_meter("raasoa")
        counter = meter.create_counter(name)
        counter.add(value, attributes or {})
    except Exception:
        pass  # Metrics are best-effort
