from raasoa.ingestion.chunker import ChunkResult
from raasoa.ingestion.parser import ParsedDocument
from raasoa.quality.checks import (
    check_boilerplate_ratio,
    check_chunk_count_range,
    check_chunk_size_distribution,
    check_embedding_success,
    check_minimum_length,
    check_parse_success,
    check_title_present,
    run_all_checks,
)


def _make_doc(text: str = "Some content", title: str | None = "My Title") -> ParsedDocument:
    return ParsedDocument(
        title=title, full_text=text, sections=[], metadata={"filename": "test.txt"}
    )


def _make_chunks(count: int = 5, token_count: int = 100) -> list[ChunkResult]:
    return [
        ChunkResult(text=f"chunk {i}", index=i, token_count=token_count)
        for i in range(count)
    ]


def test_check_parse_success_ok() -> None:
    doc = _make_doc("Hello world")
    assert check_parse_success(doc) is None


def test_check_parse_success_empty() -> None:
    doc = _make_doc("")
    result = check_parse_success(doc)
    assert result is not None
    assert result.severity == "critical"
    assert result.finding_type == "empty_content"


def test_check_parse_success_whitespace() -> None:
    doc = _make_doc("   \n\n  ")
    result = check_parse_success(doc)
    assert result is not None
    assert result.severity == "critical"


def test_check_minimum_length_ok() -> None:
    doc = _make_doc("x" * 100)
    assert check_minimum_length(doc) is None


def test_check_minimum_length_very_short() -> None:
    doc = _make_doc("hi")
    result = check_minimum_length(doc)
    assert result is not None
    assert result.severity == "critical"


def test_check_minimum_length_short() -> None:
    doc = _make_doc("x" * 30)
    result = check_minimum_length(doc)
    assert result is not None
    assert result.severity == "warning"


def test_check_title_present_ok() -> None:
    doc = _make_doc(title="Real Title")
    assert check_title_present(doc) is None


def test_check_title_present_missing() -> None:
    doc = _make_doc(title=None)
    result = check_title_present(doc)
    assert result is not None
    assert result.finding_type == "no_title"


def test_check_title_equals_filename() -> None:
    doc = _make_doc(title="test.txt")
    result = check_title_present(doc)
    assert result is not None


def test_check_boilerplate_ratio_ok() -> None:
    doc = _make_doc("\n".join(f"Line {i} unique content" for i in range(20)))
    assert check_boilerplate_ratio(doc) is None


def test_check_boilerplate_ratio_high() -> None:
    doc = _make_doc("\n".join(["Same line"] * 20))
    result = check_boilerplate_ratio(doc)
    assert result is not None
    assert result.finding_type == "high_boilerplate"


def test_check_embedding_success_ok() -> None:
    chunks = _make_chunks(5)
    assert check_embedding_success(chunks, 5) is None


def test_check_embedding_success_partial() -> None:
    chunks = _make_chunks(5)
    result = check_embedding_success(chunks, 3)
    assert result is not None
    assert result.severity == "critical"
    assert result.details["failed_chunks"] == 2


def test_check_chunk_size_distribution_ok() -> None:
    chunks = _make_chunks(10, token_count=100)
    assert check_chunk_size_distribution(chunks) is None


def test_check_chunk_size_distribution_many_tiny() -> None:
    chunks = _make_chunks(10, token_count=5)
    result = check_chunk_size_distribution(chunks)
    assert result is not None
    assert result.finding_type == "too_many_tiny_chunks"


def test_check_chunk_count_range_ok() -> None:
    doc = _make_doc("x" * 200)
    chunks = _make_chunks(3)
    assert check_chunk_count_range(doc, chunks) is None


def test_check_chunk_count_range_zero_chunks() -> None:
    doc = _make_doc("x" * 200)
    result = check_chunk_count_range(doc, [])
    assert result is not None
    assert result.finding_type == "no_chunks_from_content"


def test_run_all_checks_good_doc() -> None:
    doc = _make_doc("x" * 200, title="Good Title")
    chunks = _make_chunks(5, token_count=100)
    findings = run_all_checks(doc, chunks, embedded_count=5)
    assert len(findings) == 0


def test_run_all_checks_bad_doc() -> None:
    doc = _make_doc("", title=None)
    findings = run_all_checks(doc, [], embedded_count=0)
    assert len(findings) >= 2  # At least empty_content + no_title
    types = {f.finding_type for f in findings}
    assert "empty_content" in types
