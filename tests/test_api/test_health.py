import pytest
from httpx import ASGITransport, AsyncClient

from raasoa.main import app


@pytest.mark.asyncio
async def test_root() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "RAASOA"
    assert "health" in data
    assert "dashboard" in data


@pytest.mark.asyncio
async def test_health() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    # In test env without DB, status may be degraded
    assert data["status"] in ("healthy", "degraded")
    assert "embedding" in data
    assert "claim_extraction" in data


@pytest.mark.asyncio
async def test_readiness_probe() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert "ready" in data
