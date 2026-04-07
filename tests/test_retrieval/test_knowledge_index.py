"""Tests for knowledge index normalization."""

from raasoa.retrieval.knowledge_index import normalize


def test_normalize_lowercase() -> None:
    assert normalize("Power BI") == "power bi"


def test_normalize_strips_filler() -> None:
    result = normalize("the primary tool for data visualization")
    assert "the" not in result.split()
    assert "for" not in result.split()
    assert "primary" in result
    assert "data" in result
    assert "visualization" in result


def test_normalize_german_filler() -> None:
    result = normalize("das zentrale Tool für Datenvisualisierung")
    assert "das" not in result.split()
    assert "für" not in result.split()
    assert "zentrale" in result


def test_normalize_strips_punctuation() -> None:
    result = normalize("BI-Tool (primary)")
    assert "bi" in result
    assert "tool" in result
    assert "primary" in result


def test_normalize_empty() -> None:
    assert normalize("") == ""
    assert normalize("the a an") == ""
