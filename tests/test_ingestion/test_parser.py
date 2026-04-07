"""Tests for document parsers — text, CSV, HTML, table formatting."""

from raasoa.ingestion.parser import (
    _table_to_markdown,
    parse_csv,
    parse_file,
    parse_html,
    parse_text,
)


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


class TestTableToMarkdown:
    def test_basic_table(self) -> None:
        md = _table_to_markdown(
            ["Name", "Amount"],
            [["Invoice 1", "100 CHF"], ["Invoice 2", "200 EUR"]],
        )
        assert "| Name | Amount |" in md
        assert "| Invoice 1 | 100 CHF |" in md
        assert "| Invoice 2 | 200 EUR |" in md

    def test_empty_table(self) -> None:
        assert _table_to_markdown([], []) == ""

    def test_no_headers(self) -> None:
        md = _table_to_markdown([], [["a", "b"], ["c", "d"]])
        assert "Col1" in md
        assert "| a | b |" in md


class TestCSVParser:
    def test_parse_basic_csv(self) -> None:
        csv_data = b"Name,Amount,Currency\nInvoice 1,100,CHF\nInvoice 2,200,EUR"
        result = parse_csv(csv_data, "invoices.csv")
        assert result.metadata["format"] == "csv"
        assert result.metadata["rows"] == 2
        assert result.metadata["columns"] == 3
        assert "| Name | Amount | Currency |" in result.full_text
        assert "Invoice 1" in result.full_text
        assert "CHF" in result.full_text

    def test_parse_csv_via_parse_file(self) -> None:
        csv_data = b"col1,col2\nval1,val2"
        result = parse_file(csv_data, "data.csv")
        assert result.metadata["format"] == "csv"
        assert "table" in result.sections[0].section_type

    def test_parse_empty_csv(self) -> None:
        result = parse_csv(b"", "empty.csv")
        assert "(empty CSV)" in result.full_text


class TestHTMLParser:
    def test_strip_tags(self) -> None:
        html = "<h1>Title</h1><p>Hello <b>world</b></p>"
        result = parse_html(html, "test.html")
        assert "Title" in result.full_text
        assert "Hello world" in result.full_text
        assert "<h1>" not in result.full_text

    def test_entities(self) -> None:
        html = "Price: 100 &amp; more"
        result = parse_html(html, "test.html")
        assert "100 & more" in result.full_text

    def test_parse_html_via_parse_file(self) -> None:
        html = b"<html><body><p>test</p></body></html>"
        result = parse_file(html, "page.html")
        assert "test" in result.full_text
