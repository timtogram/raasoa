from dataclasses import dataclass

from raasoa.retrieval.hybrid_search import SearchResult


@dataclass
class ConfidenceBlock:
    retrieval_confidence: float
    source_count: int
    top_score: float
    answerable: bool


def compute_confidence(results: list[SearchResult]) -> ConfidenceBlock:
    """Compute confidence metrics from search results."""
    if not results:
        return ConfidenceBlock(
            retrieval_confidence=0.0,
            source_count=0,
            top_score=0.0,
            answerable=False,
        )

    top_score = results[0].score
    unique_docs = len({r.document_id for r in results})

    # Heuristic: confidence based on top score and result diversity
    # RRF scores are typically in [0, 0.033] range (1/(60+1) max per signal)
    # Max possible with both signals: ~0.033
    normalized_score = min(top_score / 0.033, 1.0)

    # More diverse sources = more confidence
    diversity_bonus = min(unique_docs / 3.0, 1.0) * 0.2

    confidence = min(normalized_score * 0.8 + diversity_bonus, 1.0)

    # Answerable if confidence is above threshold
    answerable = confidence > 0.3 and len(results) >= 2

    return ConfidenceBlock(
        retrieval_confidence=round(confidence, 3),
        source_count=unique_docs,
        top_score=round(top_score, 6),
        answerable=answerable,
    )
