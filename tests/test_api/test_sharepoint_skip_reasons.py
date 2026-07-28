"""Tests for SharePoint skip-reason visibility (F-046 follow-up, task 29).

Before this fix, folder/package/non-file/unsupported-extension items were
either silently dropped with no counter increment at all (folder and
"package" driveItems -- OneNote notebooks) or lumped into an undifferentiated
`stats["skipped"]` counter with no way to tell "12 .aspx pages aren't
covered" apart from "12 video files aren't covered". `_record_sharepoint_skip_reason`
gives every skip a distinguishable, aggregated reason in `stats["skip_reasons"]`.

This is deliberately a *visibility* fix, not new content coverage: real
parsing of .aspx modern pages (Graph Pages API) and OneNote notebooks
(Graph OneNote API) remains out of scope, documented as such in
`_record_sharepoint_skip_reason`'s docstring and in DEPLOYMENT.md.

Pure unit tests on `_ingest_sharepoint_item` -- every case exercised here
returns before touching the session, HTTP client, or Graph API, so no
mocking or live Postgres is needed.
"""
from __future__ import annotations

import uuid
from typing import Any

from raasoa.api.sources import _ingest_sharepoint_item, _record_sharepoint_skip_reason


def _base_stats() -> dict[str, Any]:
    return {
        "found": 0, "synced": 0, "skipped": 0,
        "deleted": 0, "errors": [], "drives": [], "delta_complete": True,
    }


async def _ingest(item: dict[str, Any], stats: dict[str, Any]) -> None:
    await _ingest_sharepoint_item(
        session=None,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        client=None,
        headers={},
        site_id="site-1",
        drive={"id": "drive-1"},
        item=item,
        sync_acl=False,
        stats=stats,
    )


class TestRecordSharepointSkipReason:
    def test_first_occurrence_sets_count_to_one(self) -> None:
        stats = _base_stats()
        _record_sharepoint_skip_reason(stats, "not_a_file")
        assert stats["skip_reasons"] == {"not_a_file": 1}

    def test_repeated_reason_increments_existing_count(self) -> None:
        stats = _base_stats()
        _record_sharepoint_skip_reason(stats, "not_a_file")
        _record_sharepoint_skip_reason(stats, "not_a_file")
        _record_sharepoint_skip_reason(stats, "not_a_file")
        assert stats["skip_reasons"] == {"not_a_file": 3}

    def test_distinct_reasons_tracked_independently(self) -> None:
        stats = _base_stats()
        _record_sharepoint_skip_reason(stats, "aspx_modern_page")
        _record_sharepoint_skip_reason(stats, "onenote_or_package_item")
        _record_sharepoint_skip_reason(stats, "aspx_modern_page")
        assert stats["skip_reasons"] == {
            "aspx_modern_page": 2,
            "onenote_or_package_item": 1,
        }


class TestIngestSharepointItemSkipSignaling:
    async def test_folder_returns_with_no_signal(self) -> None:
        """Structural, not content -- unchanged behavior: no skip counter,
        no skip_reasons key created at all."""
        stats = _base_stats()
        await _ingest({"folder": {"childCount": 3}, "name": "Reports"}, stats)
        assert stats["skipped"] == 0
        assert "skip_reasons" not in stats

    async def test_onenote_package_item_now_counted_with_reason(self) -> None:
        """Regression: this used to return silently -- not even
        incrementing stats["skipped"] -- indistinguishable from an item
        that was never discovered at all."""
        stats = _base_stats()
        await _ingest(
            {"package": {"type": "oneNote"}, "name": "Team Notebook"}, stats,
        )
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"onenote_or_package_item": 1}

    async def test_non_file_item_counted_as_not_a_file(self) -> None:
        stats = _base_stats()
        await _ingest({"name": "weird-item"}, stats)
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"not_a_file": 1}

    async def test_aspx_extension_gets_distinguishable_reason(self) -> None:
        """Regression: .aspx modern SharePoint pages used to collapse into
        the same undifferentiated "unsupported extension" bucket as any
        other unsupported file type -- now distinguishable so an admin can
        see this specific, documented coverage gap."""
        stats = _base_stats()
        await _ingest(
            {"file": {}, "name": "company-news.aspx"}, stats,
        )
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"aspx_modern_page": 1}

    async def test_other_unsupported_extension_keeps_generic_reason(self) -> None:
        stats = _base_stats()
        await _ingest({"file": {}, "name": "video.mp4"}, stats)
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"unsupported_extension:mp4": 1}

    async def test_unsupported_extension_missing_entirely_uses_none_placeholder(self) -> None:
        stats = _base_stats()
        await _ingest({"file": {}, "name": "README"}, stats)
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"unsupported_extension:none": 1}

    async def test_mixed_batch_aggregates_reasons_independently(self) -> None:
        stats = _base_stats()
        await _ingest({"folder": {}, "name": "Archive"}, stats)
        await _ingest({"package": {"type": "oneNote"}, "name": "Notes"}, stats)
        await _ingest({"file": {}, "name": "page.aspx"}, stats)
        await _ingest({"file": {}, "name": "clip.mp4"}, stats)
        await _ingest({"file": {}, "name": "another.aspx"}, stats)

        assert stats["skipped"] == 4  # folder doesn't count
        assert stats["skip_reasons"] == {
            "onenote_or_package_item": 1,
            "aspx_modern_page": 2,
            "unsupported_extension:mp4": 1,
        }
