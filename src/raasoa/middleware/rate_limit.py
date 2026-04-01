"""In-memory sliding-window rate limiter.

Uses a per-tenant token bucket to prevent abuse of expensive endpoints
like ingestion and retrieval. No external dependencies required.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import HTTPException, Request


@dataclass
class _Bucket:
    tokens: list[float] = field(default_factory=list)


class RateLimiter:
    """Sliding-window rate limiter keyed by tenant ID."""

    def __init__(self, requests_per_minute: int) -> None:
        self._rpm = requests_per_minute
        self._window = 60.0  # seconds
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)

    def _cleanup(self, bucket: _Bucket, now: float) -> None:
        cutoff = now - self._window
        bucket.tokens = [t for t in bucket.tokens if t > cutoff]

    def check(self, key: str) -> None:
        """Raise 429 if rate limit exceeded."""
        now = time.monotonic()
        bucket = self._buckets[key]
        self._cleanup(bucket, now)

        if len(bucket.tokens) >= self._rpm:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({self._rpm} requests/minute). Try again later.",
            )
        bucket.tokens.append(now)


# Singleton limiters — created on import, configured from settings
_ingest_limiter: RateLimiter | None = None
_retrieve_limiter: RateLimiter | None = None


def get_ingest_limiter() -> RateLimiter:
    global _ingest_limiter
    if _ingest_limiter is None:
        from raasoa.config import settings
        _ingest_limiter = RateLimiter(settings.ingest_rate_limit_per_minute)
    return _ingest_limiter


def get_retrieve_limiter() -> RateLimiter:
    global _retrieve_limiter
    if _retrieve_limiter is None:
        from raasoa.config import settings
        _retrieve_limiter = RateLimiter(settings.retrieve_rate_limit_per_minute)
    return _retrieve_limiter


def extract_tenant_id(request: Request) -> str:
    """Extract tenant ID from request headers for rate limiting."""
    return request.headers.get(
        "x-tenant-id", "00000000-0000-0000-0000-000000000001"
    )
