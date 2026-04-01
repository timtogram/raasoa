from raasoa.ingestion.parser import parse_file, parse_text


def test_parse_text_basic() -> None:
    content = "# My Document\n\nThis is the body."
    result = parse_text(content, "test.md")
    assert result.title == "My Document"
    assert "body" in result.full_text
    assert len(result.sections) > 0


def test_parse_file_txt() -> None:
    data = b"Hello World\n\nThis is a test file."
    result = parse_file(data, "test.txt")
    assert result.title == "Hello World"
    assert result.metadata["format"] == "text"


def test_parse_file_unknown_extension() -> None:
    data = b"Some content"
    result = parse_file(data, "test.xyz")
    assert result.full_text == "Some content"
