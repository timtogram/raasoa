from dataclasses import dataclass

from raasoa.retrieval.hybrid_search import SearchResult


@dataclass
class ConfidenceBlock:
    retrieval_confidence: float
    source_count: int
    top_score: float
    answerable: bool


def compute_confidence(
    results: list[SearchResult], *, max_score: float = 0.033
) -> ConfidenceBlock:
    """Compute confidence metrics from search results.

    ``max_score`` is the theoretical maximum of ``result.score`` for
    whatever produced ``results`` — RRF-scale hybrid search tops out
    around 1/(60+1) per signal (~0.033, the default), but a reranker's
    relevance score is already normalized to [0, 1]. Callers MUST pass
    the reranker's own scale (e.g. ``reranker.SCORE_SCALE``) when
    results have been reranked — using the RRF default against a [0, 1]
    score would saturate confidence to ~1.0 unconditionally, silently
    disabling /v1/answer's min_confidence refusal gate.
    """
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
    normalized_score = min(top_score / max_score, 1.0)

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
