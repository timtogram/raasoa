"""Tests for pluggable document-type schema validation."""

from raasoa.quality.schema_checks import (
    check_skill_schema,
    run_schema_check,
)


class TestSkillSchema:
    def test_valid_skill(self) -> None:
        fm = {
            "name": "My Skill", "description": "Does X",
            "version": "1.0", "ampel": "grün",
            "owner": "Tim", "executor": "claude",
        }
        result = check_skill_schema(fm, "## Zweck\nDoes X\n## SOP\nStep 1\n## DoD\nDone", [])
        assert result.valid
        assert result.score_penalty < 0.1

    def test_missing_name(self) -> None:
        fm = {"description": "Does X"}
        result = check_skill_schema(fm, "## Zweck\nContent", [])
        assert any(f.check == "missing_frontmatter" for f in result.findings)
        assert result.score_penalty > 0

    def test_missing_sections(self) -> None:
        fm = {"name": "X", "description": "Y"}
        result = check_skill_schema(fm, "Just text, no sections", [])
        assert any(f.check == "missing_section" for f in result.findings)


class TestSchemaRegistry:
    def test_skill_detected(self) -> None:
        result = run_schema_check(
            "skill", {"name": "X", "description": "Y"}, "## Zweck\nBody",
        )
        assert result is not None
        assert result.doc_type == "skill"

    def test_unknown_type_returns_none(self) -> None:
        result = run_schema_check("spreadsheet", {}, "data")
        assert result is None

    def test_no_type_returns_none(self) -> None:
        result = run_schema_check(None, {}, "data")
        assert result is None

    def test_type_from_frontmatter(self) -> None:
        result = run_schema_check(
            None, {"type": "skill", "name": "X", "description": "Y"},
            "## Zweck\nBody",
        )
        assert result is not None
