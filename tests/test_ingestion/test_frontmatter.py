"""Tests for YAML frontmatter extraction."""

from raasoa.ingestion.parser import extract_frontmatter, parse_text


class TestExtractFrontmatter:
    def test_basic_frontmatter(self) -> None:
        content = "---\nname: My Skill\nversion: 1.0\nampel: grün\n---\n\n# Content"
        fm, body = extract_frontmatter(content)
        assert fm["name"] == "My Skill"
        assert fm["version"] == 1.0
        assert fm["ampel"] == "grün"
        assert "---" not in body
        assert "# Content" in body

    def test_no_frontmatter(self) -> None:
        content = "# Just a heading\n\nSome text"
        fm, body = extract_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_boolean_values(self) -> None:
        content = "---\nenabled: true\narchived: false\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["enabled"] is True
        assert fm["archived"] is False

    def test_integer_values(self) -> None:
        content = "---\npriority: 5\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["priority"] == 5

    def test_quoted_values(self) -> None:
        content = '---\ntitle: "My Title"\n---\nBody'
        fm, _ = extract_frontmatter(content)
        assert fm["title"] == "My Title"

    def test_key_normalization(self) -> None:
        content = "---\nDoc Type: skill\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["doc_type"] == "skill"

    def test_block_style_list(self) -> None:
        """F-046: block-style YAML lists were silently dropped entirely
        (the ``value == ""`` branch just did ``continue`` without
        collecting the following ``- item`` lines)."""
        content = "---\ntags:\n  - foo\n  - bar\n  - baz\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["tags"] == ["foo", "bar", "baz"]

    def test_block_style_list_with_quoted_items(self) -> None:
        content = '---\ntags:\n  - "foo bar"\n  - \'baz\'\n---\nBody'
        fm, _ = extract_frontmatter(content)
        assert fm["tags"] == ["foo bar", "baz"]

    def test_flow_style_list(self) -> None:
        content = "---\ntags: [foo, bar, baz]\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["tags"] == ["foo", "bar", "baz"]

    def test_flow_style_list_empty(self) -> None:
        content = "---\ntags: []\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["tags"] == []

    def test_list_followed_by_more_keys(self) -> None:
        """A list block must not swallow subsequent unrelated keys."""
        content = "---\ntags:\n  - foo\n  - bar\nversion: 2\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["tags"] == ["foo", "bar"]
        assert fm["version"] == 2

    def test_empty_scalar_value_becomes_empty_list(self) -> None:
        """A key with no value and nothing following it degrades to an
        empty list rather than being silently dropped."""
        content = "---\nnotes:\nversion: 1\n---\nBody"
        fm, _ = extract_frontmatter(content)
        assert fm["notes"] == []
        assert fm["version"] == 1


class TestParseTextWithFrontmatter:
    def test_title_from_frontmatter(self) -> None:
        content = "---\nname: Skill ABC\nampel: grün\n---\n\nSome content"
        doc = parse_text(content, "skill.md")
        assert doc.title == "Skill ABC"
        assert doc.frontmatter["ampel"] == "grün"
        assert "---" not in doc.full_text

    def test_frontmatter_in_metadata(self) -> None:
        content = "---\nversion: 2.0\nowner: Tim\n---\nBody"
        doc = parse_text(content, "test.md")
        assert doc.metadata["version"] == 2.0
        assert doc.metadata["owner"] == "Tim"
        assert doc.frontmatter["version"] == 2.0
