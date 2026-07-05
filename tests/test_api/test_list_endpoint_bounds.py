"""F-039: list endpoints must reject out-of-range limit/offset values.

FastAPI validates ``Query(..., ge=..., le=...)`` bounds before the endpoint
body runs (and therefore before any DB access), so these are lightweight
ASGI-transport tests with no live Postgres dependency — mirroring the
pattern in test_health.py.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from raasoa.main import app


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_quality_findings_limit_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/quality/findings", params={"limit": 999999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_quality_findings_negative_offset_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/quality/findings", params={"offset": -1})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_conflicts_limit_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/conflicts", params={"limit": 999999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reviews_limit_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/reviews", params={"limit": 999999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_analytics_audit_limit_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/analytics/audit", params={"limit": 999999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dependencies_graph_limit_nodes_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/dependencies/graph", params={"limit_nodes": 999999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dependencies_graph_limit_nodes_within_existing_usage_accepted() -> None:
    """Existing test_documents_acl.py exercises limit_nodes=500 and expects
    a 200 — confirm the new bound doesn't regress that call shape at the
    validation layer (a 500/other failure here would be a DB issue, not a
    422, since Postgres is not required for this assertion)."""
    async with await _client() as client:
        resp = await client.get("/v1/dependencies/graph", params={"limit_nodes": 500})
    assert resp.status_code != 422


@pytest.mark.asyncio
async def test_claim_clusters_min_variants_out_of_range_rejected() -> None:
    async with await _client() as client:
        resp = await client.get("/v1/claim-clusters", params={"min_variants": 0})
    assert resp.status_code == 422
