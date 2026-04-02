"""Tests for retrieval evaluation metrics."""


from raasoa.eval.metrics import (
    evaluate_all,
    evaluate_query,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


class TestNDCG:
    def test_perfect_ranking(self) -> None:
        result = ndcg_at_k(["a", "b"], {"a", "b"}, k=5)
        assert result == 1.0

    def test_no_relevant_results(self) -> None:
        result = ndcg_at_k(["x", "y"], {"a", "b"}, k=5)
        assert result == 0.0

    def test_empty_retrieved(self) -> None:
        result = ndcg_at_k([], {"a"}, k=5)
        assert result == 0.0

    def test_empty_relevant(self) -> None:
        result = ndcg_at_k(["a"], set(), k=5)
        assert result == 0.0

    def test_partial_match_lower_score(self) -> None:
        perfect = ndcg_at_k(["a", "b", "c"], {"a", "b", "c"}, k=3)
        partial = ndcg_at_k(["x", "a", "b"], {"a", "b", "c"}, k=3)
        assert partial < perfect

    def test_graded_relevance(self) -> None:
        scores = {"a": 3, "b": 2, "c": 1}
        good = ndcg_at_k(["a", "b", "c"], {"a", "b", "c"}, scores, k=3)
        bad = ndcg_at_k(["c", "b", "a"], {"a", "b", "c"}, scores, k=3)
        assert good > bad

    def test_k_limits_evaluation(self) -> None:
        result_k2 = ndcg_at_k(
            ["a", "b", "c", "d"], {"a", "b", "c", "d"}, k=2
        )
        result_k4 = ndcg_at_k(
            ["a", "b", "c", "d"], {"a", "b", "c", "d"}, k=4
        )
        assert result_k2 == result_k4 == 1.0


class TestRecall:
    def test_full_recall(self) -> None:
        assert recall_at_k(["a", "b"], {"a", "b"}, k=5) == 1.0

    def test_partial_recall(self) -> None:
        assert recall_at_k(["a", "x"], {"a", "b"}, k=5) == 0.5

    def test_zero_recall(self) -> None:
        assert recall_at_k(["x", "y"], {"a", "b"}, k=5) == 0.0

    def test_k_cutoff(self) -> None:
        assert recall_at_k(["x", "x", "a"], {"a"}, k=2) == 0.0
        assert recall_at_k(["x", "x", "a"], {"a"}, k=3) == 1.0


class TestPrecision:
    def test_perfect_precision(self) -> None:
        assert precision_at_k(["a", "b"], {"a", "b"}, k=2) == 1.0

    def test_half_precision(self) -> None:
        assert precision_at_k(["a", "x"], {"a", "b"}, k=2) == 0.5

    def test_zero_precision(self) -> None:
        assert precision_at_k(["x", "y"], {"a"}, k=2) == 0.0


class TestMRR:
    def test_first_position(self) -> None:
        assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0

    def test_second_position(self) -> None:
        assert reciprocal_rank(["x", "a", "c"], {"a"}) == 0.5

    def test_third_position(self) -> None:
        rr = reciprocal_rank(["x", "y", "a"], {"a"})
        assert abs(rr - 1 / 3) < 0.001

    def test_not_found(self) -> None:
        assert reciprocal_rank(["x", "y", "z"], {"a"}) == 0.0


class TestEvaluateQuery:
    def test_full_evaluation(self) -> None:
        m = evaluate_query(
            query="test",
            retrieved_ids=["a", "b", "x"],
            relevant_ids={"a", "b"},
            k=5,
        )
        assert m.ndcg_at_k == 1.0
        assert m.recall_at_k == 1.0
        assert m.mrr == 1.0
        assert m.answerable is True
        assert m.relevant_found == 2

    def test_no_relevant_found(self) -> None:
        m = evaluate_query(
            query="test",
            retrieved_ids=["x", "y"],
            relevant_ids={"a"},
            k=5,
        )
        assert m.ndcg_at_k == 0.0
        assert m.answerable is False


class TestEvalSummary:
    def test_aggregate_metrics(self) -> None:
        r1 = evaluate_query("q1", ["a"], {"a"}, k=5)
        r2 = evaluate_query("q2", ["x"], {"a"}, k=5)
        summary = evaluate_all([r1, r2])

        assert summary.total_queries == 2
        assert summary.mean_ndcg == 0.5
        assert summary.mean_recall == 0.5
        assert summary.answerability_rate == 0.5

    def test_empty_results(self) -> None:
        summary = evaluate_all([])
        assert summary.total_queries == 0
        assert summary.mean_ndcg == 0.0
