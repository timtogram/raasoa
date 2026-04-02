"""Retrieval evaluation metrics.

Implements standard IR evaluation metrics for measuring retrieval quality:
- nDCG@k: Normalized Discounted Cumulative Gain
- Recall@k: Proportion of relevant documents retrieved
- Precision@k: Proportion of retrieved documents that are relevant
- MRR: Mean Reciprocal Rank
- Answerability: Whether the top results contain sufficient info to answer
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class RetrievalMetrics:
    """Evaluation metrics for a single query."""

    query: str
    ndcg_at_k: float = 0.0
    recall_at_k: float = 0.0
    precision_at_k: float = 0.0
    mrr: float = 0.0
    answerable: bool = False
    k: int = 5
    relevant_found: int = 0
    relevant_total: int = 0
    retrieved_count: int = 0


@dataclass
class EvalSummary:
    """Aggregated evaluation metrics across all queries."""

    total_queries: int = 0
    mean_ndcg: float = 0.0
    mean_recall: float = 0.0
    mean_precision: float = 0.0
    mean_mrr: float = 0.0
    answerability_rate: float = 0.0
    per_query: list[RetrievalMetrics] = field(default_factory=list)


def ndcg_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    relevance_scores: dict[str, float] | None = None,
    k: int = 5,
) -> float:
    """Normalized Discounted Cumulative Gain at rank k.

    Args:
        retrieved_ids: Ordered list of retrieved document/chunk IDs.
        relevant_ids: Set of IDs considered relevant.
        relevance_scores: Optional graded relevance (0-3). If None, binary (0/1).
        k: Cutoff rank.

    Returns:
        nDCG score between 0.0 and 1.0.
    """
    retrieved = retrieved_ids[:k]

    def _gain(doc_id: str) -> float:
        if relevance_scores and doc_id in relevance_scores:
            return relevance_scores[doc_id]
        return 1.0 if doc_id in relevant_ids else 0.0

    # DCG
    dcg = sum(
        _gain(doc_id) / math.log2(i + 2)
        for i, doc_id in enumerate(retrieved)
    )

    # Ideal DCG: sort all relevant docs by relevance, take top k
    if relevance_scores:
        ideal_gains = sorted(
            [relevance_scores.get(rid, 1.0) for rid in relevant_ids],
            reverse=True,
        )[:k]
    else:
        ideal_gains = [1.0] * min(len(relevant_ids), k)

    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal_gains))

    if idcg == 0:
        return 0.0
    return dcg / idcg


def recall_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    k: int = 5,
) -> float:
    """Recall at rank k: fraction of relevant docs found in top-k."""
    if not relevant_ids:
        return 0.0
    retrieved = set(retrieved_ids[:k])
    found = retrieved & relevant_ids
    return len(found) / len(relevant_ids)


def precision_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    k: int = 5,
) -> float:
    """Precision at rank k: fraction of top-k that are relevant."""
    retrieved = retrieved_ids[:k]
    if not retrieved:
        return 0.0
    relevant_in_top_k = sum(1 for rid in retrieved if rid in relevant_ids)
    return relevant_in_top_k / len(retrieved)


def reciprocal_rank(
    retrieved_ids: list[str],
    relevant_ids: set[str],
) -> float:
    """Reciprocal rank: 1/position of first relevant result."""
    for i, rid in enumerate(retrieved_ids):
        if rid in relevant_ids:
            return 1.0 / (i + 1)
    return 0.0


def evaluate_query(
    query: str,
    retrieved_ids: list[str],
    relevant_ids: set[str],
    relevance_scores: dict[str, float] | None = None,
    k: int = 5,
) -> RetrievalMetrics:
    """Evaluate a single query against its gold set."""
    return RetrievalMetrics(
        query=query,
        ndcg_at_k=ndcg_at_k(retrieved_ids, relevant_ids, relevance_scores, k),
        recall_at_k=recall_at_k(retrieved_ids, relevant_ids, k),
        precision_at_k=precision_at_k(retrieved_ids, relevant_ids, k),
        mrr=reciprocal_rank(retrieved_ids, relevant_ids),
        answerable=len(set(retrieved_ids[:k]) & relevant_ids) > 0,
        k=k,
        relevant_found=len(set(retrieved_ids[:k]) & relevant_ids),
        relevant_total=len(relevant_ids),
        retrieved_count=len(retrieved_ids),
    )


def evaluate_all(
    results: list[RetrievalMetrics],
) -> EvalSummary:
    """Aggregate metrics across multiple queries."""
    if not results:
        return EvalSummary()

    n = len(results)
    return EvalSummary(
        total_queries=n,
        mean_ndcg=sum(r.ndcg_at_k for r in results) / n,
        mean_recall=sum(r.recall_at_k for r in results) / n,
        mean_precision=sum(r.precision_at_k for r in results) / n,
        mean_mrr=sum(r.mrr for r in results) / n,
        answerability_rate=sum(1 for r in results if r.answerable) / n,
        per_query=results,
    )
