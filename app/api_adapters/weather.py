from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.api_adapters.base import AdapterExecutionResult, APIAdapter
from app.api_errors import APIRequestError
from app.json_api import JsonApiError, fetch_json

_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
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
        if endpoint_name != "current_conditions":
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
            raw = await fetch_json(
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

        normalized = _normalize_current_conditions(raw)
        return AdapterExecutionResult(
            normalized=normalized,
            formatter=_format_current_conditions,
            raw_payload=raw,
        )
