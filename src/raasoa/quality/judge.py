"""LLM-as-Judge for automated conflict resolution.

Evaluates conflicting claims and recommends which to keep.
Can auto-resolve conflicts above a configurable confidence threshold.

The judge considers:
1. Temporal recency (newer document wins if same topic)
2. Source authority (official policy > meeting notes)
3. Specificity (detailed claim > vague claim)
4. Internal consistency (claim that fits other known facts)

Usage:
    # Get recommendation without auto-resolving
    verdict = await judge_conflict(session, conflict_id, tenant_id)

    # Auto-resolve if above threshold
    await auto_resolve_conflicts(session, tenant_id)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """/no_think
You are a knowledge governance judge. Two documents contain conflicting claims about the same topic.

Your job: decide which claim is more likely CORRECT and CURRENT.

Consider:
1. RECENCY: Is one document newer? Newer policies supersede older ones.
2. AUTHORITY: Official policies > meeting notes > informal docs.
3. SPECIFICITY: "Budget is 420,000 EUR" > "Budget is around 400k".
4. CONSISTENCY: Does one claim fit better with other known facts?

Conflict:
- Topic: {predicate}
- Claim A: "{value_a}" (from: {title_a}, source: {source_a})
  Evidence: {evidence_a}
- Claim B: "{value_b}" (from: {title_b}, source: {source_b})
  Evidence: {evidence_b}

{extra_context}

Respond with ONLY this JSON (no other text):
{{
  "recommendation": "keep_a" or "keep_b" or "keep_both",
  "confidence": 0.0 to 1.0,
  "reasoning": "Brief explanation why"
}}"""


@dataclass
class JudgeVerdict:
    """Result of LLM judge evaluation."""

    conflict_id: uuid.UUID
    recommendation: str  # keep_a, keep_b, keep_both
    confidence: float
    reasoning: str
    auto_resolved: bool = False


async def judge_conflict(
    session: AsyncSession,
    conflict_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> JudgeVerdict | None:
    """Ask the LLM to evaluate a conflict and recommend resolution."""
    # Fetch conflict details
    result = await session.execute(
        text(
            "SELECT cc.id, cc.document_a_id, cc.document_b_id, "
            "cc.details, cc.confidence "
            "FROM conflict_candidates cc "
            "WHERE cc.id = :cid AND cc.tenant_id = :tid "
            "AND cc.status = 'new'"
        ),
        {"cid": conflict_id, "tid": tenant_id},
    )
    conflict = result.first()
    if not conflict:
        return None

    details = conflict.details or {}

    # Extract claim info
    new_claim = details.get("new_claim", {})
    existing_claim = details.get("existing_claim", {})

    if not new_claim or not existing_claim:
        # Non-claim conflict (embedding-based) — less data to judge
        return None

    # Fetch document metadata for context
    docs = {}
    for did in [conflict.document_a_id, conflict.document_b_id]:
        doc_result = await session.execute(
            text(
                "SELECT d.title, d.created_at, d.version, "
                "s.source_type, s.name AS source_name "
                "FROM documents d "
                "LEFT JOIN sources s ON d.source_id = s.id "
                "WHERE d.id = :did"
            ),
            {"did": did},
        )
        row = doc_result.first()
        if row:
            docs[str(did)] = {
                "title": row.title or "Untitled",
                "created_at": str(row.created_at),
                "version": row.version,
                "source_type": row.source_type or "unknown",
                "source_name": row.source_name or "unknown",
            }

    doc_a = docs.get(str(conflict.document_a_id), {})
    doc_b = docs.get(str(conflict.document_b_id), {})

    # Build extra context
    extra = ""
    if doc_a.get("created_at") and doc_b.get("created_at"):
        extra += (
            f"Document A created: {doc_a['created_at']}, "
            f"Document B created: {doc_b['created_at']}. "
        )

    prompt = JUDGE_PROMPT.format(
        predicate=new_claim.get("predicate", "unknown"),
        value_a=new_claim.get("value", "?"),
        title_a=doc_a.get("title", "Document A"),
        source_a=doc_a.get("source_name", "unknown"),
        evidence_a=(new_claim.get("evidence", "")[:300]),
        value_b=existing_claim.get("value", "?"),
        title_b=doc_b.get("title", details.get("existing_doc_title", "Document B")),
        source_b=doc_b.get("source_name", "unknown"),
        evidence_b=(existing_claim.get("evidence", "")[:300]),
        extra_context=extra,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.ollama_chat_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 256},
                },
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()

            # Strip thinking tags
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

            # Parse JSON
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end <= start:
                logger.warning("Judge returned non-JSON: %s", raw[:100])
                return None

            verdict_data = json.loads(raw[start:end])

            recommendation = verdict_data.get("recommendation", "keep_both")
            if recommendation not in ("keep_a", "keep_b", "keep_both"):
                recommendation = "keep_both"

            confidence = float(verdict_data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            # Store verdict in conflict details
            await session.execute(
                text(
                    "UPDATE conflict_candidates "
                    "SET details = COALESCE(details, CAST('{}' AS jsonb)) "
                    "|| CAST(:verdict AS jsonb) "
                    "WHERE id = :cid"
                ),
                {
                    "cid": conflict_id,
                    "verdict": json.dumps({
                        "llm_judge": {
                            "recommendation": recommendation,
                            "confidence": confidence,
                            "reasoning": verdict_data.get("reasoning", ""),
                        },
                    }),
                },
            )
            await session.commit()

            return JudgeVerdict(
                conflict_id=conflict_id,
                recommendation=recommendation,
                confidence=confidence,
                reasoning=verdict_data.get("reasoning", ""),
            )

    except Exception:
        logger.warning("LLM judge failed for conflict %s", conflict_id, exc_info=True)
        return None


async def auto_resolve_conflicts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Judge all open conflicts and auto-resolve those above threshold.

    Returns stats: judged, auto_resolved, kept_for_human.
    """
    if threshold is None:
        threshold = settings.llm_judge_auto_resolve_threshold

    # Find open claim-based conflicts
    result = await session.execute(
        text(
            "SELECT id FROM conflict_candidates "
            "WHERE tenant_id = :tid AND status = 'new' "
            "AND conflict_type = 'claim_contradiction' "
            "ORDER BY confidence DESC "
            "LIMIT 50"
        ),
        {"tid": tenant_id},
    )
    conflict_ids = [r.id for r in result.fetchall()]

    stats: dict[str, Any] = {
        "total_open": len(conflict_ids),
        "judged": 0,
        "auto_resolved": 0,
        "kept_for_human": 0,
        "verdicts": [],
    }

    for cid in conflict_ids:
        verdict = await judge_conflict(session, cid, tenant_id)
        if not verdict:
            continue

        stats["judged"] += 1
        verdict_info = {
            "conflict_id": str(cid),
            "recommendation": verdict.recommendation,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
            "auto_resolved": False,
        }

        # Auto-resolve if confidence above threshold
        if verdict.confidence >= threshold and verdict.recommendation in (
            "keep_a", "keep_b",
        ):
            # Get conflict to find doc IDs
            c_result = await session.execute(
                text(
                    "SELECT document_a_id, document_b_id "
                    "FROM conflict_candidates WHERE id = :cid"
                ),
                {"cid": cid},
            )
            conflict = c_result.first()
            if conflict:
                superseded_doc = (
                    conflict.document_b_id
                    if verdict.recommendation == "keep_a"
                    else conflict.document_a_id
                )

                # Supersede the losing document
                await session.execute(
                    text(
                        "UPDATE documents "
                        "SET review_status = 'superseded' "
                        "WHERE id = :did AND tenant_id = :tid"
                    ),
                    {"did": superseded_doc, "tid": tenant_id},
                )
                await session.execute(
                    text(
                        "UPDATE claims SET status = 'superseded' "
                        "WHERE document_id = :did"
                    ),
                    {"did": superseded_doc},
                )

                # Mark conflict as resolved
                resolution_data = json.dumps({
                    "resolution": verdict.recommendation,
                    "resolved_by": "llm_judge",
                    "confidence": verdict.confidence,
                    "reasoning": verdict.reasoning,
                })
                await session.execute(
                    text(
                        "UPDATE conflict_candidates "
                        "SET status = 'resolved', "
                        "details = COALESCE(details, CAST('{}' AS jsonb)) "
                        "|| CAST(:res AS jsonb) "
                        "WHERE id = :cid"
                    ),
                    {"cid": cid, "res": resolution_data},
                )

                # Close related review tasks
                await session.execute(
                    text(
                        "UPDATE review_tasks "
                        "SET status = 'approved', completed_at = now() "
                        "WHERE conflict_id = :cid AND status = 'new'"
                    ),
                    {"cid": cid},
                )

                await session.commit()
                stats["auto_resolved"] += 1
                verdict_info["auto_resolved"] = True

                logger.info(
                    "Auto-resolved conflict %s: %s (%.0f%% confidence)",
                    cid, verdict.recommendation, verdict.confidence * 100,
                )
        else:
            stats["kept_for_human"] += 1

        stats["verdicts"].append(verdict_info)

    return stats
