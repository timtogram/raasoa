"""Tests for source connector normalization helpers."""

from raasoa.api.sources import (
    _adf_to_text,
    _jira_issue_metadata,
    _jira_issue_to_markdown,
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
