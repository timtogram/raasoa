"""Query Router: Routes queries to the optimal retrieval strategy.

- RAG:        Knowledge questions → hybrid search + context assembly
- Structured: Factual/aggregation questions → direct SQL queries on metadata
- Hybrid:     Complex queries → both strategies combined

Routing is rule-based (fast, no LLM needed). Falls back to RAG for ambiguous queries.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class QueryType(StrEnum):
    RAG = "rag"
    STRUCTURED = "structured"


@dataclass
class RoutingDecision:
    query_type: QueryType
    confidence: float
    reason: str


# Patterns that indicate structured/metadata queries
STRUCTURED_PATTERNS = [
    (r"\bhow many\b", "aggregation query"),
    (r"\bcount\b.*\bdocuments?\b", "document count query"),
    (r"\blist all\b", "listing query"),
    (r"\bwhat documents?\b.*\babout\b", "document search"),
    (r"\bwhich documents?\b", "document filter"),
    (r"\blatest\b.*\bdocuments?\b", "recency query"),
    (r"\brecent\b.*\bdocuments?\b", "recency query"),
    (r"\bstatus\b.*\bdocuments?\b", "status filter"),
    (r"\bquality score\b", "quality metric query"),
    (r"\bconflicts?\b.*\bbetween\b", "conflict query"),
    (r"\bwho (uploaded|created|modified)\b", "provenance query"),
]

# Patterns that strongly indicate RAG/knowledge queries
RAG_PATTERNS = [
    (r"\bwhat is\b", "definition query"),
    (r"\bhow (do|does|to|can)\b", "how-to query"),
    (r"\bexplain\b", "explanation query"),
    (r"\bwhy\b", "reasoning query"),
    (r"\bdescribe\b", "description query"),
    (r"\bwhat.*\bmean\b", "meaning query"),
    (r"\bdifference between\b", "comparison query"),
    (r"\btell me about\b", "knowledge query"),
]


def route_query(query: str) -> RoutingDecision:
    """Route a query to the appropriate retrieval strategy.

    Uses pattern matching to classify queries. Falls back to RAG for
    ambiguous queries since it provides the most complete answers.
    """
    query_lower = query.lower().strip()

    # Check structured patterns first
    for pattern, reason in STRUCTURED_PATTERNS:
        if re.search(pattern, query_lower):
            return RoutingDecision(
                query_type=QueryType.STRUCTURED,
                confidence=0.8,
                reason=reason,
            )

    # Check RAG patterns
    for pattern, reason in RAG_PATTERNS:
        if re.search(pattern, query_lower):
            return RoutingDecision(
                query_type=QueryType.RAG,
                confidence=0.9,
                reason=reason,
            )

    # Default to RAG for ambiguous queries
    return RoutingDecision(
        query_type=QueryType.RAG,
        confidence=0.5,
        reason="default_rag",
    )
