"""RAASOA MCP Server — Model Context Protocol adapter.

Exposes RAASOA's RAG capabilities as MCP tools and resources for
AI agents (Claude, Cursor, Windsurf, custom agents).

Usage:
    # As stdio server (for Claude Desktop, Cursor, etc.)
    uv run python -m raasoa.mcp.server

    # Configure in Claude Desktop's claude_desktop_config.json:
    {
      "mcpServers": {
        "raasoa": {
          "command": "uv",
          "args": ["run", "python", "-m", "raasoa.mcp.server"],
          "cwd": "/path/to/raasoa"
        }
      }
    }
"""

import json
import logging
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Configurable via env
BASE_URL = "http://localhost:8000"
API_KEY = ""  # Set via RAASOA_API_KEY env


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


# ── MCP Protocol Implementation (JSON-RPC over stdio) ──────────────


def _make_response(msg_id: int | str | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _make_error(msg_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_definitions() -> list[dict[str, Any]]:
    """Define MCP tools exposed by RAASOA."""
    return [
        {
            "name": "raasoa_search",
            "description": (
                "Search the knowledge base using hybrid search (semantic + keyword). "
                "Returns ranked document chunks with confidence scores. "
                "Use this to answer questions based on ingested documents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query. Can be a question or keywords.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (1-50, default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "raasoa_ingest",
            "description": (
                "Ingest a text document into the knowledge base. "
                "The document will be chunked, embedded, and quality-checked."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title of the document.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full text content of the document.",
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "raasoa_list_documents",
            "description": (
                "List all documents in the knowledge base with their "
                "quality scores and status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of documents to return.",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "raasoa_get_document",
            "description": (
                "Get full details of a specific document including "
                "all chunks and quality information."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "UUID of the document.",
                    },
                },
                "required": ["document_id"],
            },
        },
        {
            "name": "raasoa_quality_report",
            "description": (
                "Get the quality report for a document, including "
                "quality score, findings, and review status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "UUID of the document.",
                    },
                },
                "required": ["document_id"],
            },
        },
        {
            "name": "raasoa_list_conflicts",
            "description": (
                "List detected conflicts between documents. "
                "Includes claim-based contradictions and overlaps."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (new, resolved).",
                        "enum": ["new", "resolved"],
                    },
                },
            },
        },
        {
            "name": "raasoa_feedback",
            "description": (
                "Submit feedback on a search result. Positive feedback boosts "
                "the chunk in future rankings, negative feedback demotes it. "
                "Call this after using raasoa_search when a result was "
                "particularly helpful or unhelpful."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The original search query.",
                    },
                    "chunk_id": {
                        "type": "string",
                        "description": "ID of the chunk being rated.",
                    },
                    "document_id": {
                        "type": "string",
                        "description": "ID of the parent document.",
                    },
                    "rating": {
                        "type": "number",
                        "description": (
                            "Rating from -1.0 (unhelpful) to 1.0 (very helpful). "
                            "Use 1.0 for spot-on results, -1.0 for irrelevant ones."
                        ),
                    },
                },
                "required": ["query", "chunk_id", "document_id", "rating"],
            },
        },
        {
            "name": "raasoa_get_synthesis",
            "description": (
                "Get a compiled knowledge summary for a topic. "
                "Syntheses are LLM-generated from extracted claims — "
                "more coherent than raw chunks for answering questions. "
                "Topics are typically entity names like 'Company', 'HR Policy', etc."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (e.g. 'Company', 'HR Policy').",
                    },
                },
                "required": ["topic"],
            },
        },
        {
            "name": "raasoa_curate",
            "description": (
                "Run the LLM-powered knowledge curation pipeline. "
                "Normalizes predicates (merges equivalent terms), "
                "rebuilds the knowledge index, and audits for issues. "
                "Run this after ingesting new documents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "raasoa_compile",
            "description": (
                "Trigger knowledge compilation — the LLM reads all claims "
                "and writes synthesized summaries per topic. "
                "Run this after ingesting new documents to update the "
                "compiled knowledge base."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Compile a specific topic. Omit to compile all.",
                    },
                },
            },
        },
    ]


def _resource_definitions() -> list[dict[str, Any]]:
    """Define MCP resources exposed by RAASOA."""
    return [
        {
            "uri": "raasoa://health",
            "name": "RAASOA Health Status",
            "description": "Current health status of the RAG service.",
            "mimeType": "application/json",
        },
        {
            "uri": "raasoa://stats",
            "name": "Knowledge Base Statistics",
            "description": "Statistics about the knowledge base (document count, quality, etc.).",
            "mimeType": "application/json",
        },
    ]


async def _handle_tool_call(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute an MCP tool call and return content blocks."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        if name == "raasoa_search":
            resp = await client.post(
                f"{BASE_URL}/v1/retrieve",
                json={
                    "query": arguments["query"],
                    "top_k": arguments.get("top_k", 5),
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # Format results for the AI agent
            parts = []
            routed = data.get("routed_to", "rag")
            confidence = data.get("confidence", {})
            parts.append(
                f"Query: {data['query']}\n"
                f"Routed to: {routed}\n"
                f"Confidence: {confidence.get('retrieval_confidence', 0):.0%}\n"
                f"Answerable: {confidence.get('answerable', False)}\n"
                f"Sources: {confidence.get('source_count', 0)}\n"
            )

            # Structured answer
            structured = data.get("structured")
            if structured:
                parts.append(f"\nStructured Answer: {structured['answer']}\n")

            # RAG results
            for i, hit in enumerate(data.get("results", []), 1):
                section = f" [{hit.get('section_title', '')}]" if hit.get("section_title") else ""
                parts.append(
                    f"\n--- Result #{i}{section} (score: {hit['score']:.4f}) ---\n"
                    f"{hit['text']}\n"
                )

            return [{"type": "text", "text": "\n".join(parts)}]

        elif name == "raasoa_ingest":
            # Create a temporary text file and upload
            content = arguments["content"]
            title = arguments["title"]
            filename = f"{title.replace(' ', '_')}.txt"
            file_content = f"# {title}\n\n{content}"

            resp = await client.post(
                f"{BASE_URL}/v1/ingest",
                files={"file": (filename, file_content.encode(), "text/plain")},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "type": "text",
                    "text": (
                        f"Document ingested successfully.\n"
                        f"  ID: {data['document_id']}\n"
                        f"  Title: {data.get('title', title)}\n"
                        f"  Chunks: {data['chunk_count']}\n"
                        f"  Quality: {data.get('quality_score', 'N/A')}\n"
                        f"  Status: {data.get('review_status', 'unknown')}\n"
                    ),
                }
            ]

        elif name == "raasoa_list_documents":
            resp = await client.get(
                f"{BASE_URL}/v1/documents",
                params={"limit": arguments.get("limit", 20)},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])

            if not items:
                return [{"type": "text", "text": "No documents in the knowledge base."}]

            lines = [f"Knowledge Base: {len(items)} documents\n"]
            for doc in items:
                quality = f"{doc['quality_score']:.2f}" if doc.get("quality_score") else "—"
                lines.append(
                    f"  • {doc.get('title', '(untitled)')} "
                    f"[{doc['status']}, quality={quality}, "
                    f"chunks={doc['chunk_count']}, tier={doc.get('index_tier', 'hot')}]\n"
                    f"    ID: {doc['id']}"
                )
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_get_document":
            doc_id = arguments["document_id"]
            resp = await client.get(
                f"{BASE_URL}/v1/documents/{doc_id}",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            lines = [
                f"Document: {data.get('title', '(untitled)')}\n"
                f"ID: {data['id']}\n"
                f"Status: {data['status']} | Review: {data.get('review_status', '?')}\n"
                f"Quality: {data.get('quality_score', 'N/A')} | "
                f"Conflicts: {data.get('conflict_status', 'none')}\n"
                f"Chunks: {data['chunk_count']} | Version: {data['version']}\n"
                f"Tier: {data.get('index_tier', 'hot')}\n"
            ]

            chunks = data.get("chunks", [])
            if chunks:
                lines.append(f"\n--- Chunks ({len(chunks)}) ---")
                for c in chunks[:10]:  # Limit to first 10
                    section = f" [{c.get('section_title', '')}]" if c.get("section_title") else ""
                    lines.append(
                        f"\nChunk #{c['chunk_index']}{section} "
                        f"({c.get('token_count', '?')} tokens):\n"
                        f"{c['chunk_text'][:300]}{'...' if len(c['chunk_text']) > 300 else ''}"
                    )

            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_quality_report":
            doc_id = arguments["document_id"]
            resp = await client.get(f"{BASE_URL}/v1/documents/{doc_id}/quality")
            resp.raise_for_status()
            data = resp.json()

            lines = [
                f"Quality Report: {data.get('title', '?')}\n"
                f"Score: {data.get('quality_score', 'N/A')}\n"
                f"Review Status: {data.get('review_status', '?')}\n"
                f"Conflict Status: {data.get('conflict_status', 'none')}\n"
            ]

            findings = data.get("findings", [])
            if findings:
                lines.append(f"\nFindings ({len(findings)}):")
                for f in findings:
                    lines.append(f"  [{f['severity']}] {f['finding_type']}")
            else:
                lines.append("\nNo quality findings — document is clean.")

            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_list_conflicts":
            params: dict[str, Any] = {"limit": 20}
            if "status" in arguments:
                params["status"] = arguments["status"]

            resp = await client.get(
                f"{BASE_URL}/v1/conflicts",
                params=params,
                headers=_headers(),
            )
            resp.raise_for_status()
            conflicts = resp.json()

            if not conflicts:
                return [{"type": "text", "text": "No conflicts detected."}]

            lines = [f"Conflicts: {len(conflicts)}\n"]
            for c in conflicts:
                conf = f"{c['confidence']:.2f}" if c.get("confidence") else "—"
                lines.append(
                    f"  [{c['status']}] {c['conflict_type']} "
                    f"(confidence={conf})\n"
                    f"    Doc A: {c['document_a_id']}\n"
                    f"    Doc B: {c['document_b_id']}\n"
                    f"    ID: {c['id']}"
                )
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_feedback":
            resp = await client.post(
                f"{BASE_URL}/v1/retrieve/feedback",
                json={
                    "query": arguments["query"],
                    "chunk_id": arguments["chunk_id"],
                    "document_id": arguments["document_id"],
                    "rating": arguments["rating"],
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            rating = arguments["rating"]
            label = "positive" if rating > 0 else "negative" if rating < 0 else "neutral"
            return [{"type": "text", "text": f"Feedback recorded ({label}, {rating})."}]

        elif name == "raasoa_get_synthesis":
            topic = arguments["topic"]
            resp = await client.get(
                f"{BASE_URL}/v1/synthesis/{topic}",
                headers=_headers(),
            )
            if resp.status_code == 404:
                msg = f"No synthesis for '{topic}'. Run raasoa_compile first."
                return [{"type": "text", "text": msg}]
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "type": "text",
                    "text": (
                        f"Knowledge Synthesis: {data['topic']}\n"
                        f"Claims: {data['claim_count']} | "
                        f"Sources: {data['source_documents']} | "
                        f"Confidence: {data.get('confidence', 'N/A')}\n"
                        f"Last updated: {data.get('updated_at', '?')}\n\n"
                        f"{data['summary']}"
                    ),
                }
            ]

        elif name == "raasoa_curate":
            resp = await client.post(
                f"{BASE_URL}/v1/synthesis/curate",
                json={},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            norm = data.get("normalization", {})
            idx = data.get("index", {})
            findings = data.get("findings", [])
            lines = [
                "Knowledge Curation Complete:\n",
                f"Normalization: {norm.get('normalized', 0)} claims "
                f"normalized across {norm.get('mappings', 0)} predicate groups",
                f"Index: {idx.get('entries', 0)} entries from "
                f"{idx.get('claims_processed', 0)} claims",
                f"Lint: {len(findings)} issues found",
            ]
            for f in findings[:5]:
                lines.append(
                    f"  [{f.get('type', '?')}] {f.get('description', '')}"
                )
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_compile":
            body: dict[str, str | None] = {}
            if "topic" in arguments:
                body["topic"] = arguments["topic"]
            resp = await client.post(
                f"{BASE_URL}/v1/synthesis/compile",
                json=body,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            compiled = data.get("compiled", [])
            lines = [f"Compiled {len(compiled)} topic(s):\n"]
            for item in compiled:
                lines.append(f"  • {item.get('topic', '?')}: {item.get('claim_count', 0)} claims")
            return [{"type": "text", "text": "\n".join(lines)}]

        else:
            return [{"type": "text", "text": f"Unknown tool: {name}"}]


async def _handle_resource_read(uri: str) -> list[dict[str, Any]]:
    """Read an MCP resource."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        if uri == "raasoa://health":
            resp = await client.get(f"{BASE_URL}/health")
            resp.raise_for_status()
            return [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(resp.json(), indent=2),
                }
            ]

        elif uri == "raasoa://stats":
            # Fetch document list and compute stats
            resp = await client.get(
                f"{BASE_URL}/v1/documents",
                params={"limit": 200},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])

            total = len(items)
            indexed = sum(1 for d in items if d["status"] == "indexed")
            quality_scores = [d["quality_score"] for d in items if d.get("quality_score")]
            avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0

            stats = {
                "total_documents": total,
                "indexed": indexed,
                "average_quality_score": round(avg_quality, 3),
                "total_chunks": sum(d["chunk_count"] for d in items),
            }
            return [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(stats, indent=2),
                }
            ]

        return [{"uri": uri, "mimeType": "text/plain", "text": f"Unknown resource: {uri}"}]


def _handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle a single JSON-RPC message synchronously (dispatch to async)."""
    import asyncio

    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _make_response(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": {
                "name": "raasoa",
                "version": "0.1.0",
            },
        })

    elif method == "notifications/initialized":
        return None  # No response for notifications

    elif method == "tools/list":
        return _make_response(msg_id, {"tools": _tool_definitions()})

    elif method == "resources/list":
        return _make_response(msg_id, {"resources": _resource_definitions()})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            content = asyncio.run(_handle_tool_call(tool_name, arguments))
            return _make_response(msg_id, {"content": content})
        except httpx.ConnectError:
            return _make_response(msg_id, {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Cannot connect to RAASOA API at "
                            f"{BASE_URL}. Is the server running?"
                        ),
                    }
                ],
                "isError": True,
            })
        except Exception as e:
            return _make_response(msg_id, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })

    elif method == "resources/read":
        uri = params.get("uri", "")
        try:
            contents = asyncio.run(_handle_resource_read(uri))
            return _make_response(msg_id, {"contents": contents})
        except Exception as e:
            return _make_error(msg_id, -32603, str(e))

    elif method == "ping":
        return _make_response(msg_id, {})

    else:
        return _make_error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Run the MCP server on stdio."""
    import os

    global BASE_URL, API_KEY
    BASE_URL = os.environ.get("RAASOA_URL", BASE_URL)
    API_KEY = os.environ.get("RAASOA_API_KEY", API_KEY)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,  # Logs go to stderr, protocol on stdout
    )
    logger.info("RAASOA MCP Server starting (API: %s)", BASE_URL)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
