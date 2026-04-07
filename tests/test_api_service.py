from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.api_adapters.base import AdapterExecutionResult
from app.api_service import APIRequestError, execute_api_request
from app.config import settings
from app.db.models import Secret, SecretAccessEvent, Task, TaskAPIState, User
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
        weather_secret = Secret(
            user_id=user.id,
            name="openweathermap_api_key",
            provider="weather",
            ciphertext=encrypt_secret_value("weather-api-key"),
            is_active=True,
        )
        db.add(weather_secret)
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
async def test_execute_api_request_normalizes_weather_current_conditions_and_dedupes_for_task():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [
            {
                "id": 802,
                "main": "Clouds",
                "description": "scattered clouds",
                "icon": "03d",
            }
        ],
        "main": {
            "temp": 22.3,
            "feels_like": 23.1,
            "pressure": 1014,
            "humidity": 64,
        },
        "wind": {"speed": 11.4, "deg": 270},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "id": 4591484,
        "name": "Statesboro",
    }

    async with TestSessionLocal() as db:
        with patch("app.api_adapters.weather.fetch_json", new=AsyncMock(return_value=payload)):
            first = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )
            second = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )

    assert '"city_name": "Statesboro"' in first
    assert '"deduped": false' in first
    assert '"deduped": true' in second


@pytest.mark.asyncio
async def test_execute_api_request_accepts_lat_lon_aliases_for_weather():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [
            {
                "id": 802,
                "main": "Clouds",
                "description": "scattered clouds",
                "icon": "03d",
            }
        ],
        "main": {
            "temp": 22.3,
            "feels_like": 23.1,
            "pressure": 1014,
            "humidity": 64,
        },
        "wind": {"speed": 11.4, "deg": 270},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "id": 4591484,
        "name": "Statesboro",
    }

    async with TestSessionLocal() as db:
        with patch("app.api_adapters.weather.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "lat": 32.4485,
                    "lon": -81.7832,
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )

    assert '"city_name": "Statesboro"' in result


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_weather_briefing_snapshot():
    user_id, task_id = await _seed_user_task_and_secret()
    current_payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [{"id": 802, "main": "Clouds", "description": "scattered clouds", "icon": "03d"}],
        "main": {"temp": 22.3, "feels_like": 23.1, "pressure": 1014, "humidity": 64},
        "wind": {"speed": 4.5, "deg": 270},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "name": "Statesboro",
    }
    forecast_payload = {
        "city": {
            "name": "Statesboro",
            "country": "US",
            "timezone": -14400,
            "coord": {"lat": 32.4485, "lon": -81.7832},
        },
        "list": [
            {
                "dt": 1775048400,
                "main": {"temp": 24.0, "feels_like": 24.8, "temp_min": 22.0, "temp_max": 26.0, "pressure": 1012, "humidity": 62},
                "weather": [{"id": 500, "main": "Rain", "description": "light rain", "icon": "10d"}],
                "wind": {"speed": 3.8, "deg": 250},
                "pop": 0.35,
            },
            {
                "dt": 1775059200,
                "main": {"temp": 19.0, "feels_like": 18.5, "temp_min": 17.0, "temp_max": 21.0, "pressure": 1015, "humidity": 70},
                "weather": [{"id": 803, "main": "Clouds", "description": "broken clouds", "icon": "04d"}],
                "wind": {"speed": 2.6, "deg": 220},
                "pop": 0.10,
            },
        ],
    }

    async with TestSessionLocal() as db:
        with patch(
            "app.api_adapters.weather.fetch_json",
            new=AsyncMock(side_effect=[current_payload, forecast_payload]),
        ):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="briefing_snapshot",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                    "display_timezone": "America/New_York",
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                    "forecast": "forecast",
                },
            )

    assert '"city_name": "Statesboro"' in result
    assert '"forecast"' in result
    assert '"high_c": 26.0' in result
    assert '"low_c": 17.0' in result
    assert '"max_precip_probability": 0.35' in result


@pytest.mark.asyncio
async def test_execute_api_request_honors_weather_units_argument():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [{"id": 800, "main": "Clear", "description": "clear sky", "icon": "01d"}],
        "main": {"temp": 72.0, "feels_like": 73.0, "pressure": 1013, "humidity": 55},
        "wind": {"speed": 8.0, "deg": 180},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "name": "Statesboro",
    }

    async with TestSessionLocal() as db:
        with patch("app.api_adapters.weather.fetch_json", new=AsyncMock(return_value=payload)) as mocked:
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                    "units": "imperial",
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )

    assert '"city_name": "Statesboro"' in result
    assert mocked.await_args.kwargs["params"]["units"] == "imperial"


@pytest.mark.asyncio
async def test_execute_api_request_accepts_openweathermap_provider_alias_for_weather():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [{"id": 800, "main": "Clear", "description": "clear sky", "icon": "01d"}],
        "main": {"temp": 20.0, "feels_like": 20.0, "pressure": 1013, "humidity": 55},
        "wind": {"speed": 2.0, "deg": 180},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "name": "Statesboro",
    }

    async with TestSessionLocal() as db:
        secret = (
            await db.execute(select(Secret).where(Secret.user_id == user_id, Secret.name == "openweathermap_api_key"))
        ).scalar_one()
        secret.provider = "openweathermap"
        await db.flush()

        with patch("app.api_adapters.weather.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )

    assert '"city_name": "Statesboro"' in result


@pytest.mark.asyncio
async def test_execute_api_request_accepts_weather_secret_when_provider_label_drifts():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "coord": {"lat": 32.4485, "lon": -81.7832},
        "weather": [{"id": 800, "main": "Clear", "description": "clear sky", "icon": "01d"}],
        "main": {"temp": 20.0, "feels_like": 20.0, "pressure": 1013, "humidity": 55},
        "wind": {"speed": 2.0, "deg": 180},
        "dt": 1775037600,
        "sys": {"country": "US", "sunrise": 1775006400, "sunset": 1775052000},
        "timezone": -14400,
        "name": "Statesboro",
    }

    async with TestSessionLocal() as db:
        secret = (
            await db.execute(select(Secret).where(Secret.user_id == user_id, Secret.name == "openweathermap_api_key"))
        ).scalar_one()
        secret.provider = "OpenWeatherMap API"
        await db.flush()

        with patch("app.api_adapters.weather.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="weather",
                endpoint="current_conditions",
                query_params={
                    "latitude": 32.4485,
                    "longitude": -81.7832,
                },
                task_id=task_id,
                secret_name="openweathermap_api_key",
                response_fields={
                    "location": "location",
                    "current_weather": "current_weather",
                },
            )

    assert '"city_name": "Statesboro"' in result


@pytest.mark.asyncio
async def test_execute_api_request_supports_response_field_extraction():
    user_id, task_id = await _seed_user_task_and_secret()

    class _Adapter:
        service_name = "n2yo"

        async def execute(self, *, endpoint, query_params, api_key):
            return AdapterExecutionResult(
                normalized={
                    "passes": [
                        {
                            "start_utc": "2026-04-01T09:30:00+00:00",
                            "max_elevation_deg": 67.0,
                        }
                    ]
                },
                formatter=lambda normalized, deduped: "should not be used",
                raw_payload={"passes": [{"startUTC": 1775035800, "maxEl": 67}]},
            )

    async with TestSessionLocal() as db:
        with patch("app.api_service.get_api_adapter", return_value=_Adapter()):
            result = await execute_api_request(
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
                response_fields={
                    "start_utc": "passes[0].start_utc",
                    "max_elevation_deg": "passes[0].max_elevation_deg",
                },
            )
            repeat = await execute_api_request(
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
                response_fields={
                    "start_utc": "passes[0].start_utc",
                    "max_elevation_deg": "passes[0].max_elevation_deg",
                },
            )

    assert result == '{"deduped": false, "fields": {"max_elevation_deg": 67.0, "start_utc": "2026-04-01T09:30:00+00:00"}}'
    assert repeat == '{"deduped": true, "fields": {"max_elevation_deg": 67.0, "start_utc": "2026-04-01T09:30:00+00:00"}}'


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_n2yo_and_dedupes_for_task():
    user_id, task_id = await _seed_user_task_and_secret()
    payload = {
        "passes": [
            {"startUTC": 1774578600, "duration": 348, "maxEl": 67, "startAz": 280, "endAz": 120}
        ]
    }

    async with TestSessionLocal() as db:
        with patch("app.api_adapters.n2yo.fetch_json", new=AsyncMock(return_value=payload)):
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
        events = (
            await db.execute(select(SecretAccessEvent).where(SecretAccessEvent.task_id == task_id))
        ).scalars().all()
        assert len(events) == 2
        assert all(event.success is True for event in events)


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
async def test_execute_api_request_reports_secret_decryption_failure_helpfully():
    user_id, task_id = await _seed_user_task_and_secret()
    original = settings.secrets_master_key
    try:
        settings.secrets_master_key = "different-test-master-key"
        async with TestSessionLocal() as db:
            with pytest.raises(APIRequestError, match="Rotate this secret or verify SECRETS_MASTER_KEY"):
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
                    secret_name="n2yo_api_key",
                    task_id=task_id,
                )
    finally:
        settings.secrets_master_key = original


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
        with patch("app.api_adapters.alphavantage.fetch_json", new=AsyncMock(return_value=payload)):
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
        with patch("app.api_adapters.alphavantage.fetch_json", new=AsyncMock(return_value=payload)):
            with pytest.raises(APIRequestError, match="Thank you for using Alpha Vantage!"):
                await execute_api_request(
                    db,
                    user_id=user_id,
                    service="alphavantage",
                    endpoint="global_quote",
                    query_params={"symbol": "IBM"},
                    secret_name="alphavantage_api_key",
                )


@pytest.mark.asyncio
async def test_execute_api_request_rejects_unapproved_secret_name_for_service():
    user_id, _task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        with pytest.raises(APIRequestError, match="not approved for service 'n2yo'"):
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
                secret_name="some_other_secret",
            )


@pytest.mark.asyncio
async def test_execute_api_request_rejects_provider_mismatch_secret():
    user_id, _task_id = await _seed_user_task_and_secret()
    async with TestSessionLocal() as db:
        secret = (
            await db.execute(
                select(Secret).where(
                    Secret.user_id == user_id,
                    Secret.name == "n2yo_api_key",
                )
            )
        ).scalar_one()
        assert secret is not None
        secret.provider = "weather"
        await db.commit()

    async with TestSessionLocal() as db:
        with pytest.raises(APIRequestError, match="not a valid n2yo credential"):
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
                secret_name="n2yo_api_key",
            )


@pytest.mark.asyncio
async def test_execute_api_request_stores_raw_payload_for_task_state():
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
        with patch("app.api_adapters.alphavantage.fetch_json", new=AsyncMock(return_value=payload)):
            await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="global_quote",
                query_params={"symbol": "IBM"},
                secret_name="alphavantage_api_key",
                task_id=task_id,
            )
            await db.commit()

        state = (
            await db.execute(select(TaskAPIState).where(TaskAPIState.task_id == task_id))
        ).scalar_one()
        stored = json.loads(state.value_json)
        assert stored["raw_payload"]["Global Quote"]["01. symbol"] == "IBM"


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
        with patch("app.api_adapters.alphavantage.fetch_json", new=AsyncMock(return_value=payload)):
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


@pytest.mark.asyncio
async def test_execute_api_request_normalizes_alphavantage_time_series_intraday():
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
            "1. Information": "Intraday (60min) open, high, low, close prices and volume",
            "2. Symbol": "SPY",
            "4. Interval": "60min",
        },
        "Time Series (60min)": {
            "2026-03-27 16:00:00": {
                "1. open": "552.10",
                "2. high": "555.25",
                "3. low": "549.80",
                "4. close": "553.42",
                "5. volume": "1000000",
            },
            "2026-03-27 15:00:00": {
                "1. open": "548.00",
                "2. high": "553.00",
                "3. low": "547.50",
                "4. close": "552.00",
                "5. volume": "900000",
            },
        },
    }

    async with TestSessionLocal() as db:
        with patch("app.api_adapters.alphavantage.fetch_json", new=AsyncMock(return_value=payload)):
            result = await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="time_series_intraday",
                query_params={"symbol": "SPY", "interval": "60min", "limit": 2},
                secret_name="alphavantage_api_key",
                task_id=task_id,
            )

    assert "Alpha Vantage intraday time series for SPY (60min)" in result
    assert "[1] timestamp=2026-03-27 16:00:00 | open=552.10 | high=555.25 | low=549.80 | close=553.42" in result


@pytest.mark.asyncio
async def test_execute_api_request_rejects_invalid_alphavantage_intraday_interval():
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

        with pytest.raises(APIRequestError, match="intraday interval"):
            await execute_api_request(
                db,
                user_id=user_id,
                service="alphavantage",
                endpoint="time_series_intraday",
                query_params={"symbol": "SPY", "interval": "2min"},
                secret_name="alphavantage_api_key",
            )
