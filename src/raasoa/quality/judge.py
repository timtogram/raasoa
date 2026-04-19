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

JUDGE_PROMPT = """Two documents disagree about: {predicate}

Doc A "{title_a}" says: {value_a}
Doc B "{title_b}" says: {value_b}
{extra_context}
Which is correct? Newer documents and official decisions take priority.

Reply ONLY with JSON:
{{"recommendation":"keep_a","confidence":0.9,"reasoning":"why"}}"""


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

    raw_details = conflict.details
    details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details or {}

    # Extract claim info
    new_claim = details.get("new_claim", {})
    existing_claim = details.get("existing_claim", {})

    if not new_claim or not existing_claim:
        logger.debug(
            "Cannot judge conflict %s: no claim data (keys: %s)",
            conflict_id, list(details.keys()),
        )
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
        value_b=existing_claim.get("value", "?"),
        title_b=doc_b.get("title", details.get("existing_doc_title", "Document B")),
        extra_context=extra,
    )

    # Use dedicated judge model if configured, else fall back to chat model
    judge_model = settings.llm_judge_model or settings.ollama_chat_model

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": judge_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 1024},
                },
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()

            # Strip thinking tags
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

            # Strip markdown code blocks
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]

            # Parse JSON (handle truncation)
            start = raw.find("{")
            if start == -1:
                logger.warning("Judge returned no JSON: %s", raw[:100])
                return None

            json_str = raw[start:]
            end = json_str.rfind("}")
            if end > 0:
                json_str = json_str[: end + 1]
            else:
                # Truncated — try to close it
                # Find last complete key:value and close
                last_quote = json_str.rfind('"')
                if last_quote > 0:
                    json_str = json_str[: last_quote + 1] + "}"

            try:
                verdict_data = json.loads(json_str)
            except json.JSONDecodeError:
                logger.warning("Judge JSON parse failed: %s", json_str[:200])
                return None

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
