"""Config default regression for the auto-resolve default toggle.

No live Postgres needed — this only checks the Settings default.
"""
from raasoa.config import Settings


def test_llm_judge_enabled_by_default() -> None:
    """F-013 originally shipped this opt-in (default False) since
    unattended, permanent claim supersession is a real-stakes action.
    The owner explicitly reversed that decision on 2026-07-06 (see
    AUDIT_AND_FIX_PLAN.md §5, question 2) in favor of running
    auto-resolution unattended by default. The underlying safety
    mechanisms (subject-match requirement, per-claim scoping,
    duplicate-hash exclusion, and the auto-resolve threshold below which
    a conflict is always left for human review) are unchanged — only
    this toggle flipped."""
    assert Settings.model_fields["llm_judge_enabled"].default is True
