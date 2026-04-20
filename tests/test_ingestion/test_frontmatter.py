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
