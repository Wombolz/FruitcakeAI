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


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_alphavantage_global_quote():
    user_id, task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        secret = Secret(
            user_id=user_id,
            name="alphavantage_api_key",
            provider="alphavantage",
            ciphertext=encrypt_secret_value("alpha-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()

    payload = {
        "Global Quote": {
            "01. symbol": "IBM",
            "02. open": "254.7100",
            "03. high": "255.4400",
            "04. low": "253.7700",
            "05. price": "255.1400",
            "06. volume": "3475028",
            "07. latest trading day": "2026-03-27",
            "08. previous close": "253.5400",
            "09. change": "1.6000",
            "10. change percent": "0.6311%",
        }
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="global_quote",
                query_params={"symbol": "IBM"},
                secret_name="alphavantage_api_key",
                task_id=task_id,
            )

    assert "Alpha Vantage global quote for IBM" in result
    assert "price=255.14" in result
    assert "change=1.60 (0.6311%)" in result


@pytest.mark.asyncio
async def test_execute_api_request_reports_alphavantage_rate_limit():
    user_id, _task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        secret = Secret(
            user_id=user_id,
            name="alphavantage_api_key",
            provider="alphavantage",
            ciphertext=encrypt_secret_value("alpha-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()

    payload = {
        "Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            with pytest.raises(APIRequestError, match="rate limit"):
                await execute_api_request(
                    db,
                    user_id=user_id,
                    service="alphavantage",
                    endpoint="global_quote",
                    query_params={"symbol": "IBM"},
                    secret_name="alphavantage_api_key",
                )


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_alphavantage_time_series_daily():
    user_id, task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        secret = Secret(
            user_id=user_id,
            name="alphavantage_api_key",
            provider="alphavantage",
            ciphertext=encrypt_secret_value("alpha-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()

    payload = {
        "Meta Data": {
            "1. Information": "Daily Prices (open, high, low, close) and Volumes",
            "2. Symbol": "IBM",
            "3. Last Refreshed": "2026-03-27",
            "4. Output Size": "Compact",
            "5. Time Zone": "US/Eastern",
        },
        "Time Series (Daily)": {
            "2026-03-27": {
                "1. open": "241.00",
                "2. high": "242.25",
                "3. low": "239.75",
                "4. close": "241.67",
                "5. volume": "3606840",
            },
            "2026-03-26": {
                "1. open": "240.10",
                "2. high": "241.50",
                "3. low": "238.90",
                "4. close": "241.39",
                "5. volume": "3500000",
            },
        },
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="time_series_daily",
                query_params={"symbol": "IBM", "limit": 2},
                secret_name="alphavantage_api_key",
                task_id=task_id,
            )

    assert "Alpha Vantage daily time series for IBM" in result
    assert "[1] date=2026-03-27 | open=241.00 | high=242.25 | low=239.75 | close=241.67" in result
    assert "[2] date=2026-03-26 | open=240.10 | high=241.50 | low=238.90 | close=241.39" in result


@pytest.mark.asyncio
async def test_execute_api_request_rejects_invalid_alphavantage_daily_outputsize():
    user_id, _task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        secret = Secret(
            user_id=user_id,
            name="alphavantage_api_key",
            provider="alphavantage",
            ciphertext=encrypt_secret_value("alpha-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()

        with pytest.raises(APIRequestError, match="outputsize"):
            await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="time_series_daily",
                query_params={"symbol": "IBM", "outputsize": "bad"},
                secret_name="alphavantage_api_key",
            )
