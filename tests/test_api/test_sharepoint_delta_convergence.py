"""Regression tests for the SharePoint delta-sync stale-cursor bug.

Before this fix, ``_sync_sharepoint_delta_drive`` checked the per-call
``limit`` INSIDE the items loop (mid-page) and, on hitting it, returned the
original ``cursor_url`` argument completely unchanged — not the page it
had actually advanced to. Since Graph's delta feed can only resume from a
real page boundary (``@odata.nextLink``/``@odata.deltaLink``), returning
the untouched input meant the next call always restarted from the exact
same position: any drive whose backlog spans more items than fit in one
call's ``limit`` never advances past that point, on any subsequent call,
ever — a permanent stall, not a slow convergence.

The fix moves the limit check to BETWEEN pages (before fetching a new
one), so every returned cursor is a real Graph-provided pointer and two
consecutive calls make genuine forward progress through a backlog larger
than one call's budget.

These tests mock the Graph HTTP boundary (``client.get``) and
``_ingest_sharepoint_item`` (to isolate pagination/cursor logic from the
ingestion pipeline) but exercise the real ``_sync_sharepoint_delta_drive``
function directly — unlike the existing fairness test, which mocks that
function out entirely and therefore cannot catch this bug.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

from raasoa.api.sources import _sync_sharepoint_delta_drive


def _page_response(
    items: list[dict[str, Any]], next_link: str | None, delta_link: str | None,
) -> Any:
    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            body: dict[str, Any] = {"value": items}
            if next_link:
                body["@odata.nextLink"] = next_link
            if delta_link:
                body["@odata.deltaLink"] = delta_link
            return body

    return _Resp()


def _file_item(item_id: str) -> dict[str, Any]:
    return {"id": item_id, "name": f"{item_id}.txt", "file": {}}


class TestDeltaDriveConvergence:
    async def test_hitting_limit_mid_page_returns_a_real_resumable_url(self) -> None:
        """Page 1 has 20 items; limit is 5, so the limit is hit mid-page.
        The old bug returned `cursor_url` unchanged (here: None, the
        drive's very first sync). The fix must return a URL Graph can
        actually resume from -- i.e. NOT None, and specifically the page
        we're currently working through (its own URL), not a hand-rolled
        position."""
        page1_items = [_file_item(f"item-{i}") for i in range(20)]
        page1_url = "https://graph.microsoft.com/v1.0/drives/d1/root/delta"

        get_calls: list[str] = []

        async def _fake_get(url: str, **kwargs: Any) -> Any:
            get_calls.append(url)
            return _page_response(page1_items, next_link="https://graph/page2", delta_link=None)

        client = AsyncMock()
        client.get = _fake_get

        stats: dict[str, Any] = {"found": 0, "synced": 0, "deleted": 0, "delta_complete": True}

        with patch(
            "raasoa.api.sources._ingest_sharepoint_item",
            new=AsyncMock(side_effect=lambda **kw: kw["stats"].__setitem__(
                "synced", kw["stats"]["synced"] + 1,
            )),
        ):
            result = await _sync_sharepoint_delta_drive(
                session=AsyncMock(),
                tenant_id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                client=client,
                headers={},
                site_id="site-1",
                drive={"id": "d1", "name": "Drive 1"},
                cursor_url=None,
                limit=5,
                sync_acl=False,
                stats=stats,
            )

        # Only 1 page ever fetched: the limit check happens BEFORE
        # fetching page 2, so a second HTTP call never even goes out.
        assert len(get_calls) == 1
        assert get_calls[0] == page1_url
        # ALL 20 items on the page get processed (limit only gates
        # between-page decisions, not mid-page) -- this is the correctness
        # trade-off the fix makes: possible overshoot of `limit` in
        # exchange for every returned cursor being real and resumable.
        assert stats["synced"] == 20
        assert stats["delta_complete"] is False
        # The critical assertion: the returned cursor must be the actual
        # NEXT page link, not None/unchanged. The pre-fix code returned
        # `cursor_url` here, which was None -- indistinguishable from "no
        # progress made at all".
        assert result == "https://graph/page2"

    async def test_two_calls_converge_through_a_backlog_larger_than_one_limit(
        self,
    ) -> None:
        """3 pages of 10 items each (30 total), limit=12 per call. The
        fix must make real forward progress across two calls -- not
        re-fetch page 1 a second time, and not need a third call to see
        new pages beyond what a naive re-walk would find."""
        pages = {
            "https://graph.microsoft.com/v1.0/drives/d1/root/delta": (
                [_file_item(f"p1-{i}") for i in range(10)], "https://graph/page2", None,
            ),
            "https://graph/page2": (
                [_file_item(f"p2-{i}") for i in range(10)], "https://graph/page3", None,
            ),
            "https://graph/page3": (
                [_file_item(f"p3-{i}") for i in range(10)], None, "https://graph/delta-final",
            ),
        }
        get_calls: list[str] = []

        async def _fake_get(url: str, **kwargs: Any) -> Any:
            get_calls.append(url)
            items, next_link, delta_link = pages[url]
            return _page_response(items, next_link, delta_link)

        client = AsyncMock()
        client.get = _fake_get

        async def _fake_ingest(**kw: Any) -> None:
            kw["stats"]["synced"] += 1

        with patch(
            "raasoa.api.sources._ingest_sharepoint_item",
            new=AsyncMock(side_effect=_fake_ingest),
        ):
            # Call 1: limit=12 -> processes page 1 (10 items, still under
            # limit) then page 2 (10 more, now at 20 >= 12) -> stops
            # between pages, returns page 3's URL.
            stats1: dict[str, Any] = {"found": 0, "synced": 0, "deleted": 0, "delta_complete": True}
            result1 = await _sync_sharepoint_delta_drive(
                session=AsyncMock(), tenant_id=uuid.uuid4(), source_id=uuid.uuid4(),
                client=client, headers={}, site_id="site-1",
                drive={"id": "d1", "name": "Drive 1"}, cursor_url=None,
                limit=12, sync_acl=False, stats=stats1,
            )
            assert result1 == "https://graph/page3"
            assert stats1["synced"] == 20  # pages 1+2, not stopped mid-page-2
            assert get_calls == [
                "https://graph.microsoft.com/v1.0/drives/d1/root/delta",
                "https://graph/page2",
            ]

            # Call 2: resumes from page 3 using the cursor from call 1 --
            # this is the crux of the regression: the old bug would have
            # returned `cursor_url` (None, call 1's input) instead of
            # "https://graph/page3", causing call 2 to restart from
            # scratch at page 1 forever.
            stats2: dict[str, Any] = {"found": 0, "synced": 0, "deleted": 0, "delta_complete": True}
            result2 = await _sync_sharepoint_delta_drive(
                session=AsyncMock(), tenant_id=uuid.uuid4(), source_id=uuid.uuid4(),
                client=client, headers={}, site_id="site-1",
                drive={"id": "d1", "name": "Drive 1"}, cursor_url=result1,
                limit=12, sync_acl=False, stats=stats2,
            )

        assert get_calls[-1] == "https://graph/page3"
        assert stats2["synced"] == 10  # only page 3's items -- real progress
        assert stats2["delta_complete"] is True
        assert result2 == "https://graph/delta-final"

    async def test_limit_hit_before_any_page_returns_input_cursor_unchanged(self) -> None:
        """limit=0 (no budget at all this call) must not make any HTTP
        call and must hand back whatever cursor was passed in, so a
        drive that got zero budget this round doesn't lose its place."""
        client = AsyncMock()
        client.get = AsyncMock()
        stats: dict[str, Any] = {"found": 0, "synced": 0, "deleted": 0, "delta_complete": True}

        result = await _sync_sharepoint_delta_drive(
            session=AsyncMock(), tenant_id=uuid.uuid4(), source_id=uuid.uuid4(),
            client=client, headers={}, site_id="site-1",
            drive={"id": "d1", "name": "Drive 1"}, cursor_url="https://graph/existing-cursor",
            limit=0, sync_acl=False, stats=stats,
        )

        client.get.assert_not_called()
        assert result == "https://graph/existing-cursor"
        assert stats["delta_complete"] is False
