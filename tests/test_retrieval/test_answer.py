"""Tests for grounded answer synthesis (pure helpers)."""
from raasoa.retrieval.answer import (
    SourceChunk,
    build_answer_prompt,
    build_sources_block,
    cited_source_numbers,
    clean_model_output,
    is_grounded,
    is_insufficient,
    valid_citation_numbers,
)

_CHUNKS = [
    SourceChunk(
        n=1, chunk_id="c1", document_id="d1",
        document_title="Travel Policy 2026", source_url=None,
        source_location="Section 1", text="Meal allowance is 32 EUR per day.",
    ),
    SourceChunk(
        n=2, chunk_id="c2", document_id="d2",
        document_title="Travel Policy 2024", source_url=None,
        source_location=None, text="Meal allowance is 28 EUR per day.",
    ),
]


def test_sources_block_numbers_and_titles() -> None:
    block = build_sources_block(_CHUNKS)
    assert "[1] Travel Policy 2026 (Section 1)" in block
    assert "[2] Travel Policy 2024" in block
    assert "32 EUR" in block and "28 EUR" in block


def test_prompt_includes_query_and_rules() -> None:
    p = build_answer_prompt("How much is the meal allowance?", _CHUNKS)
    assert "How much is the meal allowance?" in p
    assert "INSUFFICIENT" in p  # refusal instruction present
    assert "[1]" in p


def test_clean_strips_closed_think() -> None:
    assert clean_model_output("<think>reasoning</think>The answer is 32 EUR [1].") == (
        "The answer is 32 EUR [1]."
    )


def test_clean_drops_unclosed_think() -> None:
    assert clean_model_output("<think>still thinking and ran out") == ""


def test_is_insufficient() -> None:
    assert is_insufficient("INSUFFICIENT")
    assert is_insufficient("  insufficient ")
    assert is_insufficient("")
    assert not is_insufficient("The allowance is 32 EUR [1].")


def test_cited_source_numbers() -> None:
    assert cited_source_numbers("Foo [1] and bar [3].") == {1, 3}
    assert cited_source_numbers("No citations here.") == set()


def test_valid_citation_numbers_filters_out_of_range() -> None:
    # Model hallucinated [9] but only 4 sources exist.
    assert valid_citation_numbers("See [1] and [9].", 4) == {1}
    assert valid_citation_numbers("See [2], [3].", 4) == {2, 3}


def test_is_grounded_backstop() -> None:
    # Real citation → grounded
    assert is_grounded("The allowance is 32 EUR [1].", 4)
    # Leaked reasoning with no citation → not grounded → endpoint refuses
    assert not is_grounded("Okay, let me think about what the user wants...", 4)
    # Out-of-range-only citations → not grounded
    assert not is_grounded("As per [7].", 4)
