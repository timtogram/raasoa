"""E2E ACL enforcement tests for T-17's five previously-unfiltered read
paths (F-015, F-016): claim clusters, document versioning/diff, the
source tree, quality findings/report, and document deletion.

Requires a live Postgres. Skips gracefully when unreachable. Follows
the same fixture/engine-reset pattern as test_documents_acl.py.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

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


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def scenario() -> AsyncGenerator[dict[str, object], None]:
    """One tenant, personal key for user:jane, an open source + doc, a
    restricted source with a granted doc and an ungranted doc — each doc
    carries a claim, a version row, and a quality finding; plus two
    conflicts (open-vs-ungranted, granted-vs-ungranted) for the
    either-side-visible policy."""
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    src_open = uuid.uuid4()
    src_restricted = uuid.uuid4()
    doc_open = uuid.uuid4()
    doc_granted = uuid.uuid4()
    doc_ungranted = uuid.uuid4()
    raw_key = "sk-test-" + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'ReadEndpointsAclTest')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, principal_id, "
                " clearance, is_admin, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'Jane', 'user:jane', "
                " 'public', false, true)"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "hash": key_hash, "prefix": raw_key[:10]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'notion', 'Open', '{}'::jsonb, 'inherit')"
            ),
            {"id": src_open, "tid": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'Restricted', '{}'::jsonb, 'restricted')"
            ),
            {"id": src_restricted, "tid": tenant_id},
        )
        for doc_id, sid, title in [
            (doc_open, src_open, "RE Open Doc"),
            (doc_granted, src_restricted, "RE Restricted Granted"),
            (doc_ungranted, src_restricted, "RE Restricted Ungranted"),
        ]:
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count, quality_score) "
                    "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', "
                    " 'published', 2, 1, 0, 0.8)"
                ),
                {
                    "id": doc_id, "tid": tenant_id, "sid": sid,
                    "soid": f"re-{doc_id.hex[:6]}", "title": title,
                },
            )
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', :pred, 'value', "
                    " 0.9, 'evidence', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "did": doc_id, "pred": f"pred-{title}"},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO document_versions "
                    "(id, document_id, version, content_hash, content_snapshot, created_at) "
                    "VALUES (:id, :did, 1, :hash, 'old content', now()) "
                ),
                {
                    "id": uuid.uuid4(), "did": doc_id,
                    "hash": hashlib.sha256(f"v1-{doc_id}".encode()).digest(),
                },
            )
            await session.execute(
                sql_text(
                    "INSERT INTO document_versions "
                    "(id, document_id, version, content_hash, content_snapshot, created_at) "
                    "VALUES (:id, :did, 2, :hash, 'new content', now()) "
                ),
                {
                    "id": uuid.uuid4(), "did": doc_id,
                    "hash": hashlib.sha256(f"v2-{doc_id}".encode()).digest(),
                },
            )
            await session.execute(
                sql_text(
                    "INSERT INTO quality_findings "
                    "(id, document_id, finding_type, severity, details) "
                    "VALUES (:id, :did, 'test_finding', 'warning', CAST(:details AS jsonb)) "
                ),
                {"id": uuid.uuid4(), "did": doc_id, "details": f'{{"title": "{title}"}}'},
            )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission) "
                "VALUES (:id, :did, 'user', 'user:jane', 'read')"
            ),
            {"id": uuid.uuid4(), "did": doc_granted},
        )
        # Conflict where one side (open) is visible, other (ungranted) isn't.
        await session.execute(
            sql_text(
                "INSERT INTO conflict_candidates "
                "(id, tenant_id, document_a_id, document_b_id, conflict_type, "
                " confidence, status) "
                "VALUES (:id, :tid, :a, :b, 'value_mismatch', 0.9, 'new')"
            ),
            {
                "id": uuid.uuid4(), "tid": tenant_id,
                "a": doc_open, "b": doc_ungranted,
            },
        )
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "doc_open": doc_open, "doc_granted": doc_granted, "doc_ungranted": doc_ungranted,
        "headers": {"Authorization": f"Bearer {raw_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        doc_ids = [doc_open, doc_granted, doc_ungranted]
        await session.execute(
            sql_text("DELETE FROM conflict_candidates WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM quality_findings WHERE document_id = ANY(:ids)"),
            {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM document_versions WHERE document_id = ANY(:ids)"),
            {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM claims WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM api_keys WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        await session.commit()


class TestClaimClustersAcl:
    async def test_list_excludes_ungranted_restricted_claims(
        self, scenario: dict[str, object],
    ) -> None:
        async with await _client() as client:
            resp = await client.get(
                "/v1/claim-clusters", params={"min_variants": 1}, headers=scenario["headers"],
            )
        assert resp.status_code == 200
        all_titles = {
            v.get("doc_title")
            for cluster in resp.json()
            for v in cluster["variants"]
        }
        assert "RE Restricted Ungranted" not in all_titles

    async def test_detail_excludes_ungranted_restricted_claims(
        self, scenario: dict[str, object],
    ) -> None:
        async with await _client() as client:
            resp = await client.get(
                "/v1/claim-clusters/pred-re restricted ungranted",
                headers=scenario["headers"],
            )
        assert resp.status_code == 200
        assert resp.json()["claims"] == []


class TestVersioningAcl:
    async def test_list_versions_404_for_ungranted(
        self, scenario: dict[str, object],
    ) -> None:
        headers = scenario["headers"]
        async with await _client() as client:
            granted = await client.get(
                f"/v1/documents/{scenario['doc_granted']}/versions", headers=headers,
            )
            ungranted = await client.get(
                f"/v1/documents/{scenario['doc_ungranted']}/versions", headers=headers,
            )
        assert granted.status_code == 200
        assert ungranted.status_code == 404

    async def test_diff_404_for_ungranted(self, scenario: dict[str, object]) -> None:
        headers = scenario["headers"]
        async with await _client() as client:
            granted = await client.get(
                f"/v1/documents/{scenario['doc_granted']}/diff", headers=headers,
            )
            ungranted = await client.get(
                f"/v1/documents/{scenario['doc_ungranted']}/diff", headers=headers,
            )
        assert granted.status_code == 200
        assert ungranted.status_code == 404


class TestSourceTreeAcl:
    async def test_excludes_ungranted_restricted_document(
        self, scenario: dict[str, object],
    ) -> None:
        async with await _client() as client:
            resp = await client.get("/v1/source-tree", headers=scenario["headers"])
        assert resp.status_code == 200
        all_titles = {
            doc["title"] for src in resp.json() for doc in src["documents"]
        }
        assert "RE Restricted Ungranted" not in all_titles
        assert "RE Restricted Granted" in all_titles


class TestQualityAcl:
    async def test_get_document_quality_404_for_ungranted(
        self, scenario: dict[str, object],
    ) -> None:
        headers = scenario["headers"]
        async with await _client() as client:
            granted = await client.get(
                f"/v1/documents/{scenario['doc_granted']}/quality", headers=headers,
            )
            ungranted = await client.get(
                f"/v1/documents/{scenario['doc_ungranted']}/quality", headers=headers,
            )
        assert granted.status_code == 200
        assert ungranted.status_code == 404

    async def test_list_findings_excludes_ungranted(
        self, scenario: dict[str, object],
    ) -> None:
        async with await _client() as client:
            resp = await client.get(
                "/v1/quality/findings", params={"limit": 200}, headers=scenario["headers"],
            )
        assert resp.status_code == 200
        doc_ids = {f["document_id"] for f in resp.json()}
        assert str(scenario["doc_ungranted"]) not in doc_ids
        assert str(scenario["doc_granted"]) in doc_ids

    async def test_conflicts_visible_when_either_side_visible(
        self, scenario: dict[str, object],
    ) -> None:
        """The open-vs-ungranted conflict must still show up (the open
        side is visible) — matches structured.py's identical policy."""
        async with await _client() as client:
            resp = await client.get(
                "/v1/conflicts", params={"limit": 200}, headers=scenario["headers"],
            )
        assert resp.status_code == 200
        pairs = {
            (c["document_a_id"], c["document_b_id"]) for c in resp.json()
        }
        assert (str(scenario["doc_open"]), str(scenario["doc_ungranted"])) in pairs


class TestDeleteDocumentAcl:
    async def test_delete_404_for_ungranted_restricted_document(
        self, scenario: dict[str, object],
    ) -> None:
        """The exact escalation from F-016: a caller with no grant on a
        restricted document must not be able to delete it just by
        knowing/guessing its id."""
        async with await _client() as client:
            resp = await client.delete(
                f"/v1/documents/{scenario['doc_ungranted']}", headers=scenario["headers"],
            )
        assert resp.status_code == 404

    async def test_delete_succeeds_for_granted_document(
        self, scenario: dict[str, object],
    ) -> None:
        async with await _client() as client:
            resp = await client.delete(
                f"/v1/documents/{scenario['doc_granted']}", headers=scenario["headers"],
            )
        assert resp.status_code == 200
