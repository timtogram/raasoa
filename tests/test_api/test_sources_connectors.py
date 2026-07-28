"""Tests for source connector normalization helpers."""

from raasoa.api.sources import (
    _adf_to_text,
    _hubspot_record_title,
    _hubspot_record_to_markdown,
    _jira_issue_metadata,
    _jira_issue_to_markdown,
    _notion_block_to_text,
    _notion_blocks_to_text,
    _sharepoint_item_path,
    _sharepoint_source_object_id,
)


def test_sharepoint_item_path_from_parent_reference() -> None:
    item = {
        "id": "item-1",
        "name": "Policy.pdf",
        "parentReference": {"path": "/drives/drive-1/root:/Policies/HR"},
    }
    source_path, folder_path = _sharepoint_item_path(item)
    assert source_path == "Policies/HR/Policy.pdf"
    assert folder_path == "Policies/HR"
    assert _sharepoint_source_object_id("drive-1", "item-1") == "sharepoint:drive-1:item-1"


def test_jira_adf_to_text_extracts_nested_text() -> None:
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "First line"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "Second line"},
                ],
            }
        ],
    }
    assert "First line" in _adf_to_text(adf)
    assert "Second line" in _adf_to_text(adf)


def test_jira_issue_to_markdown_and_metadata() -> None:
    issue = {
        "id": "10001",
        "key": "OPS-42",
        "fields": {
            "summary": "Fix knowledge import",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Importer fails."}],
                    }
                ],
            },
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Bug"},
            "priority": {"name": "High"},
            "project": {"key": "OPS", "name": "Operations"},
            "labels": ["enterprise"],
            "assignee": {"displayName": "Ada Lovelace"},
            "reporter": {"displayName": "Grace Hopper"},
            "created": "2026-04-01T10:00:00.000+0000",
            "updated": "2026-04-02T10:00:00.000+0000",
            "comment": {"comments": []},
        },
    }
    markdown = _jira_issue_to_markdown(issue, "https://example.atlassian.net")
    assert "# OPS-42: Fix knowledge import" in markdown
    assert "Importer fails." in markdown
    metadata = _jira_issue_metadata(issue, "https://example.atlassian.net")
    assert metadata["source_path"] == "OPS/OPS-42"
    assert metadata["folder_path"] == "OPS"
    assert metadata["status"] == "In Progress"


def test_notion_blocks_to_text_preserves_common_blocks() -> None:
    text = _notion_blocks_to_text([
        {
            "type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": "Plan"}]},
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": "Ship it"}]},
        },
        {
            "type": "to_do",
            "to_do": {"checked": True, "rich_text": [{"plain_text": "Verified"}]},
        },
    ])
    assert "## Plan" in text
    assert "- Ship it" in text
    assert "[x] Verified" in text


def test_notion_table_row_cells_are_rendered_not_dropped() -> None:
    """Regression: table_row's cell content lives under "cells" (a list
    of lists of rich-text objects), not "rich_text" like every other
    block -- the generic rich_text-based extraction silently produced an
    empty string for every table row before this fix."""
    row_text = _notion_block_to_text({
        "type": "table_row",
        "table_row": {
            "cells": [
                [{"plain_text": "Name"}],
                [{"plain_text": "Status"}],
            ],
        },
    })
    assert row_text == "| Name | Status |"


def test_notion_table_row_with_empty_cells_is_dropped() -> None:
    row_text = _notion_block_to_text({
        "type": "table_row",
        "table_row": {"cells": [[], []]},
    })
    assert row_text == ""


def test_notion_table_parent_block_contributes_no_text_itself() -> None:
    """The parent "table" block carries only layout config
    (table_width/has_column_header/has_row_header), never cell data --
    its rows arrive separately as table_row children."""
    table_text = _notion_block_to_text({
        "type": "table",
        "table": {"table_width": 2, "has_column_header": True},
    })
    assert table_text == ""


def test_hubspot_record_title_prefers_named_property() -> None:
    assert _hubspot_record_title("deals", {"dealname": "Acme Renewal"}) == "Acme Renewal"
    assert _hubspot_record_title("companies", {"name": "Acme Corp"}) == "Acme Corp"


def test_hubspot_record_title_falls_back_to_contact_name() -> None:
    title = _hubspot_record_title(
        "contacts", {"firstname": "Ada", "lastname": "Lovelace"},
    )
    assert title == "Ada Lovelace"


def test_hubspot_record_title_falls_back_to_object_id() -> None:
    title = _hubspot_record_title("deals", {"hs_object_id": "42"})
    assert title == "deal 42"


def test_hubspot_record_to_markdown_includes_properties() -> None:
    md = _hubspot_record_to_markdown(
        "deals",
        "123",
        {
            "dealname": "Acme Renewal",
            "amount": "50000",
            "dealstage": "closedwon",
            "hubspot_owner_id": None,  # should be skipped, not rendered as "None"
        },
    )
    assert "# Acme Renewal" in md
    assert "HubSpot object type: deal" in md
    assert "amount: 50000" in md
    assert "dealstage: closedwon" in md
    assert "hubspot_owner_id" not in md
