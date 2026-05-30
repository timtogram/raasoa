"""LLM-based claim extraction from document chunks.

Uses Ollama to extract factual claims (subject-predicate-object triples)
from chunk text. Claims are the foundation for contradiction detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.models.claim import Claim

logger = logging.getLogger(__name__)

CLAIM_EXTRACTION_PROMPT = """/no_think
Extract factual claims from the following text as structured triples.

A claim has exactly these fields:
- subject: the organization, department, or entity (e.g., "Company", "IT Department", "HR Policy")
- predicate: a SPECIFIC, DESCRIPTIVE property — NEVER use generic verbs like "is", "has", "uses". Instead describe WHAT the relationship is about. Examples:
  GOOD: "official data visualization tool", "standard BI platform", "vacation notice period in days", "P1 ticket response time"
  BAD: "is", "has", "uses", "platform"
- object_value: the concrete value (e.g., "Power BI", "SAP Analytics Cloud", "14 days")
- confidence: 0.0-1.0
- valid_from: when this fact became true (e.g., "2026-01-01", "Q3 2026", "March 2025"). null if unknown.
- valid_until: when this fact stopped being true. null if still valid.

CRITICAL: The predicate must be descriptive enough that two claims about the SAME topic will have SIMILAR predicates even if the source text uses different words. For example:
- "Our main BI tool is Power BI" → predicate: "primary data visualization and BI tool"
- "We use SAP for all data visualization" → predicate: "primary data visualization and BI tool"
Both should produce a similar predicate because they describe the SAME organizational decision.

Rules:
- Only extract concrete, verifiable facts
- Works with any language (German, English, etc.)
- Return ONLY a JSON array, no other text
- If no claims, return []

Example:
[
  {{"subject": "Company", "predicate": "official data visualization and BI tool", "object_value": "Power BI", "confidence": 0.9, "valid_from": "2025-01-01", "valid_until": null}},
  {{"subject": "HR Policy", "predicate": "minimum vacation request notice period", "object_value": "14 days", "confidence": 0.85, "valid_from": null, "valid_until": null}}
]

Text:
---
{text}
---

JSON array:"""

REFINE_PROMPT = """/no_think
You already extracted these claims from a text passage:
{existing_claims}

Now re-read the SAME text and find claims you MISSED in the first pass.
Focus on: numbers, dates, deadlines, costs, responsibilities, tools,
policies, thresholds, and implicit facts.

Do NOT repeat claims you already found. Only return NEW claims.
Return a JSON array (empty if nothing new).

Text:
---
{text}
---

JSON array of NEW claims only:"""


def parse_claim_response(raw_response: str) -> list[dict[str, Any]]:
    """Parse an LLM ``/api/generate`` response into validated claim dicts.

    Pure, model-free, and defensive against the messy output of small
    reasoning models:
      - strips closed ``<think>…</think>`` blocks,
      - strips an *unclosed* leading think block (model ran out of budget
        mid-thought) by jumping to the first JSON delimiter,
      - unwraps markdown ``json`` code fences,
      - repairs a truncated array by closing after the last complete object.
    Returns ``[]`` on anything unparseable.
    """
    import re

    raw = (raw_response or "").strip()

    # Closed thinking blocks first …
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # … then an unclosed leading block: drop up to the first JSON delimiter.
    if "<think>" in raw and "</think>" not in raw:
        m = re.search(r"[\[{]", raw)
        raw = raw[m.start():] if m else ""

    # Markdown code fences
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]

    start = raw.find("[")
    if start == -1:
        return []
    end = raw.rfind("]") + 1
    json_str = raw[start:end] if end > start else raw[start:]

    # Repair truncated arrays — close after the last complete object.
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace > 0:
            json_str = json_str[: last_brace + 1] + "]"
        else:
            return []

    try:
        claims = json.loads(json_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(claims, list):
        return []

    valid_claims: list[dict[str, Any]] = []
    for c in claims:
        if (
            isinstance(c, dict)
            and "subject" in c
            and "predicate" in c
            and "object_value" in c
        ):
            valid_claims.append({
                "subject": str(c["subject"]),
                "predicate": str(c["predicate"]),
                "object_value": str(c["object_value"]),
                "confidence": float(c.get("confidence", 0.5)),
                "valid_from": str(c["valid_from"]) if c.get("valid_from") else None,
                "valid_until": str(c["valid_until"]) if c.get("valid_until") else None,
            })
    return valid_claims


async def extract_claims_from_text(
    text: str,
    base_url: str = settings.ollama_base_url,
    model: str = settings.ollama_chat_model,
    meter_tenant_id: str | None = None,
    existing_claims: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Call Ollama to extract claims from a text passage.

    If existing_claims is provided, runs a refinement pass that
    looks for claims missed in the first extraction.
    """
    if existing_claims:
        claims_str = json.dumps(existing_claims[:20], ensure_ascii=False)
        prompt = REFINE_PROMPT.format(
            existing_claims=claims_str, text=text[:4000],
        )
    else:
        prompt = CLAIM_EXTRACTION_PROMPT.format(text=text[:4000])

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    # Natively disable reasoning for qwen3/other thinking
                    # models. Without this, smaller variants (e.g. qwen3:4b)
                    # spend the whole num_predict budget inside <think> and
                    # never emit JSON. Ignored by models that don't think.
                    "think": False,
                    "options": {"temperature": 0.1, "num_predict": 4096},
                },
            )
            response.raise_for_status()
            data = response.json()
            valid_claims = parse_claim_response(data.get("response", ""))

            # Track LLM call for metering
            if meter_tenant_id:
                try:
                    import uuid as _uuid

                    from raasoa.db import async_session
                    from raasoa.middleware.metering import track_usage

                    async with async_session() as meter_session:
                        await track_usage(
                            meter_session,
                            _uuid.UUID(meter_tenant_id),
                            "llm_call",
                            1,
                            {"model": model, "purpose": "claim_extraction"},
                        )
                        await meter_session.commit()
                except Exception:
                    pass

            return valid_claims

    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Claim extraction failed: %s", e)
        return []


async def extract_and_store_claims(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    chunks: list[tuple[uuid.UUID, str]],  # (chunk_id, chunk_text)
    max_concurrent: int = 3,
) -> list[Claim]:
    """Extract claims from all chunks and store them in the database.

    Chunks are processed concurrently (up to max_concurrent) to reduce
    total wall-clock time for multi-chunk documents.
    """

    eligible = [
        (cid, text) for cid, text in chunks if len(text.strip()) >= 30
    ]
    if not eligible:
        return []

    # Cap at 50 chunks to prevent LLM overload on huge documents
    if len(eligible) > 50:
        logger.info(
            "Capping claim extraction to 50/%d chunks for doc %s",
            len(eligible), document_id,
        )
        eligible = eligible[:50]

    semaphore = asyncio.Semaphore(max_concurrent)
    num_passes = settings.claim_extraction_passes

    async def _extract_one(
        chunk_id: uuid.UUID, chunk_text: str,
    ) -> list[dict[str, Any]]:
        async with semaphore:
            # Pass 1: initial extraction
            raw = await extract_claims_from_text(
                chunk_text, meter_tenant_id=str(tenant_id),
            )
            all_raw = list(raw)

            # Pass 2+: refinement — find what was missed
            if num_passes >= 2 and raw:
                refine = await extract_claims_from_text(
                    chunk_text,
                    meter_tenant_id=str(tenant_id),
                    existing_claims=raw,
                )
                all_raw.extend(refine)

            return [
                {**rc, "chunk_id": chunk_id, "evidence": chunk_text[:500]}
                for rc in all_raw
            ]

    tasks = [_extract_one(cid, text) for cid, text in eligible]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect raw claims, dedup by (subject, predicate, value)
    seen: set[tuple[str, str, str]] = set()
    all_claims: list[Claim] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Claim extraction task failed: %s", result)
            continue
        for rc in result:
            dedup_key = (
                rc["subject"].lower().strip(),
                rc["predicate"].lower().strip(),
                rc["object_value"].lower().strip(),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            claim = Claim(
                tenant_id=tenant_id,
                document_id=document_id,
                chunk_id=rc["chunk_id"],
                subject=rc["subject"],
                predicate=rc["predicate"],
                object_value=rc["object_value"],
                confidence=rc["confidence"],
                evidence_span=rc["evidence"],
                status="active",
                valid_from=rc.get("valid_from"),
                valid_until=rc.get("valid_until"),
            )
            session.add(claim)
            all_claims.append(claim)

    if all_claims:
        await session.flush()

    logger.info(
        "Extracted %d claims from %d chunks for document %s",
        len(all_claims), len(eligible), document_id,
    )
    return all_claims
