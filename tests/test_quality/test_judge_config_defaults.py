"""Config default regression for F-013's opt-in auto-resolve fix.

No live Postgres needed — this only checks the Settings default.
"""
from raasoa.config import Settings


def test_llm_judge_disabled_by_default() -> None:
    """Unattended, permanent claim supersession must require an explicit
    opt-in (LLM_JUDGE_ENABLED=true), not ship enabled by default."""
    assert Settings.model_fields["llm_judge_enabled"].default is False
