"""
FruitcakeAI v5 — Backend-owned JSON/API helpers.

This is the first JSON/API sprint substrate: a narrow, reusable HTTP JSON
fetch helper plus deterministic field extraction for backend-owned contracts.
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


def _split_json_path(path: str) -> List[str]:
    cleaned = str(path or "").strip()
    if not cleaned:
        raise JsonApiError("JSON field path is required.")

    tokens: List[str] = []
    current = []
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if char == ".":
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue
        if char == "[":
            if current:
                tokens.append("".join(current))
                current = []
            close = cleaned.find("]", index)
            if close == -1:
                raise JsonApiError(f"Invalid JSON field path '{path}'.")
            inner = cleaned[index + 1 : close].strip()
            if not inner:
                raise JsonApiError(f"Invalid JSON field path '{path}'.")
            if not inner.isdigit():
                raise JsonApiError(f"JSON field path '{path}' must use numeric list indexes inside brackets.")
            tokens.append(inner)
            index = close + 1
            continue
        current.append(char)
        index += 1

    if current:
        tokens.append("".join(current))
    return tokens


def extract_json_path(payload: Any, path: str) -> Any:
    """Extract a deterministic value from a JSON-compatible payload."""

    current = payload
    for token in _split_json_path(path):
        if isinstance(current, dict):
            if token not in current:
                raise JsonApiError(f"JSON field '{path}' was missing.")
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                raise JsonApiError(f"JSON field '{path}' expected a list index but found '{token}'.")
            index = int(token)
            if index < 0 or index >= len(current):
                raise JsonApiError(f"JSON field '{path}' was missing.")
            current = current[index]
            continue
        raise JsonApiError(f"JSON field '{path}' was missing.")

    if current is None:
        raise JsonApiError(f"JSON field '{path}' was missing.")
    return current


def extract_json_fields(payload: Any, fields: Dict[str, str]) -> Dict[str, Any]:
    """Extract a normalized mapping of named JSON fields from a payload."""

    if not isinstance(fields, dict) or not fields:
        raise JsonApiError("JSON field selectors must be a non-empty object.")

    extracted: Dict[str, Any] = {}
    for field_name, path in fields.items():
        name = str(field_name or "").strip()
        selector = str(path or "").strip()
        if not name:
            raise JsonApiError("JSON field selectors must use non-empty names.")
        extracted[name] = extract_json_path(payload, selector)
    return extracted


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
