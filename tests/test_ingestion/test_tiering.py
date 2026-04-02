"""Tests for tiered indexing logic."""

from raasoa.ingestion.tiering import assign_initial_tier
from raasoa.models.document import Document


def test_initial_tier_is_hot() -> None:
    doc = Document.__new__(Document)
    tier = assign_initial_tier(doc)
    assert tier == "hot"
