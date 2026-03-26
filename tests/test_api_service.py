from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.api_service import APIRequestError, execute_api_request
from app.db.models import Secret, Task, TaskAPIState, User
from app.secrets_service import encrypt_secret_value
from tests.conftest import TestSessionLocal


async def _seed_user_task_and_secret() -> tuple[int, int]:
    async with TestSessionLocal() as db:
        user = User(
            username="apiuser",
            email="apiuser@example.com",
            hashed_password="x",
            role="parent",
            persona="family_assistant",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        task = Task(
            user_id=user.id,
            title="ISS Passes",
            instruction="Fetch ISS passes",
            task_type="recurring",
            schedule="every:1h",
            status="pending",
            deliver=True,
        )
        db.add(task)
        secret = Secret(
            user_id=user.id,
            name="n2yo_api_key",
            provider="n2yo",
            ciphertext=encrypt_secret_value("real-api-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()
        return int(user.id), int(task.id)


@pytest.mark.asyncio
async def test_execute_api_request_rejects_unknown_service():
    async with TestSessionLocal() as db:
        with pytest.raises(APIRequestError, match="Unsupported service"):
            await execute_api_request(
                db,
                user_id=1,
                service="unknown",
                endpoint="anything",
                query_params={},
            )


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_n2yo_and_dedupes_for_task():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "passes": [
            {"startUTC": 1774578600, "duration": 348, "maxEl": 67, "startAz": 280, "endAz": 120}
        ]
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            first = await execute_api_request(
                db,
                user_id=user_id,
                service="n2yo",
                endpoint="iss_visual_passes",
                query_params={
                    "satellite_id": 25544,
                    "lat": 32.4485,
                    "lon": -81.7832,
                    "alt_meters": 60,
                    "days": 1,
                    "min_visibility_seconds": 120,
                },
                secret_name="n2yo_api_key",
                task_id=task_id,
            )
            second = await execute_api_request(
                db,
                user_id=user_id,
                service="n2yo",
                endpoint="iss_visual_passes",
                query_params={
                    "satellite_id": 25544,
                    "lat": 32.4485,
                    "lon": -81.7832,
                    "alt_meters": 60,
                    "days": 1,
                    "min_visibility_seconds": 120,
                },
                secret_name="n2yo_api_key",
                task_id=task_id,
            )
            await db.commit()

        assert "ISS visible pass results" in first
        assert second == "No new ISS pass changes since the last successful check."

        state = (
            await db.execute(select(TaskAPIState).where(TaskAPIState.task_id == task_id))
        ).scalar_one()
        payload = json.loads(state.value_json)
        assert payload["normalized"]["passes"][0]["duration_seconds"] == 348


@pytest.mark.asyncio
async def test_execute_api_request_requires_secret_for_n2yo():
    user_id, _task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        with pytest.raises(APIRequestError, match="named secret"):
            await execute_api_request(
                db,
                user_id=user_id,
                service="n2yo",
                endpoint="iss_visual_passes",
                query_params={
                    "satellite_id": 25544,
                    "lat": 32.4485,
                    "lon": -81.7832,
                    "alt_meters": 60,
                    "days": 1,
                    "min_visibility_seconds": 120,
                },
            )
