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
  {{"subject": "Company", "predicate": "official data visualization and BI tool", "object_value": "Power BI", "confidence": 0.9}},
  {{"subject": "HR Policy", "predicate": "minimum vacation request notice period", "object_value": "14 days", "confidence": 0.85}}
]

Text:
---
{text}
---

JSON array:"""


async def extract_claims_from_text(
    text: str,
    base_url: str = settings.ollama_base_url,
    model: str = settings.ollama_chat_model,
) -> list[dict[str, Any]]:
    """Call Ollama to extract claims from a text passage."""
    prompt = CLAIM_EXTRACTION_PROMPT.format(text=text[:4000])

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 4096},
                },
            )
            response.raise_for_status()
            data = response.json()
            raw_response = data.get("response", "").strip()

            # Strip thinking tags from models like qwen3
            import re

            raw_response = re.sub(
                r"<think>.*?</think>", "", raw_response, flags=re.DOTALL
            ).strip()

            # Parse JSON from response — handle markdown code blocks
            if "```json" in raw_response:
                raw_response = raw_response.split("```json")[1].split("```")[0]
            elif "```" in raw_response:
                raw_response = raw_response.split("```")[1].split("```")[0]

            # Find the JSON array in the response
            start = raw_response.find("[")
            end = raw_response.rfind("]") + 1
            if start == -1:
                return []

            json_str = raw_response[start:end] if end > start else raw_response[start:]
            # Handle truncated JSON — try to close the array
            if not json_str.endswith("]"):
                # Remove incomplete last object and close
                last_brace = json_str.rfind("}")
                if last_brace > 0:
                    json_str = json_str[: last_brace + 1] + "]"
                else:
                    return []

            claims = json.loads(json_str)
            if not isinstance(claims, list):
                return []

            # Validate structure
            valid_claims = []
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
                    })
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

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _extract_one(
        chunk_id: uuid.UUID, chunk_text: str,
    ) -> list[dict[str, Any]]:
        async with semaphore:
            raw = await extract_claims_from_text(chunk_text)
            return [
                {**rc, "chunk_id": chunk_id, "evidence": chunk_text[:500]}
                for rc in raw
            ]

    tasks = [_extract_one(cid, text) for cid, text in eligible]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_claims: list[Claim] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Claim extraction task failed: %s", result)
            continue
        for rc in result:
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
