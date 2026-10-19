from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_coding_routes_are_registered_in_application():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert {"/coding/status", "/coding/sessions", "/coding/runs/{run_id}/events",
            "/coding/sessions/{session_id}/checkout", "/coding/sessions/{session_id}/checkout/remove"} <= paths


@pytest.mark.asyncio
async def test_coding_uses_application_auth_and_error_envelope(client, monkeypatch):
    from app.main import app
    from app.coding_runtime.service import CodingServiceError
    from uuid import uuid4

    username = f"coding{uuid4().hex[:8]}"
    registered = await client.post("/auth/register", json={
        "username": username, "email": f"{username}@example.com", "password": "testpass123",
    })
    assert registered.status_code == 201, registered.text
    login = await client.post("/auth/login", json={"username": username, "password": "testpass123"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    monkeypatch.setattr(app.state, "coding_sessions", None, raising=False)
    assert (await client.get("/coding/status")).status_code == 403
    assert not (await client.get("/coding/status", headers=headers)).json()["enabled"]
    disabled = await client.get("/coding/sessions", headers=headers)
    assert disabled.status_code == 503
    assert disabled.json()["error"]["code"] == "coding_unavailable"
    assert disabled.json()["trace_id"]
    service = SimpleNamespace(get_session=AsyncMock(side_effect=CodingServiceError("not_found", "Coding session not found")))
    monkeypatch.setattr(app.state, "coding_sessions", service)
    missing = await client.get("/coding/sessions/missing", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,failure", [(True, None), (False, None), (True, "context"), (False, "context"), (True, "startup")])
async def test_app_closes_coding_before_mcp_models_and_database(monkeypatch, enabled, failure):
    from app import main
    from app.agent import local_model_lifecycle
    from app.autonomy import scheduler
    from app.coding_runtime import bootstrap
    from app.db import session as database
    from app.db import security as database_security
    from app import model_profiles
    from app.mcp import registry
    from app.rag import job_runner, service as rag

    closed = []

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def db_session():
        yield SimpleNamespace(commit=AsyncMock())

    async def close_coding():
        closed.append("coding")

    async def close_mcp():
        closed.append("mcp")

    async def release_models():
        closed.append("models")

    async def dispose_database():
        closed.append("database")

    monkeypatch.setattr(main, "engine", SimpleNamespace(begin=connection, dispose=dispose_database))
    database_check = AsyncMock()
    monkeypatch.setattr(database_security, "check_database", database_check)
    monkeypatch.setattr(database, "AsyncSessionLocal", db_session)
    monkeypatch.setattr(model_profiles, "get_model_profile_service", lambda: SimpleNamespace(ensure_seeded=AsyncMock(), profile_count=0))
    monkeypatch.setattr(rag, "get_rag_service", lambda: SimpleNamespace(startup=AsyncMock()))
    monkeypatch.setattr(job_runner, "recover_stale_document_ingest_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(job_runner, "start_document_ingest_runner", AsyncMock())
    monkeypatch.setattr(job_runner, "shutdown_document_ingest_runner", lambda: None)
    monkeypatch.setattr(registry, "get_mcp_registry", lambda: SimpleNamespace(startup=AsyncMock(), shutdown=close_mcp))
    monkeypatch.setattr(scheduler, "start_scheduler", AsyncMock())
    monkeypatch.setattr(scheduler, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(local_model_lifecycle, "release_tracked_local_models", release_models)
    monkeypatch.setattr(main.settings, "local_model_release_on_shutdown", True)
    coding = SimpleNamespace(startup=AsyncMock(return_value=0), shutdown=close_coding) if enabled else None
    if failure == "startup":
        coding.startup = AsyncMock(side_effect=RuntimeError("Fixture lifespan failure"))
    monkeypatch.setattr(bootstrap, "get_coding_session_service", lambda: coding)

    async def run_context():
        async with main.lifespan(main.app):
            assert main.app.state.coding_sessions is coding
            if failure == "context":
                raise RuntimeError("Fixture lifespan failure")

    if failure:
        with pytest.raises(RuntimeError, match="Fixture lifespan failure"):
            await run_context()
    else:
        await run_context()
    database_check.assert_awaited_once()
    assert closed == (["coding"] if enabled else []) + ["mcp", "models", "database"]
