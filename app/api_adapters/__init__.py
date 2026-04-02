from __future__ import annotations

from app.api_adapters.alphavantage import AlphaVantageAdapter
from app.api_adapters.base import APIAdapter
from app.api_adapters.n2yo import N2YOAdapter
from app.api_adapters.weather import WeatherAdapter

_ADAPTERS: dict[str, APIAdapter] = {
    "n2yo": N2YOAdapter(),
    "alphavantage": AlphaVantageAdapter(),
    "weather": WeatherAdapter(),
}


def get_api_adapter(service_name: str) -> APIAdapter | None:
    return _ADAPTERS.get(str(service_name or "").strip().lower())
