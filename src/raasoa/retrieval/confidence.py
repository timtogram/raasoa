from dataclasses import dataclass

from raasoa.retrieval.hybrid_search import SearchResult

# A document only counts toward the diversity bonus if its score is at
# least this fraction of the top result's score -- see compute_confidence.
_COHERENCE_RATIO = 0.7


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

    # More diverse sources = more confidence -- but only counting
    # documents whose score is close to the top result's. Counting
    # raw document count regardless of score previously let a handful of
    # weak, unrelated tail matches (different documents, but nothing to
    # do with each other or the query) inflate confidence via sheer
    # document count alone -- exactly the scenario where retrieval
    # actually found nothing good and is padding out top_k with
    # whatever was least-bad. A genuinely corroborated answer has
    # multiple documents scoring close to each other near the top, not
    # just multiple documents somewhere in the result set.
    coherence_floor = top_score * _COHERENCE_RATIO
    corroborating_docs = len(
        {r.document_id for r in results if r.score >= coherence_floor}
    )
    diversity_bonus = min(corroborating_docs / 3.0, 1.0) * 0.2

    confidence = min(normalized_score * 0.8 + diversity_bonus, 1.0)

    # Answerable if confidence is above threshold
    answerable = confidence > 0.3 and len(results) >= 2

    return ConfidenceBlock(
        retrieval_confidence=round(confidence, 3),
        source_count=unique_docs,
        top_score=round(top_score, 6),
        answerable=answerable,
    )
