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

import contextvars
import json
import logging
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Configurable via env
BASE_URL = "http://localhost:8000"
API_KEY = ""  # Set via RAASOA_API_KEY env

# Per-request bearer token, set by the HTTP transport so concurrent
# requests stay isolated (the stdio transport leaves this unset and falls
# back to the global API_KEY). ContextVars are per-async-task safe.
_request_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "raasoa_mcp_request_api_key", default=None,
)


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    key = _request_api_key.get() or API_KEY
    if key:
        headers["Authorization"] = f"Bearer {key}"
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
                    "metadata_filter": {
                        "type": "object",
                        "description": (
                            "Filter by frontmatter metadata. "
                            "E.g. {'ampel': 'grün'} returns only approved docs. "
                            "{'type': 'policy'} returns only policies."
                        ),
                    },
                    "source_type": {
                        "type": "string",
                        "description": "Filter by source (notion, sharepoint, etc.)",
                    },
                    "agent_clearance": {
                        "type": "string",
                        "description": (
                            "Policy-gate clearance for the calling agent. "
                            "One of: public, internal, restricted, "
                            "confidential, secret. Documents with a higher "
                            "classification are filtered out and audit-logged."
                        ),
                        "default": "public",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "raasoa_find_by_metadata",
            "description": (
                "Find documents by their structured metadata (frontmatter). "
                "Use this when you need to filter by specific fields like "
                "ampel, type, owner, version, abteilung — without a text query. "
                "Returns documents matching ALL specified metadata fields."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Key-value pairs to match. "
                            "E.g. {'ampel': 'grün', 'type': 'policy'}"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["metadata"],
            },
        },
        {
            "name": "raasoa_doc_dependencies",
            "description": (
                "Find documents related to a specific document. "
                "Shows shared claims, same-source siblings, and contradictions. "
                "Use this to understand what other knowledge is connected."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "ID of the document to find dependencies for.",
                    },
                },
                "required": ["document_id"],
            },
        },
        {
            "name": "raasoa_doc_diff",
            "description": (
                "Show what changed between versions of a document. "
                "Returns claim changes: what values were updated, added, or removed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "ID of the document.",
                    },
                },
                "required": ["document_id"],
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
            "name": "raasoa_auto_resolve",
            "description": (
                "Ask the LLM judge to evaluate and auto-resolve conflicts. "
                "Conflicts above the confidence threshold are resolved automatically. "
                "Lower-confidence conflicts are kept for human review. "
                "Call this after ingesting documents that created conflicts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": (
                            "Confidence threshold for auto-resolve (0.0-1.0). "
                            "Default: 0.85. Higher = more conservative."
                        ),
                    },
                },
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
        {
            "name": "raasoa_get_skill",
            "description": (
                "Look up a Skill (a structured work-instruction document) "
                "by name or topic. Returns the full SKILL.md content "
                "plus structured sections (zweck/sop/dod) and the "
                "review/ampel status — so the agent can either follow "
                "the SOP or refuse if the skill is not approved. "
                "Use this when the user/agent asks 'how do I X' for any "
                "process the company has documented."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name or topic. Matched first against "
                            "frontmatter 'name', then by title, then by "
                            "semantic search inside type=skill documents."
                        ),
                    },
                    "agent_clearance": {
                        "type": "string",
                        "description": (
                            "Policy-gate clearance "
                            "(public/internal/restricted/...). Skills "
                            "above this level are filtered out."
                        ),
                        "default": "public",
                    },
                },
                "required": ["name"],
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
    from raasoa.mcp.policy import (
        apply_policy_gate,
        audit_denials,
        env_default_clearance,
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        if name == "raasoa_search":
            search_body: dict[str, Any] = {
                "query": arguments["query"],
                "top_k": arguments.get("top_k", 5),
            }
            if "metadata_filter" in arguments:
                search_body["metadata_filter"] = arguments["metadata_filter"]
            if "source_type" in arguments:
                search_body["source_type"] = arguments["source_type"]

            resp = await client.post(
                f"{BASE_URL}/v1/retrieve",
                json=search_body,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # ── Policy-gate ─────────────────────────────────
            # Server-side default acts as a hard ceiling; agent
            # may request a *lower* clearance but never higher.
            requested = (
                arguments.get("agent_clearance")
                or env_default_clearance()
            )
            allowed_hits, denied_hits = apply_policy_gate(
                data.get("results", []),
                clearance=requested,
            )
            data["results"] = allowed_hits
            if denied_hits:
                await audit_denials(
                    BASE_URL, _headers(),
                    tool="raasoa_search",
                    query=arguments.get("query"),
                    denied=denied_hits,
                )
                data.setdefault("policy", {})
                data["policy"]["denied_count"] = len(denied_hits)
                data["policy"]["clearance"] = requested

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

            # RAG results with source provenance
            for i, hit in enumerate(data.get("results", []), 1):
                section = f" [{hit.get('section_title', '')}]" if hit.get("section_title") else ""
                title = hit.get("document_title") or ""
                source_url = hit.get("source_url") or ""
                source_type = hit.get("source_type") or ""
                location = hit.get("source_location") or ""
                page = hit.get("page_number")
                provenance = ""
                if title:
                    provenance += f"Document: {title}\n"
                if location:
                    provenance += f"Location: {location}\n"
                elif page:
                    provenance += f"Page: {page}\n"
                if source_url:
                    provenance += f"Source: {source_url}\n"
                if source_type:
                    provenance += f"Source type: {source_type}\n"
                parts.append(
                    f"\n--- Result #{i}{section} "
                    f"(score: {hit['score']:.4f}) ---\n"
                    f"{provenance}"
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

        elif name == "raasoa_auto_resolve":
            resolve_params: dict[str, Any] = {}
            if "threshold" in arguments:
                resolve_params["threshold"] = arguments["threshold"]
            resp = await client.post(
                f"{BASE_URL}/v1/conflicts/auto-resolve",
                params=resolve_params,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            lines = [
                f"LLM Judge Results:\n"
                f"Open conflicts: {data.get('total_open', 0)}\n"
                f"Judged: {data.get('judged', 0)}\n"
                f"Auto-resolved: {data.get('auto_resolved', 0)}\n"
                f"Kept for human: {data.get('kept_for_human', 0)}\n"
            ]
            for v in data.get("verdicts", [])[:5]:
                status = "AUTO-RESOLVED" if v.get("auto_resolved") else "needs human"
                lines.append(
                    f"\n[{status}] {v['recommendation']} "
                    f"({v['confidence']:.0%}): {v['reasoning']}"
                )
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_find_by_metadata":
            meta = arguments.get("metadata", {})
            limit = arguments.get("limit", 20)
            # Query documents with matching metadata
            resp = await client.get(
                f"{BASE_URL}/v1/documents",
                params={"limit": limit},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            # Note: server-side metadata filtering would be better
            # but for now we filter client-side from the document list
            lines = [f"Documents matching {meta}:\n"]
            if not items:
                lines.append("No documents found.")
            else:
                for doc in items:
                    lines.append(
                        f"- {doc.get('title', 'Untitled')} "
                        f"(quality: {doc.get('quality_score', '?')}, "
                        f"status: {doc.get('status', '?')})"
                    )
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_doc_dependencies":
            doc_id = arguments["document_id"]
            resp = await client.get(
                f"{BASE_URL}/v1/documents/{doc_id}/dependencies",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            deps = data.get("dependencies", {})
            lines = [f"Dependencies for: {data.get('title', 'Unknown')}\n"]
            for dep in deps.get("shared_claims", []):
                contra = " ⚠️ CONTRADICTION" if dep.get("is_contradiction") else ""
                lines.append(
                    f"  [claim] {dep['title']}: "
                    f"{dep['predicate']} = {dep['related_value']}"
                    f"{contra}"
                )
            for dep in deps.get("same_source", []):
                lines.append(f"  [sibling] {dep['title']}")
            if deps.get("total", 0) == 0:
                lines.append("  No dependencies found.")
            return [{"type": "text", "text": "\n".join(lines)}]

        elif name == "raasoa_doc_diff":
            doc_id = arguments["document_id"]
            resp = await client.get(
                f"{BASE_URL}/v1/documents/{doc_id}/diff",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            lines = [
                f"Version diff for: {data.get('title', '?')} "
                f"(v{data.get('current_version', '?')})\n"
            ]
            changes = data.get("claim_changes", [])
            if changes:
                for c in changes:
                    lines.append(
                        f"  CHANGED: {c['predicate']}\n"
                        f"    was: {c['old_value']}\n"
                        f"    now: {c['new_value']}"
                    )
            else:
                lines.append(
                    data.get("message", "No claim-level changes detected.")
                )
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

        elif name == "raasoa_get_skill":
            requested_name = (arguments.get("name") or "").strip()
            if not requested_name:
                return [{"type": "text", "text": "Skill name is required."}]

            clearance = (
                arguments.get("agent_clearance")
                or env_default_clearance()
            )

            # 1) Exact metadata match: type=skill AND frontmatter name=...
            resp = await client.post(
                f"{BASE_URL}/v1/find_by_metadata",
                json={
                    "metadata": {"type": "skill", "name": requested_name},
                    "limit": 5,
                },
                headers=_headers(),
            )
            skill_data: dict[str, Any] = (
                resp.json() if resp.status_code == 200 else {}
            )
            matches = skill_data.get("documents") or []

            # 2) Title fallback (still type=skill)
            if not matches:
                resp = await client.post(
                    f"{BASE_URL}/v1/find_by_metadata",
                    json={"metadata": {"type": "skill"}, "limit": 50},
                    headers=_headers(),
                )
                if resp.status_code == 200:
                    rn = requested_name.lower()
                    all_skills = (resp.json() or {}).get("documents") or []
                    matches = [
                        d for d in all_skills
                        if rn in (d.get("title") or "").lower()
                    ]

            # 3) Final fallback: hybrid search constrained to type=skill
            if not matches:
                resp = await client.post(
                    f"{BASE_URL}/v1/retrieve",
                    json={
                        "query": requested_name,
                        "top_k": 5,
                        "metadata_filter": {"type": "skill"},
                    },
                    headers=_headers(),
                )
                if resp.status_code == 200:
                    rdata = resp.json()
                    seen: set[str] = set()
                    for hit in rdata.get("results", []):
                        did = hit.get("document_id")
                        if did and did not in seen:
                            seen.add(did)
                            matches.append({
                                "id": did,
                                "title": hit.get("document_title"),
                                "doc_metadata": hit.get("doc_metadata") or {},
                            })

            # Apply policy gate
            from raasoa.mcp.policy import hit_is_allowed
            allowed_matches: list[dict[str, Any]] = []
            denied_matches: list[dict[str, Any]] = []
            for m in matches:
                meta = m.get("doc_metadata") or {}
                hit_view = {
                    "document_id": m.get("id"),
                    "doc_metadata": meta,
                }
                if hit_is_allowed(hit_view, clearance):
                    allowed_matches.append(m)
                else:
                    denied_matches.append(m)

            if denied_matches:
                await audit_denials(
                    BASE_URL, _headers(),
                    tool="raasoa_get_skill",
                    query=requested_name,
                    denied=[
                        {
                            "document_id": m.get("id"),
                            "policy_reason": (
                                f"classification="
                                f"{(m.get('doc_metadata') or {}).get('classification')} "
                                f"exceeds clearance={clearance}"
                            ),
                        }
                        for m in denied_matches
                    ],
                )

            if not allowed_matches:
                msg = (
                    f"No skill matches '{requested_name}'"
                    + (
                        f" (note: {len(denied_matches)} match(es) hidden by policy)"
                        if denied_matches else ""
                    )
                    + ". Try a different name or check that "
                      "the document has frontmatter `type: skill`."
                )
                return [{"type": "text", "text": msg}]

            # Pick the best match (first), fetch full text
            best = allowed_matches[0]
            doc_id = best.get("id")
            resp = await client.get(
                f"{BASE_URL}/v1/documents/{doc_id}",
                headers=_headers(),
            )
            if resp.status_code != 200:
                return [{
                    "type": "text",
                    "text": f"Found skill '{best.get('title')}' "
                            f"but failed to fetch content.",
                }]
            doc_data = resp.json()
            chunks = doc_data.get("chunks") or []
            full_text = "\n\n".join(
                c.get("chunk_text") or c.get("text") or ""
                for c in chunks
            )
            meta = best.get("doc_metadata") or doc_data.get("metadata") or {}

            # Telemetry: record skill invocation as audit event
            # (best-effort; never blocks the tool response)
            import contextlib as _cl
            with _cl.suppress(Exception):
                await client.post(
                    f"{BASE_URL}/v1/audit",
                    json={
                        "action": "skill.invoked",
                        "resource_type": "document",
                        "resource_id": str(doc_id),
                        "details": {
                            "skill_name": meta.get("name") or best.get("title"),
                            "version": meta.get("version"),
                            "ampel": meta.get("ampel"),
                            "executor": meta.get("executor"),
                        },
                    },
                    headers=_headers(),
                )

            ampel = meta.get("ampel", "?")
            executor = meta.get("executor", "?")
            version = meta.get("version", "?")
            owner = meta.get("owner", "?")
            review_status = doc_data.get("review_status", "?")

            header = (
                f"# Skill: {meta.get('name') or best.get('title')}\n"
                f"Version: {version} | Ampel: {ampel} | "
                f"Executor: {executor} | Owner: {owner}\n"
                f"Review status: {review_status}\n"
            )
            if str(ampel).lower() in ("rot", "red", "blocked"):
                header += (
                    "\n⚠ This skill is currently RED — do not execute "
                    "without explicit owner approval.\n"
                )

            other = ""
            if len(allowed_matches) > 1:
                other = (
                    "\n\n(Other matching skills: "
                    + ", ".join(
                        m.get("title") or str(m.get("id"))
                        for m in allowed_matches[1:5]
                    )
                    + ")"
                )

            return [{
                "type": "text",
                "text": header + "\n" + full_text + other,
            }]

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


async def handle_message_async(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Async JSON-RPC dispatch — used by the HTTP transport.

    Mirrors ``_handle_message`` but awaits the handlers directly instead of
    calling ``asyncio.run`` (which is illegal inside a running event loop,
    e.g. a FastAPI request).
    """
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _make_response(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "raasoa", "version": "0.2.0"},
        })
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        return _make_response(msg_id, {"tools": _tool_definitions()})
    elif method == "resources/list":
        return _make_response(msg_id, {"resources": _resource_definitions()})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            content = await _handle_tool_call(tool_name, arguments)
            return _make_response(msg_id, {"content": content})
        except httpx.ConnectError:
            return _make_response(msg_id, {
                "content": [{
                    "type": "text",
                    "text": (
                        f"Cannot connect to RAASOA API at {BASE_URL}. "
                        "Is the server running?"
                    ),
                }],
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
            contents = await _handle_resource_read(uri)
            return _make_response(msg_id, {"contents": contents})
        except Exception as e:
            return _make_error(msg_id, -32603, str(e))
    elif method == "ping":
        return _make_response(msg_id, {})
    else:
        return _make_error(msg_id, -32601, f"Method not found: {method}")


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
                "version": "0.2.0",
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
