"""Pure-logic tests for principal resolution / ACL predicate building."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from raasoa.security.principal import (
    Principal,
    acl_predicate_sql,
    clamp_principal_override,
)


def _principal(*, legacy: bool, principal_id: str | None = "user:jane") -> Principal:
    return Principal(
        tenant_id=uuid.uuid4(),
        principal_id=None if legacy else principal_id,
        clearance="public",
        is_admin=legacy,
        is_legacy_tenant_wide=legacy,
    )


def test_acl_predicate_sql_default_aliases() -> None:
    sql = acl_predicate_sql()
    assert "d.source_id" in sql
    assert "s.default_visibility != 'restricted'" in sql
    assert ":principal_ids" in sql
    assert ":tenant_id" in sql
    # All three grant paths must be present.
    assert "acl_entries a2" in sql  # default-open-if-no-rows escape hatch
    assert "acl_entries a WHERE" in sql  # per-document grant
    assert "source_acl_grants g" in sql  # per-source grant


def test_acl_predicate_sql_custom_aliases() -> None:
    sql = acl_predicate_sql(
        doc_alias="doc", source_alias="src",
        principal_ids_param="pids", tenant_id_param="tid",
    )
    assert "doc.source_id" in sql
    assert "src.default_visibility" in sql
    assert ":pids" in sql
    assert ":tid" in sql
    # Default alias names must not leak in when overridden.
    assert " d.id" not in sql
    assert "s.default_visibility" not in sql


def test_clamp_override_legacy_key_unrestricted() -> None:
    """Legacy/tenant-wide keys keep today's unrestricted override — no
    resolved closure to check against, and no regression for existing
    self-hosted single-key deployments."""
    p = _principal(legacy=True)
    assert clamp_principal_override(p, "group:exec", resolved_ids=None) == "group:exec"


def test_clamp_override_no_override_requested() -> None:
    p = _principal(legacy=False)
    assert clamp_principal_override(p, None, resolved_ids=["user:jane", "group:sales"]) is None


def test_clamp_override_personal_key_within_closure_allowed() -> None:
    p = _principal(legacy=False)
    resolved = ["user:jane", "group:sales"]
    assert clamp_principal_override(p, "group:sales", resolved) == "group:sales"


def test_clamp_override_personal_key_impersonation_blocked() -> None:
    """A Sales rep's personal key must not be able to pass
    principal_id="group:exec" and read Exec-only documents."""
    p = _principal(legacy=False)
    resolved = ["user:jane", "group:sales"]
    with pytest.raises(HTTPException) as exc_info:
        clamp_principal_override(p, "group:exec", resolved)
    assert exc_info.value.status_code == 403
