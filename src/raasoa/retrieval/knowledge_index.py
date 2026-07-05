"""Knowledge Index — materialized lookup for factual queries.

Compiles claims into a normalized index that enables sub-millisecond
answers for entity-attribute-value questions. No embedding needed.

The index is the "fast path" in retrieval:
1. Parse query into (subject?, predicate?) pattern
2. Look up in knowledge_index
3. If found → return instantly with 100% confidence
4. If not → fall through to hybrid search

Normalization:
- Lowercase, strip whitespace
- Remove common filler words
- Collapse synonyms (basic)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Filler words to strip during normalization
_FILLER = {
    "the", "a", "an", "of", "for", "in", "to", "and", "or", "is",
    "are", "was", "were", "our", "their", "its", "das", "die", "der",
    "ein", "eine", "und", "oder", "für", "von", "im", "am",
}


def normalize(text_val: str) -> str:
    """Normalize a string for index lookup.

    Lowercase, strip filler words, collapse whitespace.
    """
    words = re.sub(r"[^\w\s]", " ", text_val.lower()).split()
    words = [w for w in words if w not in _FILLER]
    return " ".join(words).strip()


@dataclass
class IndexEntry:
    """A single entry in the knowledge index."""

    subject: str
    predicate: str
    value: str
    confidence: float
    source_documents: list[str]
    valid_from: str | None = None
    valid_until: str | None = None
    status: str = "active"


@dataclass
class IndexLookupResult:
    """Result of a knowledge index lookup."""

    found: bool
    entries: list[IndexEntry]
    query_subject: str | None = None
    query_predicate: str | None = None


# Shared WHERE clause fragment: "is this claim eligible for the fast-path
# knowledge index" — active claim, live (non-deleted/quarantined/etc.)
# document, and not restricted/ACL-protected. See build_index's docstring
# for the ACL-exclusion rationale. Used by both the full rebuild and the
# incremental per-document update so the two stay in sync.
_ELIGIBLE_CLAIMS_WHERE = (
    "  AND c.status = 'active' "
    "  AND d.review_status NOT IN "
    "    ('quarantined', 'rejected', 'superseded', 'deleted') "
    "  AND src.default_visibility != 'restricted' "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM acl_entries ae WHERE ae.document_id = d.id"
    "  ) "
)


def _group_claims(claims: Sequence[Any]) -> dict[tuple[str, str], list[Any]]:
    """Group claim rows by normalized (subject, predicate).

    Rows must already be ordered by confidence DESC within each
    (subject, predicate) group so that group[0] is the winner.
    """
    groups: dict[tuple[str, str], list[Any]] = {}
    for c in claims:
        key = (normalize(c.subject), normalize(c.predicate))
        groups.setdefault(key, []).append(c)
    return groups


async def _insert_index_entries(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    groups: dict[tuple[str, str], list[Any]],
) -> int:
    """Insert one winning index entry per (subject, predicate) group.

    Assumes any prior rows for these keys have already been deleted by
    the caller — this only inserts, it never deletes/upserts itself.
    """
    count = 0
    for (subj_norm, pred_norm), group in groups.items():
        # Pick highest-confidence value
        best = group[0]
        claim_ids = [str(c.claim_id) for c in group]
        doc_ids = list({str(c.document_id) for c in group})

        await session.execute(
            text(
                "INSERT INTO knowledge_index "
                "(id, tenant_id, subject, subject_normalized, "
                " predicate, predicate_normalized, value, "
                " source_claim_ids, source_document_ids, "
                " confidence, claim_count, status) "
                "VALUES (:id, :tid, :subj, :subj_n, "
                " :pred, :pred_n, :val, "
                " CAST(:claim_ids AS jsonb), "
                " CAST(:doc_ids AS jsonb), "
                " :conf, :count, 'active')"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "subj": best.subject,
                "subj_n": subj_norm,
                "pred": best.predicate,
                "pred_n": pred_norm,
                "val": best.object_value,
                "claim_ids": json.dumps(claim_ids),
                "doc_ids": json.dumps(doc_ids),
                "conf": best.confidence,
                "count": len(group),
            },
        )
        count += 1
    return count


async def build_index(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """Rebuild the knowledge index from active claims.

    Groups claims by (subject, predicate), picks the highest-confidence
    value, and upserts into the index table.

    Claims from a 'restricted'-visibility source, OR from any document
    with its own acl_entries grants (regardless of the source's
    default_visibility — a document with an ACL entry is visible only to
    a matching principal, never by default; see
    raasoa.security.principal.acl_predicate_sql), are excluded entirely.
    This fast-lookup index has no per-query principal awareness (it's
    meant to answer factual queries in <5ms, not run an ACL join per
    lookup), so any ACL-protected fact simply never enters it. Callers
    still reach such documents through hybrid_search()/structured_query(),
    which do apply per-principal ACL filtering. This is a deliberate
    tradeoff, not an oversight: without it, a restricted document's
    extracted claims (e.g. "Acme Corp: deal_amount = $50,000") would be
    answerable via the knowledge index layer regardless of who's asking.

    This performs a full tenant-wide rebuild (DELETE + re-insert of every
    entry) and is intended for the standalone build_index worker job /
    manual admin use. For per-ingest updates, use
    update_index_for_document() instead — rebuilding the entire tenant
    index on every document ingest is O(total tenant claims) per ingest
    and unsafe under concurrent ingests for the same tenant (interleaved
    DELETE/INSERT can duplicate or drop entries).
    """
    # Fetch all active claims grouped by subject + predicate
    result = await session.execute(
        text(
            "SELECT c.subject, c.predicate, c.object_value, "
            "  c.confidence, c.id as claim_id, c.document_id "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "JOIN sources src ON src.id = d.source_id "
            "WHERE d.tenant_id = :tid "
            f"{_ELIGIBLE_CLAIMS_WHERE}"
            "ORDER BY c.subject, c.predicate, c.confidence DESC"
        ),
        {"tid": tenant_id},
    )
    claims = result.fetchall()

    if not claims:
        return {"entries": 0, "claims_processed": 0}

    # Group by normalized (subject, predicate)
    groups = _group_claims(claims)

    # Clear existing index for this tenant
    await session.execute(
        text("DELETE FROM knowledge_index WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )

    # Build index entries
    count = await _insert_index_entries(session, tenant_id, groups)

    await session.commit()
    logger.info(
        "Built knowledge index: %d entries from %d claims (tenant %s)",
        count, len(claims), tenant_id,
    )
    return {"entries": count, "claims_processed": len(claims)}


async def update_index_for_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> dict[str, int]:
    """Incrementally update the knowledge index for one newly-ingested
    document, instead of rebuilding the whole tenant index.

    Only the (subject, predicate) groups that this document's claims
    could affect are recomputed:

    1. Find the normalized (subject, predicate) keys touched by this
       document's own (eligible) claims.
    2. Also include any existing knowledge_index keys whose
       source_document_ids already reference this document — this
       covers re-ingest/update, where a key the document used to win
       must be re-evaluated even if the new claim set no longer
       produces that exact key (e.g. claim was retracted).
    3. Delete only the knowledge_index rows for that key set (scoped by
       tenant_id), then recompute each key from ALL of the tenant's
       currently-eligible claims for that (subject, predicate) — not
       just this document's claims — so a claim from another document
       that wins the group is preserved, and a claim from this document
       that wins the group is correctly inserted.

    This keeps ingest-time cost proportional to the number of distinct
    (subject, predicate) keys in the new document rather than the total
    number of claims in the tenant, and avoids the delete-then-rebuild
    race that two concurrent ingests for the same tenant would hit
    against build_index().
    """
    # Step 1: keys touched by this document's own eligible claims.
    own_result = await session.execute(
        text(
            "SELECT c.subject, c.predicate "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "JOIN sources src ON src.id = d.source_id "
            "WHERE d.tenant_id = :tid AND c.document_id = :did "
            f"{_ELIGIBLE_CLAIMS_WHERE}"
        ),
        {"tid": tenant_id, "did": document_id},
    )
    affected_keys: set[tuple[str, str]] = {
        (normalize(r.subject), normalize(r.predicate)) for r in own_result.fetchall()
    }

    # Step 2: keys already in the index that reference this document
    # (covers updates/retractions where the doc no longer produces a
    # key it used to hold the winning claim for).
    existing_result = await session.execute(
        text(
            "SELECT subject_normalized, predicate_normalized "
            "FROM knowledge_index "
            "WHERE tenant_id = :tid "
            "  AND source_document_ids @> CAST(:doc_id_json AS jsonb)"
        ),
        {"tid": tenant_id, "doc_id_json": json.dumps([str(document_id)])},
    )
    for r in existing_result.fetchall():
        affected_keys.add((r.subject_normalized, r.predicate_normalized))

    if not affected_keys:
        return {"entries_updated": 0, "claims_processed": 0}

    # Step 3: pull the tenant's eligible claims and keep only those
    # belonging to an affected (subject, predicate) key - from ANY
    # document, not just this one - so a claim from another, untouched
    # document can still correctly win (or lose) the group.
    #
    # normalize() is a Python function, not SQL, so the key match can't
    # be pushed into the WHERE clause directly. Correctness and the
    # concurrency fix both come from scoping the DELETE/INSERT below to
    # only the affected keys, not from filtering this SELECT - the
    # SELECT itself scans per-tenant eligible claims same as
    # build_index would, but only entries for the affected keys are
    # ever written.
    all_result = await session.execute(
        text(
            "SELECT c.subject, c.predicate, c.object_value, "
            "  c.confidence, c.id as claim_id, c.document_id "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "JOIN sources src ON src.id = d.source_id "
            "WHERE d.tenant_id = :tid "
            f"{_ELIGIBLE_CLAIMS_WHERE}"
            "ORDER BY c.subject, c.predicate, c.confidence DESC"
        ),
        {"tid": tenant_id},
    )
    all_claims = all_result.fetchall()

    scoped_claims = [
        c for c in all_claims
        if (normalize(c.subject), normalize(c.predicate)) in affected_keys
    ]

    groups = _group_claims(scoped_claims)

    # Delete only the affected keys' existing rows, scoped to tenant -
    # never a tenant-wide DELETE, so a concurrent ingest touching a
    # different (subject, predicate) key is unaffected. Matches key
    # pairs exactly (not independent subject/predicate membership) via
    # a JSON array of the affected (subject, predicate) tuples.
    key_rows = [{"subj": s, "pred": p} for s, p in affected_keys]
    await session.execute(
        text(
            "DELETE FROM knowledge_index "
            "WHERE tenant_id = :tid "
            "  AND (subject_normalized, predicate_normalized) IN ("
            "    SELECT (x->>'subj'), (x->>'pred') "
            "    FROM jsonb_array_elements(CAST(:keys AS jsonb)) AS x"
            "  )"
        ),
        {"tid": tenant_id, "keys": json.dumps(key_rows)},
    )

    count = await _insert_index_entries(session, tenant_id, groups)

    await session.commit()
    logger.info(
        "Incrementally updated knowledge index: %d entries for %d affected "
        "keys from document %s (tenant %s)",
        count, len(affected_keys), document_id, tenant_id,
    )
    return {"entries_updated": count, "claims_processed": len(scoped_claims)}


async def lookup(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
) -> IndexLookupResult:
    """Look up a query in the knowledge index.

    Tries to match the query against normalized predicates.
    Returns matching entries sorted by confidence.
    """
    query_normalized = normalize(query)

    if not query_normalized:
        return IndexLookupResult(found=False, entries=[])

    # Strategy 1: Full-text match on predicate
    result = await session.execute(
        text(
            "SELECT subject, predicate, value, confidence, "
            "  source_document_ids, status "
            "FROM knowledge_index "
            "WHERE tenant_id = :tid "
            "  AND status = 'active' "
            "  AND predicate_normalized LIKE :pattern "
            "ORDER BY confidence DESC "
            "LIMIT 5"
        ),
        {"tid": tenant_id, "pattern": f"%{query_normalized}%"},
    )
    rows = result.fetchall()

    if not rows:
        # Strategy 2: Match any word from query against predicates
        words = query_normalized.split()
        if len(words) >= 2:
            # Use the two most significant words
            pattern = "%".join(words[-2:])
            result = await session.execute(
                text(
                    "SELECT subject, predicate, value, confidence, "
                    "  source_document_ids, status "
                    "FROM knowledge_index "
                    "WHERE tenant_id = :tid "
                    "  AND status = 'active' "
                    "  AND predicate_normalized LIKE :pattern "
                    "ORDER BY confidence DESC "
                    "LIMIT 5"
                ),
                {"tid": tenant_id, "pattern": f"%{pattern}%"},
            )
            rows = result.fetchall()

    if not rows:
        return IndexLookupResult(
            found=False, entries=[],
            query_predicate=query_normalized,
        )

    entries = [
        IndexEntry(
            subject=r.subject,
            predicate=r.predicate,
            value=r.value,
            confidence=r.confidence,
            source_documents=r.source_document_ids or [],
            status=r.status,
        )
        for r in rows
    ]

    return IndexLookupResult(
        found=True,
        entries=entries,
        query_predicate=query_normalized,
    )
