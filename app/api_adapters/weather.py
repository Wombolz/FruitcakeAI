from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.api_adapters.base import AdapterExecutionResult, APIAdapter
from app.api_errors import APIRequestError
from app.json_api import JsonApiError, fetch_json

_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
_ALLOWED_UNITS = {"standard", "metric", "imperial"}


def _normalize_current_conditions(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIRequestError("Weather returned an invalid response shape.")

    coord = raw.get("coord")
    main = raw.get("main")
    wind = raw.get("wind")
    weather_list = raw.get("weather")
    sys = raw.get("sys")
    if not isinstance(coord, dict) or not isinstance(main, dict) or not isinstance(wind, dict) or not isinstance(sys, dict):
        raise APIRequestError("Weather response did not include the expected current-weather payload.")
    if not isinstance(weather_list, list) or not weather_list or not isinstance(weather_list[0], dict):
        raise APIRequestError("Weather response did not include weather conditions.")

    weather = weather_list[0]
    dt = raw.get("dt")
    timezone_offset = int(raw.get("timezone") or 0)
    observed_at_utc = ""
    if dt is not None:
        try:
            observed_at_utc = datetime.fromtimestamp(int(dt), tz=timezone.utc).isoformat()
        except Exception:
            observed_at_utc = ""

    temperature_c = main.get("temp")
    feels_like_c = main.get("feels_like")
    humidity = main.get("humidity")
    pressure = main.get("pressure")
    wind_speed = wind.get("speed")
    wind_direction = wind.get("deg")
    sunrise = sys.get("sunrise")
    sunset = sys.get("sunset")
    is_day = None
    try:
        if dt is not None and sunrise is not None and sunset is not None:
            is_day = int(sunrise) <= int(dt) <= int(sunset)
    except Exception:
        is_day = None

    return {
        "location": {
            "latitude": coord.get("lat"),
            "longitude": coord.get("lon"),
            "city_name": str(raw.get("name") or "").strip() or None,
            "country": str(sys.get("country") or "").strip() or None,
            "timezone_offset_seconds": timezone_offset,
        },
        "current_weather": {
            "observed_at_utc": observed_at_utc,
            "temperature_c": float(temperature_c) if temperature_c is not None else None,
            "feels_like_c": float(feels_like_c) if feels_like_c is not None else None,
            "humidity_percent": int(humidity) if humidity is not None else None,
            "pressure_hpa": int(pressure) if pressure is not None else None,
            "wind_speed_mps": float(wind_speed) if wind_speed is not None else None,
            "wind_direction_deg": int(wind_direction) if wind_direction is not None else None,
            "weather_code": int(weather.get("id")) if weather.get("id") is not None else None,
            "weather_main": str(weather.get("main") or "").strip() or None,
            "description": str(weather.get("description") or "").strip() or None,
            "icon": str(weather.get("icon") or "").strip() or None,
            "sunrise_utc": int(sunrise) if sunrise is not None else None,
            "sunset_utc": int(sunset) if sunset is not None else None,
            "is_day": is_day,
        },
    }


def _resolve_forecast_tzinfo(*, timezone_name: str | None, offset_seconds: int) -> timezone:
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(timezone_name)
        except Exception:
            pass
    return timezone(timedelta(seconds=offset_seconds))


def _normalize_forecast_snapshot(raw: Any, *, timezone_name: str | None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIRequestError("Weather forecast returned an invalid response shape.")

    city = raw.get("city")
    entries = raw.get("list")
    if not isinstance(city, dict) or not isinstance(entries, list):
        raise APIRequestError("Weather forecast response did not include the expected forecast payload.")

    timezone_offset = int(city.get("timezone") or 0)
    tzinfo = _resolve_forecast_tzinfo(timezone_name=timezone_name, offset_seconds=timezone_offset)
    filtered_entries: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        weather_list = item.get("weather")
        main = item.get("main")
        wind = item.get("wind") if isinstance(item.get("wind"), dict) else {}
        if not isinstance(main, dict) or not isinstance(weather_list, list) or not weather_list or not isinstance(weather_list[0], dict):
            continue
        weather = weather_list[0]
        try:
            utc_dt = datetime.fromtimestamp(int(item.get("dt")), tz=timezone.utc)
        except Exception:
            continue
        local_dt = utc_dt.astimezone(tzinfo)
        precip_probability = item.get("pop")
        rain = item.get("rain") if isinstance(item.get("rain"), dict) else {}
        snow = item.get("snow") if isinstance(item.get("snow"), dict) else {}
        filtered_entries.append(
            {
                "time_utc": utc_dt.isoformat(),
                "time_local": local_dt.strftime("%Y-%m-%d %I:%M %p %Z"),
                "date_local": local_dt.date().isoformat(),
                "temperature_c": float(main.get("temp")) if main.get("temp") is not None else None,
                "feels_like_c": float(main.get("feels_like")) if main.get("feels_like") is not None else None,
                "temp_min_c": float(main.get("temp_min")) if main.get("temp_min") is not None else None,
                "temp_max_c": float(main.get("temp_max")) if main.get("temp_max") is not None else None,
                "humidity_percent": int(main.get("humidity")) if main.get("humidity") is not None else None,
                "pressure_hpa": int(main.get("pressure")) if main.get("pressure") is not None else None,
                "precip_probability": float(precip_probability) if precip_probability is not None else None,
                "rain_mm_3h": float(rain.get("3h")) if rain.get("3h") is not None else None,
                "snow_mm_3h": float(snow.get("3h")) if snow.get("3h") is not None else None,
                "wind_speed_mps": float(wind.get("speed")) if wind.get("speed") is not None else None,
                "wind_direction_deg": int(wind.get("deg")) if wind.get("deg") is not None else None,
                "weather_code": int(weather.get("id")) if weather.get("id") is not None else None,
                "weather_main": str(weather.get("main") or "").strip() or None,
                "description": str(weather.get("description") or "").strip() or None,
                "icon": str(weather.get("icon") or "").strip() or None,
            }
        )

    if not filtered_entries:
        raise APIRequestError("Weather forecast response did not include usable forecast entries.")

    today_local = filtered_entries[0]["date_local"]
    today_entries = [item for item in filtered_entries if item.get("date_local") == today_local]
    today_highs = [item["temp_max_c"] for item in today_entries if item.get("temp_max_c") is not None]
    today_lows = [item["temp_min_c"] for item in today_entries if item.get("temp_min_c") is not None]
    today_pop = [item["precip_probability"] for item in today_entries if item.get("precip_probability") is not None]
    summary_entry = max(
        today_entries,
        key=lambda item: (
            item.get("precip_probability") is not None,
            item.get("precip_probability") or 0.0,
            item.get("temp_max_c") or float("-inf"),
        ),
    )

    return {
        "location": {
            "latitude": city.get("coord", {}).get("lat") if isinstance(city.get("coord"), dict) else None,
            "longitude": city.get("coord", {}).get("lon") if isinstance(city.get("coord"), dict) else None,
            "city_name": str(city.get("name") or "").strip() or None,
            "country": str(city.get("country") or "").strip() or None,
            "timezone_offset_seconds": timezone_offset,
        },
        "forecast": {
            "provider": "openweathermap_forecast",
            "today": {
                "date_local": today_local,
                "high_c": max(today_highs) if today_highs else None,
                "low_c": min(today_lows) if today_lows else None,
                "max_precip_probability": max(today_pop) if today_pop else None,
                "summary": summary_entry.get("description") or summary_entry.get("weather_main"),
            },
            "next_periods": filtered_entries[:4],
        },
    }


def _format_current_conditions(normalized: Dict[str, Any], deduped: bool) -> str:
    location = normalized.get("location") or {}
    weather = normalized.get("current_weather") or {}
    if deduped:
        return "No new weather changes since the last successful check."

    lines = ["Weather current conditions:", ""]
    city = str(location.get("city_name") or "").strip()
    country = str(location.get("country") or "").strip()
    if city or country:
        location_label = ", ".join([part for part in [city, country] if part])
        lines.append(f"location={location_label}")
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is not None and longitude is not None:
        lines.append(f"coordinates={float(latitude):.4f}, {float(longitude):.4f}")
    if weather.get("observed_at_utc"):
        lines.append(f"observed_at_utc={weather['observed_at_utc']}")
    if weather.get("temperature_c") is not None:
        lines.append(f"temperature_c={float(weather['temperature_c']):.1f}")
    if weather.get("feels_like_c") is not None:
        lines.append(f"feels_like_c={float(weather['feels_like_c']):.1f}")
    if weather.get("humidity_percent") is not None:
        lines.append(f"humidity_percent={int(weather['humidity_percent'])}")
    if weather.get("pressure_hpa") is not None:
        lines.append(f"pressure_hpa={int(weather['pressure_hpa'])}")
    if weather.get("wind_speed_mps") is not None:
        lines.append(f"wind_speed_mps={float(weather['wind_speed_mps']):.1f}")
    if weather.get("wind_direction_deg") is not None:
        lines.append(f"wind_direction_deg={int(weather['wind_direction_deg'])}")
    if weather.get("weather_code") is not None:
        lines.append(f"weather_code={int(weather['weather_code'])}")
    if weather.get("weather_main"):
        lines.append(f"weather_main={weather['weather_main']}")
    if weather.get("description"):
        lines.append(f"description={weather['description']}")
    if weather.get("is_day") is not None:
        lines.append(f"is_day={bool(weather['is_day'])}")
    return "\n".join(lines)


class WeatherAdapter(APIAdapter):
    service_name = "weather"

    async def execute(
        self,
        *,
        endpoint: str,
        query_params: Dict[str, Any],
        api_key: str | None,
    ) -> AdapterExecutionResult:
        endpoint_name = str(endpoint or "").strip().lower()
        params = dict(query_params or {})
        if endpoint_name not in {"current_conditions", "briefing_snapshot"}:
            raise APIRequestError(f"Unsupported endpoint '{endpoint}'.")
        if not api_key:
            raise APIRequestError("Weather requests require a named secret.")

        latitude = params.get("latitude")
        longitude = params.get("longitude")
        if latitude in (None, ""):
            latitude = params.get("lat")
        if longitude in (None, ""):
            longitude = params.get("lon")
        units = str(params.get("units") or "metric").strip().lower() or "metric"
        if units not in _ALLOWED_UNITS:
            raise APIRequestError("Unsupported weather units. Use standard, metric, or imperial.")

        missing = []
        if latitude in (None, ""):
            missing.append("latitude")
        if longitude in (None, ""):
            missing.append("longitude")
        if missing:
            raise APIRequestError(f"Missing required query params: {', '.join(missing)}")

        try:
            current_raw = await fetch_json(
                url=_WEATHER_URL,
                params={
                    "lat": latitude,
                    "lon": longitude,
                    "appid": api_key,
                    "units": units,
                },
            )
        except JsonApiError as exc:
            raise APIRequestError(str(exc)) from exc

        current_normalized = _normalize_current_conditions(current_raw)
        if endpoint_name == "current_conditions":
            return AdapterExecutionResult(
                normalized=current_normalized,
                formatter=_format_current_conditions,
                raw_payload=current_raw,
            )

        timezone_name = str(params.get("display_timezone") or "").strip() or None
        try:
            forecast_raw = await fetch_json(
                url=_FORECAST_URL,
                params={
                    "lat": latitude,
                    "lon": longitude,
                    "appid": api_key,
                    "units": units,
                },
            )
        except JsonApiError as exc:
            raise APIRequestError(str(exc)) from exc

        forecast_normalized = _normalize_forecast_snapshot(forecast_raw, timezone_name=timezone_name)
        merged = {
            "location": current_normalized.get("location") or forecast_normalized.get("location") or {},
            "current_weather": current_normalized.get("current_weather") or {},
            "forecast": forecast_normalized.get("forecast") or {},
        }
        return AdapterExecutionResult(
            normalized=merged,
            formatter=_format_current_conditions,
            raw_payload={
                "current_conditions": current_raw,
                "forecast": forecast_raw,
            },
        )
