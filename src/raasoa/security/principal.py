"""Principal resolution for ACL/RBAC enforcement.

A "principal" is whoever/whatever is making a request — a specific human
(``user:jane``), a group (``group:sales``), or a source-derived identity
(``hubspot:owner:42``). This is a *different, complementary* mechanism to
the MCP Policy-Gate in ``raasoa.mcp.policy`` (which ranks a document's
``classification`` frontmatter against a caller-supplied clearance level):
this module answers "does this specific principal have a grant for this
specific document/source", not "is this principal's clearance high enough
for this document's sensitivity tier". Both apply independently.

Design note (the one thing to get exactly right): a legacy/tenant-wide API
key (no personal ``principal_id`` set — the default for every key created
before this module existed, for ENV-configured keys, and for
``AUTH_ENABLED=false`` deployments) must NOT be represented as a normal
principal-id *string*. If it were, it would never match any
``acl_entries``/``source_acl_grants`` row (nobody grants access to a
synthetic placeholder string), so the moment any tenant creates its first
``restricted`` source, every existing self-hosted deployment and shared
key would silently lose access to it — a regression, not a security
improvement. Instead, ``Principal.is_legacy_tenant_wide`` is a structural
flag: when true, callers must skip ACL filtering entirely (pass
``principal_ids=None``), reproducing today's exact unfiltered behavior.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.middleware.auth import (
    DEFAULT_TENANT,
    _extract_api_key,
    _get_env_key_map,
    _resolve_key_row_from_db,
)
from raasoa.middleware.auth import (
    resolve_tenant_async as _resolve_tenant_async,
)

# Max hops when walking the group-membership graph. A generous bound
# against a misconfigured or malicious membership cycle; real membership
# depth is expected to be 1-2 hops (user -> team -> department, at most).
_MAX_MEMBERSHIP_DEPTH = 5


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    principal_id: str | None
    clearance: str
    is_admin: bool
    is_legacy_tenant_wide: bool


async def resolve_principal_async(request: Request) -> Principal:
    """Resolve the calling principal for ACL-aware endpoints.

    Falls back to a legacy tenant-wide principal (unfiltered access, same
    as today) for: AUTH_ENABLED=false, ENV-configured keys, and any DB key
    with no principal_id set. Only a DB-issued personal key (principal_id
    set via /v1/admin/users/{id}/api-key) resolves to a real principal.
    """
    if not settings.auth_enabled:
        return Principal(
            tenant_id=DEFAULT_TENANT, principal_id=None,
            clearance="secret", is_admin=True, is_legacy_tenant_wide=True,
        )

    api_key = _extract_api_key(request)
    if api_key:
        env_map = _get_env_key_map()
        if api_key in env_map:
            return Principal(
                tenant_id=env_map[api_key], principal_id=None,
                clearance="secret", is_admin=True, is_legacy_tenant_wide=True,
            )

        row = await _resolve_key_row_from_db(api_key)
        if row:
            tenant_id, principal_id, clearance, is_admin = row
            if principal_id is None:
                return Principal(
                    tenant_id=tenant_id, principal_id=None,
                    clearance=clearance, is_admin=True,
                    is_legacy_tenant_wide=True,
                )
            return Principal(
                tenant_id=tenant_id, principal_id=principal_id,
                clearance=clearance, is_admin=is_admin,
                is_legacy_tenant_wide=False,
            )

    # No usable key found — delegate to resolve_tenant_async so the
    # exact same 401 behavior (or DEFAULT_TENANT passthrough) applies.
    tenant_id = await _resolve_tenant_async(request)
    return Principal(
        tenant_id=tenant_id, principal_id=None,
        clearance="secret", is_admin=True, is_legacy_tenant_wide=True,
    )


async def expand_principal_ids(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: str,
) -> list[str]:
    """Return [principal_id, *all transitive group principal_ids].

    Every hop is scoped by tenant_id explicitly — principal_groups/
    principal_memberships have no cross-tenant FK chain protecting them
    (unlike acl_entries, which is protected transitively via
    documents.tenant_id), so a human-chosen principal_id string like
    "group:sales" could otherwise collide across tenants that both picked
    the same name.
    """
    seen: set[str] = {principal_id}
    frontier = [principal_id]
    for _ in range(_MAX_MEMBERSHIP_DEPTH):
        if not frontier:
            break
        result = await session.execute(
            text(
                "SELECT DISTINCT group_principal_id FROM principal_memberships "
                "WHERE tenant_id = :tenant_id "
                "AND member_principal_id = ANY(:frontier)"
            ),
            {"tenant_id": tenant_id, "frontier": frontier},
        )
        next_frontier = [r.group_principal_id for r in result if r.group_principal_id not in seen]
        seen.update(next_frontier)
        frontier = next_frontier
    return list(seen)


async def resolve_principal_ids(request: Request, session: AsyncSession) -> list[str] | None:
    """Convenience wrapper: resolve the principal and expand its group
    closure in one call. Returns None for legacy/tenant-wide callers
    (meaning: apply no ACL filter, exactly today's behavior) — callers
    MUST check for None rather than treating an empty list the same way;
    an empty list means "authenticated personal principal with zero
    grants" and must filter to nothing, not everything.
    """
    principal = await resolve_principal_async(request)
    if principal.is_legacy_tenant_wide:
        return None
    assert principal.principal_id is not None  # guaranteed by the branch above
    return await expand_principal_ids(session, principal.tenant_id, principal.principal_id)


def acl_predicate_sql(
    *, doc_alias: str = "d", source_alias: str = "s",
    principal_ids_param: str = "principal_ids", tenant_id_param: str = "tenant_id",
) -> str:
    """The single ACL/visibility predicate shared by every document-reading
    query: hybrid_search, structured_query, list_documents, get_document,
    find_by_metadata, get_dependencies.

    Requires the caller's query to already join
    ``{source_alias} ON {source_alias}.id = {doc_alias}.source_id`` and to
    bind ``:{principal_ids_param}`` (a list[str], via SQLAlchemy's native
    Postgres-array parameter binding — NOT ``bindparams(expanding=True)``,
    which is for ``IN (...)`` and would be wrong here) and
    ``:{tenant_id_param}``.

    Semantics:
      - A document with NO acl_entries rows is visible to everyone, UNLESS
        its source has default_visibility='restricted' — that's the
        "restricted sources lose the default-open escape hatch" rule.
      - A document WITH acl_entries rows is visible only to a matching
        principal (regardless of the source's default_visibility).
      - source_acl_grants acts like a virtual ACL row for every document
        from that source — the "grant the whole source to a group" path.

    Call ``if principal_ids is not None:`` (never a truthy check) before
    appending this fragment — principal_ids=[] must still apply the
    predicate (and correctly match nothing, i.e. fail closed for an
    authenticated-but-scopeless principal), which is exactly what
    ``= ANY('{}')`` does in Postgres. Only principal_ids=None (a
    legacy/tenant-wide caller) should skip this fragment entirely,
    reproducing today's unfiltered behavior.
    """
    return (
        f" AND ("
        f"   ({source_alias}.default_visibility != 'restricted' AND NOT EXISTS ("
        f"     SELECT 1 FROM acl_entries a2 WHERE a2.document_id = {doc_alias}.id"
        f"   ))"
        f"   OR EXISTS ("
        f"     SELECT 1 FROM acl_entries a WHERE a.document_id = {doc_alias}.id"
        f"     AND a.principal_id = ANY(:{principal_ids_param})"
        f"     AND a.permission IN ('read', 'write', 'admin')"
        f"   )"
        f"   OR EXISTS ("
        f"     SELECT 1 FROM source_acl_grants g"
        f"     WHERE g.source_id = {doc_alias}.source_id"
        f"     AND g.tenant_id = :{tenant_id_param}"
        f"     AND g.principal_id = ANY(:{principal_ids_param})"
        f"   )"
        f" )"
    )


def crm_acl_predicate_sql(
    *, crm_alias: str = "co", source_alias: str = "s",
    principal_ids_param: str = "principal_ids", tenant_id_param: str = "tenant_id",
) -> str:
    """ACL predicate for ``crm_objects`` rows — parallel to
    ``acl_predicate_sql`` but keyed on ``owner_principal_id`` instead of a
    per-document ``acl_entries`` row, since ``crm_objects`` has no such
    table (see the migration's docstring for why: CRM records are
    per-owner sensitive by construction, so the owner check is a plain
    column comparison rather than a correlated EXISTS).

    Same calling convention as ``acl_predicate_sql``: only skip this
    fragment when ``principal_ids is None`` (legacy/tenant-wide caller);
    an empty list must still apply the predicate and correctly match
    nothing.
    """
    return (
        f" AND ("
        f"   {source_alias}.default_visibility != 'restricted'"
        f"   OR {crm_alias}.owner_principal_id = ANY(:{principal_ids_param})"
        f"   OR EXISTS ("
        f"     SELECT 1 FROM source_acl_grants g"
        f"     WHERE g.source_id = {crm_alias}.source_id"
        f"     AND g.tenant_id = :{tenant_id_param}"
        f"     AND g.principal_id = ANY(:{principal_ids_param})"
        f"   )"
        f" )"
    )


def clamp_principal_override(
    principal: Principal, requested_principal_id: str | None, resolved_ids: list[str] | None,
) -> str | None:
    """Enforce that a personal key's caller-supplied principal_id override
    can only narrow its own resolved closure, never impersonate another
    principal (e.g. a Sales rep passing principal_id="group:exec" in a
    request body to read Exec-only documents). Legacy/tenant-wide keys
    keep today's unrestricted override behavior — nothing to narrow.
    """
    if requested_principal_id is None:
        return None
    if principal.is_legacy_tenant_wide:
        return requested_principal_id
    if resolved_ids is not None and requested_principal_id not in resolved_ids:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="principal_id override is not in the caller's resolved principal set",
        )
    return requested_principal_id
