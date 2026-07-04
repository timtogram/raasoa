"""E2E tests for the structured CRM query path (Task #15): DSL validation,
SQL-injection resistance, operator semantics, and ACL enforcement via
crm_acl_predicate_sql (owner_principal_id / source_acl_grants).

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import pytest
from pydantic import ValidationError
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    try:
        async def _check() -> bool:
            engine = create_async_engine(DATABASE_URL)
            try:
                async with engine.connect() as conn:
                    await conn.execute(sql_text("SELECT 1"))
                return True
            finally:
                await engine.dispose()

        return asyncio.run(_check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}",
)

Scenario = tuple[dict[str, uuid.UUID], "async_sessionmaker[AsyncSession]"]


@pytest.fixture
async def scenario() -> AsyncGenerator[Scenario, None]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids = {
        "tenant_id": uuid.uuid4(),
        "src_open": uuid.uuid4(),
        "src_restricted": uuid.uuid4(),
        "src_group_only": uuid.uuid4(),
        "co_open": uuid.uuid4(),
        "co_owned": uuid.uuid4(),
        "co_ungranted": uuid.uuid4(),
        "co_via_grant": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'CrmQueryTest')"),
            {"id": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'Open', '{}'::jsonb, 'inherit')"
            ),
            {"id": ids["src_open"], "tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'Restricted', '{}'::jsonb, 'restricted')"
            ),
            {"id": ids["src_restricted"], "tid": ids["tenant_id"]},
        )
        # A separate restricted source that is visible ONLY via a whole-
        # source grant (no owner match for anyone) — isolates the
        # source_acl_grants path from the owner_principal_id path, since a
        # source-level grant legitimately makes every record on that
        # source visible, not just one record.
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'GroupOnly', '{}'::jsonb, 'restricted')"
            ),
            {"id": ids["src_group_only"], "tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO source_acl_grants "
                "(id, tenant_id, source_id, principal_id, permission) "
                "VALUES (:id, :tid, :sid, 'group:sales', 'read')"
            ),
            {"id": uuid.uuid4(), "tid": ids["tenant_id"], "sid": ids["src_group_only"]},
        )

        records = [
            (
                "co_open", ids["src_open"], "d1", None,
                {"dealname": "Widget Deal", "amount": "15000", "dealstage": "closedwon"},
            ),
            (
                "co_owned", ids["src_restricted"], "d2", "user:jane",
                {"dealname": "Secret Deal", "amount": "5000", "dealstage": "closedwon"},
            ),
            (
                "co_ungranted", ids["src_restricted"], "d3", "user:bob",
                {"dealname": "Other Secret Deal", "amount": "99999", "dealstage": "closedlost"},
            ),
            (
                "co_via_grant", ids["src_group_only"], "d4", None,
                {"dealname": "Group Grant Deal", "amount": "42000", "dealstage": "closedwon"},
            ),
        ]
        for key, source_id, external_id, owner, props in records:
            await session.execute(
                sql_text(
                    "INSERT INTO crm_objects "
                    "(id, tenant_id, source_id, object_type, external_id, "
                    " owner_principal_id, properties) "
                    "VALUES (:id, :tid, :sid, 'deals', :extid, :owner, CAST(:props AS jsonb))"
                ),
                {
                    "id": ids[key], "tid": ids["tenant_id"], "sid": source_id,
                    "extid": external_id, "owner": owner, "props": json.dumps(props),
                },
            )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        await session.execute(
            sql_text("DELETE FROM crm_objects WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM source_acl_grants WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.commit()
    await engine.dispose()


# ---- DSL validation (rejected before ever touching SQL) ----


def test_invalid_field_name_rejected() -> None:
    from raasoa.retrieval.crm_query import CrmFilter

    with pytest.raises(ValidationError):
        CrmFilter(field="amount; DROP TABLE crm_objects;--", op="eq", value="x")


def test_invalid_operator_rejected() -> None:
    from raasoa.retrieval.crm_query import CrmFilter

    with pytest.raises(ValidationError):
        CrmFilter(field="amount", op="'; DROP TABLE crm_objects; --", value="x")


def test_invalid_object_type_rejected() -> None:
    from raasoa.retrieval.crm_query import CrmQuery

    with pytest.raises(ValidationError):
        CrmQuery(object_type="deals; DROP TABLE crm_objects;--")


def test_in_requires_nonempty_list() -> None:
    from raasoa.retrieval.crm_query import CrmFilter

    with pytest.raises(ValidationError):
        CrmFilter(field="dealstage", op="in", value="closedwon")
    with pytest.raises(ValidationError):
        CrmFilter(field="dealstage", op="in", value=[])


def test_numeric_op_requires_numeric_value() -> None:
    from raasoa.retrieval.crm_query import CrmFilter

    with pytest.raises(ValidationError):
        CrmFilter(field="amount", op="gte", value="not-a-number")


# ---- SQL injection resistance (malicious *values* must be inert) ----


async def test_injection_style_value_is_inert(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    payload = "x'; DROP TABLE crm_objects; --"
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "dealname", "op": "eq", "value": payload},
            ]),
        )
        assert results == []

        # The table must still exist and hold all 4 seeded rows.
        check = await session.execute(
            sql_text("SELECT COUNT(*) FROM crm_objects WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        assert check.scalar() == 4


async def test_injection_style_in_list_is_inert(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "dealstage", "op": "in", "value": ["closedwon'); --", "closedlost"]},
            ]),
        )
    titles = {r["properties"]["dealname"] for r in results}
    assert titles == {"Other Secret Deal"}


# ---- Operator semantics (legacy/unfiltered caller — principal_ids=None) ----


async def test_eq_filter(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "dealstage", "op": "eq", "value": "closedwon"},
            ]),
        )
    assert {r["properties"]["dealname"] for r in results} == {
        "Widget Deal", "Secret Deal", "Group Grant Deal",
    }


async def test_gte_numeric_filter(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "amount", "op": "gte", "value": 10000},
            ]),
        )
    assert {r["properties"]["dealname"] for r in results} == {
        "Widget Deal", "Other Secret Deal", "Group Grant Deal",
    }


async def test_contains_filter(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "dealname", "op": "contains", "value": "Secret"},
            ]),
        )
    assert {r["properties"]["dealname"] for r in results} == {
        "Secret Deal", "Other Secret Deal",
    }


async def test_is_null_filter_matches_missing_property(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(object_type="deals", filters=[
                {"field": "no_such_property", "op": "is_null"},
            ]),
        )
    assert len(results) == 4


async def test_sort_and_limit(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None,
            CrmQuery(
                object_type="deals",
                sort={"field": "amount", "direction": "desc"},
                limit=1,
            ),
        )
    assert len(results) == 1
    assert results[0]["properties"]["dealname"] == "Other Secret Deal"


# ---- ACL enforcement ----


async def test_legacy_caller_sees_everything(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], None, CrmQuery(object_type="deals"),
        )
    assert len(results) == 4


async def test_stranger_sees_only_open_source(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], ["user:stranger"], CrmQuery(object_type="deals"),
        )
    assert {r["properties"]["dealname"] for r in results} == {"Widget Deal"}


async def test_owner_sees_own_record_plus_open_source(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], ["user:jane"], CrmQuery(object_type="deals"),
        )
    assert {r["properties"]["dealname"] for r in results} == {"Widget Deal", "Secret Deal"}


async def test_source_acl_grant_widens_access(scenario: Scenario) -> None:
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], ["group:sales"], CrmQuery(object_type="deals"),
        )
    assert {r["properties"]["dealname"] for r in results} == {
        "Widget Deal", "Group Grant Deal",
    }


async def test_empty_principal_ids_fails_closed_to_open_source_only(
    scenario: Scenario,
) -> None:
    """An authenticated personal principal with zero group memberships and
    no grants must see exactly the open-source records — never the
    'legacy unfiltered' set."""
    from raasoa.retrieval.crm_query import CrmQuery, run_crm_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await run_crm_query(
            session, ids["tenant_id"], [], CrmQuery(object_type="deals"),
        )
    assert {r["properties"]["dealname"] for r in results} == {"Widget Deal"}
