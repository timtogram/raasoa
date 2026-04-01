"""Tests for the sliding-window rate limiter."""

import pytest
from fastapi import HTTPException

from raasoa.middleware.rate_limit import RateLimiter


def test_allows_requests_within_limit() -> None:
    limiter = RateLimiter(requests_per_minute=5)
    for _ in range(5):
        limiter.check("tenant-1")  # Should not raise


def test_blocks_requests_over_limit() -> None:
    limiter = RateLimiter(requests_per_minute=3)
    for _ in range(3):
        limiter.check("tenant-1")

    with pytest.raises(HTTPException) as exc_info:
        limiter.check("tenant-1")
    assert exc_info.value.status_code == 429


def test_separate_tenants_have_separate_limits() -> None:
    limiter = RateLimiter(requests_per_minute=2)
    # Tenant A fills up
    limiter.check("tenant-a")
    limiter.check("tenant-a")
    with pytest.raises(HTTPException):
        limiter.check("tenant-a")

    # Tenant B still has capacity
    limiter.check("tenant-b")  # Should not raise


def test_bucket_cleanup_removes_old_entries() -> None:
    """Verify that old tokens are cleaned up (unit test for internal method)."""
    import time

    limiter = RateLimiter(requests_per_minute=2)
    bucket = limiter._buckets["test"]
    # Add tokens in the past (beyond the 60s window)
    bucket.tokens = [time.monotonic() - 120, time.monotonic() - 90]
    limiter._cleanup(bucket, time.monotonic())
    assert len(bucket.tokens) == 0
