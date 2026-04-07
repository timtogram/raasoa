"""LLM-powered Knowledge Index Curator.

Periodically reviews and optimizes the knowledge base:
1. Normalize: Merge equivalent predicates ("BI tool" = "visualization platform")
2. Deduplicate: Collapse identical claims from different chunks
3. Enrich: Infer missing relationships from existing claims
4. Lint: Flag inconsistencies, gaps, and stale entries

This is Karpathy's "lint and maintain" phase, adapted for enterprise:
instead of maintaining a personal wiki, the LLM curates a shared
knowledge index that serves many agents.

Runs as a background task — not in the hot retrieval path.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings

logger = logging.getLogger(__name__)

NORMALIZE_PROMPT = """/no_think
You are a knowledge curator. Given a list of claim predicates from a knowledge base,
your job is to normalize them so that predicates about the SAME concept use the SAME wording.

Rules:
- Group predicates that mean the same thing
- Choose the clearest, most descriptive canonical form for each group
- Keep the language of the original (German stays German, English stays English)
- Return a JSON object mapping original predicates to their canonical form
- Only include predicates that need normalizing (skip already-good ones)

Example input:
["primary BI tool", "main visualization platform", "central data visualization and BI tool",
 "Haupt-BI-Werkzeug", "vacation days per year", "annual leave allowance"]

Example output:
{
  "primary BI tool": "primary data visualization and BI tool",
  "main visualization platform": "primary data visualization and BI tool",
  "Haupt-BI-Werkzeug": "primary data visualization and BI tool",
  "annual leave allowance": "vacation days per year"
}

Predicates to normalize:
INPUT_PREDICATES

JSON mapping (only predicates that need normalizing):"""

LINT_PROMPT = """/no_think
You are a knowledge auditor. Given a set of knowledge entries (subject, predicate, value),
identify issues:

1. CONTRADICTIONS: Entries where the same subject+predicate has different values
2. STALE: Entries that look outdated (references to past dates, deprecated terms)
3. GAPS: Missing information that would be expected given related entries
4. DUPLICATES: Entries that are essentially the same fact stated differently

Return a JSON array of findings. Each finding has:
- type: "contradiction" | "stale" | "gap" | "duplicate"
- description: What the issue is
- affected_entries: List of entry indices involved
- suggestion: What to do about it

Entries:
INPUT_ENTRIES

JSON array of findings:"""


async def _call_llm(prompt: str) -> str:
    """Call the Ollama LLM for curation tasks."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_chat_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 4096},
            },
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


async def normalize_predicates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """LLM-driven predicate normalization.

    Finds predicates that mean the same thing and normalizes them
    to a canonical form. This makes the knowledge index more consistent
    and improves lookup hit rates.
    """
    # Fetch distinct predicates
    result = await session.execute(
        text(
            "SELECT DISTINCT c.predicate "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid AND c.status = 'active' "
            "ORDER BY c.predicate"
        ),
        {"tid": tenant_id},
    )
    predicates = [r.predicate for r in result.fetchall()]

    if len(predicates) < 2:
        return {"predicates": len(predicates), "normalized": 0}

    # Ask LLM to find equivalent predicates
    prompt = NORMALIZE_PROMPT.replace(
        "INPUT_PREDICATES", json.dumps(predicates, ensure_ascii=False),
    )

    try:
        raw = await _call_llm(prompt)
        # Extract JSON
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end <= start:
            return {"predicates": len(predicates), "normalized": 0}

        mapping = json.loads(raw[start:end])
    except Exception:
        logger.warning("LLM predicate normalization failed", exc_info=True)
        return {"predicates": len(predicates), "normalized": 0, "error": True}

    # Apply normalizations
    normalized_count = 0
    for original, canonical in mapping.items():
        if original == canonical:
            continue
        result = await session.execute(
            text(
                "UPDATE claims SET predicate = :canonical "
                "WHERE predicate = :original "
                "AND document_id IN ("
                "  SELECT id FROM documents WHERE tenant_id = :tid"
                ") "
                "AND status = 'active'"
            ),
            {"canonical": canonical, "original": original, "tid": tenant_id},
        )
        normalized_count += result.rowcount  # type: ignore[attr-defined]

    if normalized_count > 0:
        await session.commit()

    logger.info(
        "Normalized %d claims across %d predicates",
        normalized_count, len(mapping),
    )
    return {
        "predicates": len(predicates),
        "normalized": normalized_count,
        "mappings": len(mapping),
    }


async def lint_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """LLM-driven knowledge audit.

    Reviews the knowledge index for contradictions, stale entries,
    gaps, and duplicates. Returns findings for human review.
    """
    # Fetch index entries
    result = await session.execute(
        text(
            "SELECT subject, predicate, value, confidence "
            "FROM knowledge_index "
            "WHERE tenant_id = :tid AND status = 'active' "
            "ORDER BY subject, predicate"
        ),
        {"tid": tenant_id},
    )
    entries = result.fetchall()

    if len(entries) < 2:
        return []

    entries_text = json.dumps(
        [
            {
                "index": i,
                "subject": e.subject,
                "predicate": e.predicate,
                "value": e.value,
                "confidence": e.confidence,
            }
            for i, e in enumerate(entries)
        ],
        ensure_ascii=False,
        indent=2,
    )

    prompt = LINT_PROMPT.replace("INPUT_ENTRIES", entries_text)

    try:
        raw = await _call_llm(prompt)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end <= start:
            return []
        findings = json.loads(raw[start:end])
        if not isinstance(findings, list):
            return []
        return findings
    except Exception:
        logger.warning("LLM lint failed", exc_info=True)
        return []


async def curate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict[str, Any]:
    """Run the full curation pipeline.

    1. Normalize predicates
    2. Rebuild knowledge index
    3. Lint for issues
    """
    from raasoa.retrieval.knowledge_index import build_index

    # Step 1: Normalize
    norm_stats = await normalize_predicates(session, tenant_id)

    # Step 2: Rebuild index (with normalized predicates)
    index_stats = await build_index(session, tenant_id)

    # Step 3: Lint
    findings = await lint_knowledge(session, tenant_id)

    return {
        "normalization": norm_stats,
        "index": index_stats,
        "lint_findings": len(findings),
        "findings": findings,
    }
