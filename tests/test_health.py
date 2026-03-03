"""
Smoke test — /health returns 200 without a real database.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch


@pytest.fixture
def app():
    # Patch out the DB engine so tests don't need postgres
    with patch("app.db.session.engine") as mock_engine:
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.run_sync = AsyncMock()
        mock_engine.begin.return_value = mock_conn
        mock_engine.dispose = AsyncMock()

        from app.main import app as _app
        yield _app


@pytest.mark.asyncio
async def test_health(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
