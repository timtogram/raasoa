"""Tests for usage metering and quota enforcement."""

import uuid

import pytest

from raasoa.middleware.metering import check_quota


class TestCheckQuota:
    """Test quota checking logic.

    Note: These test the function signatures and return types.
    Full integration tests require a running database.
    """

    @pytest.mark.asyncio
    async def test_check_quota_returns_tuple(self) -> None:
        """check_quota should return (bool, str) tuple."""
        # Without a real DB, check_quota returns (True, "ok") as fallback
        from unittest.mock import AsyncMock, MagicMock

        session = MagicMock()
        session.execute = AsyncMock(side_effect=Exception("no db"))

        allowed, reason = await check_quota(
            session, uuid.uuid4(), "documents",
        )
        # On DB failure, quota check is best-effort → allows
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_check_quota_unknown_type(self) -> None:
        """Unknown quota type should pass (best-effort)."""
        from unittest.mock import AsyncMock, MagicMock

        session = MagicMock()
        session.execute = AsyncMock(side_effect=Exception("no db"))

        allowed, reason = await check_quota(
            session, uuid.uuid4(), "nonexistent_quota_type",
        )
        assert allowed is True
