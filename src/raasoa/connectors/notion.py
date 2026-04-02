"""Notion Connector for RAASOA.

Syncs Notion pages into the RAASOA knowledge base via the Webhook API.
Can be run as a standalone script or imported as a module.

Usage:
    # Sync specific pages by search query
    uv run python -m raasoa.connectors.notion --query "KI-Strategie" --limit 10

    # Sync all pages from workspace
    uv run python -m raasoa.connectors.notion --query "*" --limit 50
"""

import argparse
import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RAASOA_URL = "http://localhost:8001"
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
DEFAULT_NOTION_API = "https://api.notion.com/v1"


def _strip_markdown_artifacts(text: str) -> str:
    """Clean up Notion-exported markdown for better chunking."""
    # Remove excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # Remove Notion block references
    text = re.sub(
        r"<(?:page|database|data-source)[^>]*>.*?</(?:page|database|data-source)>", "", text
    )
    text = re.sub(r"<(?:page|database)[^>]*/>", "", text)
    return text.strip()


async def sync_notion_pages(
    raasoa_url: str,
    tenant_id: str,
    notion_token: str,
    search_query: str = "*",
    limit: int = 20,
) -> dict[str, Any]:
    """Search Notion and sync matching pages to RAASOA.

    This function uses the Notion API directly. For MCP-based sync,
    use the sync_from_mcp_results function instead.
    """
    stats: dict[str, Any] = {"found": 0, "synced": 0, "skipped": 0, "failed": 0, "errors": []}

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Search Notion
        resp = await client.post(
            f"{DEFAULT_NOTION_API}/search",
            headers={
                "Authorization": f"Bearer {notion_token}",
                "Notion-Version": "2022-06-28",
            },
            json={"query": search_query, "page_size": min(limit, 100)},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        stats["found"] = len(results)

        for page in results:
            if page.get("object") != "page":
                stats["skipped"] += 1
                continue

            page_id = page["id"]
            title = _extract_title(page)

            # Fetch page content
            try:
                blocks_resp = await client.get(
                    f"{DEFAULT_NOTION_API}/blocks/{page_id}/children",
                    headers={
                        "Authorization": f"Bearer {notion_token}",
                        "Notion-Version": "2022-06-28",
                    },
                    params={"page_size": 100},
                )
                blocks_resp.raise_for_status()
                content = _blocks_to_text(blocks_resp.json().get("results", []))
            except Exception as e:
                logger.warning("Failed to fetch blocks for %s: %s", page_id, e)
                content = title

            if len(content.strip()) < 20:
                stats["skipped"] += 1
                continue

            # Push to RAASOA via webhook
            try:
                ingest_resp = await client.post(
                    f"{raasoa_url}/v1/webhooks/ingest",
                    json={
                        "event": "document.created",
                        "source": "notion",
                        "title": title,
                        "content": content,
                        "source_object_id": f"notion-{page_id}",
                        "source_url": page.get("url", ""),
                        "metadata": {
                            "notion_id": page_id,
                            "last_edited": page.get("last_edited_time", ""),
                        },
                    },
                    headers={"X-Tenant-Id": tenant_id},
                )
                if ingest_resp.status_code == 200:
                    data = ingest_resp.json()
                    logger.info(
                        "Synced: %s (quality=%.0f%%, chunks=%s)",
                        title,
                        float(data.get("quality_score") or 0) * 100,
                        data.get("chunk_count", "?"),
                    )
                    stats["synced"] += 1
                else:
                    stats["failed"] += 1
                    stats["errors"].append({"page": title, "error": ingest_resp.text})
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append({"page": title, "error": str(e)})

    return stats


def _extract_title(page: dict[str, Any]) -> str:
    """Extract title from a Notion page object."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            titles = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in titles) or "Untitled"
    return "Untitled"


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    """Convert Notion blocks to plain text."""
    parts: list[str] = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})

        if block_type in ("paragraph", "bulleted_list_item", "numbered_list_item", "quote"):
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                if "list_item" in block_type:
                    prefix = "- "
                elif block_type == "quote":
                    prefix = "> "
                else:
                    prefix = ""
                parts.append(f"{prefix}{text}")

        elif block_type.startswith("heading_"):
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            level = block_type[-1]
            if text:
                parts.append(f"{'#' * int(level)} {text}")

        elif block_type == "code":
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            lang = content.get("language", "")
            if text:
                parts.append(f"```{lang}\n{text}\n```")

        elif block_type == "to_do":
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            checked = content.get("checked", False)
            if text:
                parts.append(f"[{'x' if checked else ' '}] {text}")

        elif block_type == "toggle":
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                parts.append(text)

        elif block_type == "callout":
            rich_text = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                parts.append(f"> {text}")

    return "\n\n".join(parts)


async def sync_page_by_content(
    raasoa_url: str,
    tenant_id: str,
    page_id: str,
    title: str,
    content: str,
    source_url: str = "",
) -> dict[str, Any]:
    """Sync a single page to RAASOA using pre-fetched content.

    Use this when you already have the page content (e.g. from MCP fetch).
    """
    content = _strip_markdown_artifacts(content)

    if len(content.strip()) < 20:
        return {"status": "skipped", "reason": "too_short"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{raasoa_url}/v1/webhooks/ingest",
            json={
                "event": "document.created",
                "source": "notion",
                "title": title,
                "content": content,
                "source_object_id": f"notion-{page_id}",
                "source_url": source_url,
            },
            headers={"X-Tenant-Id": tenant_id},
        )
        if resp.status_code == 200:
            return {"status": "synced", **resp.json()}
        return {"status": "failed", "error": resp.text}


def main() -> None:
    """CLI entrypoint for Notion sync."""
    parser = argparse.ArgumentParser(description="Sync Notion pages to RAASOA")
    parser.add_argument("--query", default="*", help="Notion search query")
    parser.add_argument("--limit", type=int, default=20, help="Max pages to sync")
    parser.add_argument("--url", default=DEFAULT_RAASOA_URL, help="RAASOA API URL")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="Tenant ID")
    parser.add_argument("--token", required=True, help="Notion integration token")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    stats = asyncio.run(
        sync_notion_pages(args.url, args.tenant, args.token, args.query, args.limit)
    )
    print(f"\nSync complete: {stats}")


if __name__ == "__main__":
    main()
