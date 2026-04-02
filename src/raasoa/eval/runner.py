"""Retrieval evaluation runner.

Runs a gold-set against the RAASOA retrieval API and computes metrics.

Gold-set format (JSON):
[
  {
    "query": "What tool do we use for data visualization?",
    "relevant_doc_ids": ["doc-uuid-1"],
    "relevant_chunk_ids": ["chunk-uuid-1", "chunk-uuid-2"],
    "relevance_scores": {"chunk-uuid-1": 3, "chunk-uuid-2": 2},
    "expected_answerable": true
  }
]

Usage:
    # Run evaluation against live API
    uv run python -m raasoa.eval.runner --gold-set eval/gold_set.json

    # Run against specific endpoint
    uv run python -m raasoa.eval.runner --gold-set eval/gold_set.json --url http://localhost:8001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from raasoa.eval.metrics import (
    EvalSummary,
    RetrievalMetrics,
    evaluate_all,
    evaluate_query,
)

logger = logging.getLogger(__name__)


async def run_eval(
    gold_set_path: str,
    base_url: str = "http://localhost:8001",
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
    api_key: str | None = None,
    top_k: int = 10,
) -> EvalSummary:
    """Run evaluation against the RAASOA API.

    Args:
        gold_set_path: Path to gold-set JSON file.
        base_url: RAASOA API base URL.
        tenant_id: Tenant ID for queries.
        api_key: Optional API key for auth.
        top_k: Number of results to retrieve per query.

    Returns:
        EvalSummary with per-query and aggregate metrics.
    """
    gold_set = json.loads(Path(gold_set_path).read_text())

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    results: list[RetrievalMetrics] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for item in gold_set:
            query = item["query"]
            relevant_chunks = set(item.get("relevant_chunk_ids", []))
            relevant_docs = set(item.get("relevant_doc_ids", []))
            relevance_scores = item.get("relevance_scores")

            # Use chunk-level eval if available, else doc-level
            relevant_ids = relevant_chunks or relevant_docs
            if not relevant_ids:
                logger.warning("Skipping query with no relevant IDs: %s", query)
                continue

            start = time.monotonic()
            try:
                resp = await client.post(
                    f"{base_url}/v1/retrieve",
                    json={
                        "query": query,
                        "top_k": top_k,
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error("Query failed: %s — %s", query, e)
                continue

            latency_ms = int((time.monotonic() - start) * 1000)

            # Extract retrieved IDs
            hits = data.get("results", [])
            if relevant_chunks:
                retrieved_ids = [h["chunk_id"] for h in hits]
            else:
                retrieved_ids = [h["document_id"] for h in hits]

            metrics = evaluate_query(
                query=query,
                retrieved_ids=retrieved_ids,
                relevant_ids=relevant_ids,
                relevance_scores=relevance_scores,
                k=top_k,
            )
            results.append(metrics)

            logger.info(
                "  %s | nDCG=%.3f recall=%.3f precision=%.3f mrr=%.3f %dms",
                query[:50],
                metrics.ndcg_at_k,
                metrics.recall_at_k,
                metrics.precision_at_k,
                metrics.mrr,
                latency_ms,
            )

    return evaluate_all(results)


def print_report(summary: EvalSummary) -> None:
    """Print a human-readable evaluation report."""
    print("\n" + "=" * 60)
    print("RAASOA Retrieval Evaluation Report")
    print("=" * 60)
    print(f"  Queries evaluated:  {summary.total_queries}")
    print(f"  Mean nDCG@k:        {summary.mean_ndcg:.3f}")
    print(f"  Mean Recall@k:      {summary.mean_recall:.3f}")
    print(f"  Mean Precision@k:   {summary.mean_precision:.3f}")
    print(f"  Mean MRR:           {summary.mean_mrr:.3f}")
    print(f"  Answerability:      {summary.answerability_rate:.0%}")
    print("=" * 60)

    if summary.per_query:
        print("\nPer-Query Breakdown:")
        print(f"  {'Query':<40} {'nDCG':>6} {'Recall':>7} {'MRR':>5}")
        print("  " + "-" * 58)
        for r in summary.per_query:
            q = r.query[:38] + ".." if len(r.query) > 40 else r.query
            print(
                f"  {q:<40} {r.ndcg_at_k:>6.3f} "
                f"{r.recall_at_k:>7.3f} {r.mrr:>5.3f}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAASOA Retrieval Evaluation",
    )
    parser.add_argument(
        "--gold-set", required=True,
        help="Path to gold-set JSON file",
    )
    parser.add_argument(
        "--url", default="http://localhost:8001",
        help="RAASOA API base URL",
    )
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--output", default=None,
        help="Write JSON results to file",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    summary = await run_eval(
        gold_set_path=args.gold_set,
        base_url=args.url,
        api_key=args.api_key,
        top_k=args.top_k,
    )

    print_report(summary)

    if args.output:
        Path(args.output).write_text(
            json.dumps(asdict(summary), indent=2, default=str)
        )
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
