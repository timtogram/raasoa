from raasoa.ingestion.chunker import chunk_document, count_tokens, recursive_split


def test_count_tokens() -> None:
    tokens = count_tokens("Hello world")
    assert tokens > 0
    assert isinstance(tokens, int)


def test_recursive_split_short_text() -> None:
    text = "This is a short text."
    chunks = recursive_split(text, chunk_size=100, chunk_overlap=10)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_recursive_split_respects_size() -> None:
    # Generate text that's definitely larger than chunk_size
    text = "This is sentence number one. " * 200
    chunks = recursive_split(text, chunk_size=50, chunk_overlap=10)
    assert len(chunks) > 1
    for chunk in chunks:
        # Allow some tolerance for overlap and splitting
        assert count_tokens(chunk) <= 80  # 50 + some margin


def test_recursive_split_overlap() -> None:
    paragraphs = [f"Paragraph {i} with some content for testing." for i in range(20)]
    text = "\n\n".join(paragraphs)
    chunks = recursive_split(text, chunk_size=30, chunk_overlap=10)
    assert len(chunks) > 1


def test_chunk_document_produces_results() -> None:
    text = "This is a test document.\n\n" * 50
    results = chunk_document(text, title="Test Doc", chunk_size=50, chunk_overlap=10)
    assert len(results) > 0
    for r in results:
        assert r.text.strip()
        assert r.token_count > 0
        assert r.index >= 0


def test_chunk_document_empty() -> None:
    results = chunk_document("", title="Empty")
    assert len(results) == 0


def test_chunk_document_preserves_title() -> None:
    results = chunk_document("Some content here.", title="My Title", chunk_size=100)
    assert len(results) == 1
    assert results[0].section_title == "My Title"
