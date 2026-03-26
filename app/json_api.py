"""
FruitcakeAI v5 — Backend-owned JSON/API helpers.

This is the first JSON/API sprint substrate: a narrow, reusable HTTP JSON
fetch helper plus a structured place lookup built on OpenStreetMap Nominatim.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_JSON_TIMEOUT = 12.0
_PLACE_SEARCH_URL = "https://nominatim.openstreetmap.org/search"


class JsonApiError(RuntimeError):
    """Raised when a backend-owned JSON/API call fails."""


async def fetch_json(
    *,
    url: str,
    params: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    timeout_seconds: float = _JSON_TIMEOUT,
) -> Any:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": f"{settings.app_name}/{settings.app_version} (JSON API)",
    }
    if headers:
        request_headers.update(headers)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=request_headers)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise JsonApiError("JSON API request timed out.") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise JsonApiError(f"JSON API request failed with HTTP {status}.") from exc
    except httpx.HTTPError as exc:
        raise JsonApiError("JSON API request failed.") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise JsonApiError("JSON API response was not valid JSON.") from exc


def _format_place_result(item: Dict[str, Any], index: int) -> str:
    name = (
        item.get("name")
        or item.get("display_name")
        or item.get("namedetails", {}).get("name")
        or "Unnamed place"
    )
    display_name = str(item.get("display_name") or "").strip()
    lat = str(item.get("lat") or "").strip()
    lon = str(item.get("lon") or "").strip()

    extras: List[str] = []
    if display_name and display_name != name:
        extras.append(display_name)
    if lat and lon:
        extras.append(f"lat={lat}, lon={lon}")

    suffix = f" — {' | '.join(extras)}" if extras else ""
    return f"[{index}] {name}{suffix}"


async def search_places(*, query: str, near: str | None = None, limit: int = 5) -> str:
    q = (query or "").strip()
    near_value = (near or "").strip()
    if not q:
        return "No place query provided."

    limit = max(1, min(int(limit or 5), 8))
    combined_query = q if not near_value else f"{q}, {near_value}"
    payload = await fetch_json(
        url=_PLACE_SEARCH_URL,
        params={
            "q": combined_query,
            "format": "jsonv2",
            "addressdetails": 1,
            "namedetails": 1,
            "limit": limit,
        },
    )

    if not isinstance(payload, list) or not payload:
        context = f" near {near_value}" if near_value else ""
        return f"No places found for: {q}{context}"

    lines = [f"Place search results for: {q}"]
    if near_value:
        lines[0] += f" near {near_value}"
    lines.append("")
    for idx, item in enumerate(payload[:limit], start=1):
        if isinstance(item, dict):
            lines.append(_format_place_result(item, idx))
    return "\n".join(lines)
