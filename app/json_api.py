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
from app.agent.runtime.models import ToolOutputText

log = structlog.get_logger(__name__)

_JSON_TIMEOUT = 12.0
_NOMINATIM_PLACE_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
_BRAVE_PLACE_SEARCH_URL = "https://api.search.brave.com/res/v1/local/place_search"


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


def _clean_place_value(value: Any, *, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _coordinate(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_nominatim_place(item: Dict[str, Any]) -> Dict[str, Any] | None:
    namedetails = item.get("namedetails") if isinstance(item.get("namedetails"), dict) else {}
    name = _clean_place_value(item.get("name") or namedetails.get("name") or item.get("display_name"), limit=200)
    if not name:
        return None
    place: Dict[str, Any] = {
        "name": name,
        "address": _clean_place_value(item.get("display_name")),
        "latitude": _coordinate(item.get("lat")),
        "longitude": _coordinate(item.get("lon")),
        "category": _clean_place_value(item.get("type") or item.get("category"), limit=120),
        "provider": "nominatim",
    }
    return {key: value for key, value in place.items() if value not in (None, "", [])}


def _normalize_brave_place(item: Dict[str, Any]) -> Dict[str, Any] | None:
    name = _clean_place_value(item.get("title") or item.get("name"), limit=200)
    if not name:
        return None
    postal = item.get("postal_address") if isinstance(item.get("postal_address"), dict) else {}
    contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    distance = item.get("distance") if isinstance(item.get("distance"), dict) else {}
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    thumbnail = item.get("thumbnail") if isinstance(item.get("thumbnail"), dict) else {}
    coordinates = item.get("coordinates") if isinstance(item.get("coordinates"), list) else []
    categories = item.get("categories") if isinstance(item.get("categories"), list) else []
    place: Dict[str, Any] = {
        "name": name,
        "address": _clean_place_value(postal.get("displayAddress") or item.get("address")),
        "latitude": _coordinate(coordinates[0]) if len(coordinates) > 0 else None,
        "longitude": _coordinate(coordinates[1]) if len(coordinates) > 1 else None,
        "category": _clean_place_value(categories[0] if categories else item.get("icon_category"), limit=120),
        "categories": [_clean_place_value(value, limit=120) for value in categories[:8] if _clean_place_value(value, limit=120)],
        "url": _clean_place_value(item.get("url") or action.get("url")),
        "phone": _clean_place_value(contact.get("telephone"), limit=80),
        "rating": _coordinate(rating.get("ratingValue")),
        "rating_max": _coordinate(rating.get("bestRating")),
        "review_count": rating.get("reviewCount") if isinstance(rating.get("reviewCount"), int) else None,
        "price_range": _clean_place_value(item.get("price_range"), limit=40),
        "distance": _coordinate(distance.get("value")),
        "distance_unit": _clean_place_value(distance.get("units"), limit=30),
        "thumbnail_url": _clean_place_value(thumbnail.get("src") or thumbnail.get("original")),
        "provider": "brave",
    }
    return {key: value for key, value in place.items() if value not in (None, "", [])}


def _format_place_result(item: Dict[str, Any], index: int) -> str:
    extras: List[str] = []
    address = _clean_place_value(item.get("address"))
    if address and address != item.get("name"):
        extras.append(address)
    rating = item.get("rating")
    if rating is not None:
        reviews = item.get("review_count")
        extras.append(f"rating={rating:g}" + (f" ({reviews} reviews)" if reviews is not None else ""))
    lat = item.get("latitude")
    lon = item.get("longitude")
    if lat is not None and lon is not None:
        extras.append(f"lat={lat:g}, lon={lon:g}")
    if item.get("url"):
        extras.append(f"URL: {item['url']}")
    suffix = f" — {' | '.join(extras)}" if extras else ""
    return f"[{index}] {item.get('name') or 'Unnamed place'}{suffix}"


async def _search_places_nominatim(query: str, near: str, limit: int) -> List[Dict[str, Any]]:
    combined_query = query if not near else f"{query}, {near}"
    payload = await fetch_json(
        url=_NOMINATIM_PLACE_SEARCH_URL,
        params={
            "q": combined_query,
            "format": "jsonv2",
            "addressdetails": 1,
            "namedetails": 1,
            "limit": limit,
        },
    )
    if not isinstance(payload, list):
        return []
    return [place for item in payload[:limit] if isinstance(item, dict) and (place := _normalize_nominatim_place(item))]


async def _search_places_brave(query: str, near: str, limit: int) -> List[Dict[str, Any]]:
    payload = await fetch_json(
        url=_BRAVE_PLACE_SEARCH_URL,
        params={
            "q": query,
            "location": near or None,
            "count": limit,
            "country": "US",
            "search_lang": "en",
            "units": "imperial",
        },
        headers={"X-Subscription-Token": settings.brave_search_api_key.strip()},
    )
    raw_results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(raw_results, list):
        return []
    return [place for item in raw_results[:limit] if isinstance(item, dict) and (place := _normalize_brave_place(item))]


def _place_search_output(
    *,
    query: str,
    near: str,
    places: List[Dict[str, Any]],
    provider: str,
    requested_provider: str,
    providers_attempted: List[str],
) -> ToolOutputText:
    context = f" near {near}" if near else ""
    if places:
        lines = [f"Place search results for: {query}{context}", f"Provider: {provider}", ""]
        lines.extend(_format_place_result(item, index) for index, item in enumerate(places, start=1))
    else:
        lines = [f"No places found for: {query}{context}"]
    citations = [
        {"url": place["url"], "title": place["name"], "source": provider}
        for place in places
        if place.get("url")
    ]
    return ToolOutputText(
        "\n".join(lines),
        structured_content={
            "schema_version": 1,
            "capability": "place_search",
            "provider": provider,
            "requested_provider": requested_provider,
            "providers_attempted": providers_attempted,
            "fallback_used": len(providers_attempted) > 1,
            "query": query,
            "near": near or None,
            "result_count": len(places),
            "places": places,
            "citations": citations,
        },
    )


async def search_places(
    *, query: str, near: str | None = None, limit: int = 5, provider: str | None = None
) -> ToolOutputText:
    q = (query or "").strip()
    near_value = (near or "").strip()
    if not q:
        return ToolOutputText("No place query provided.")

    limit = max(1, min(int(limit or 5), 8))
    requested = _clean_place_value(provider or settings.place_search_provider or "auto", limit=20).casefold()
    if requested not in {"auto", "brave", "nominatim"}:
        requested = "auto"
    brave_available = bool(settings.brave_search_api_key.strip())
    if requested == "nominatim":
        provider_order = ["nominatim"]
    elif requested == "brave":
        provider_order = ["brave"]
        if settings.brave_place_fallback_to_nominatim:
            provider_order.append("nominatim")
    else:
        provider_order = ["brave", "nominatim"] if brave_available else ["nominatim"]

    attempted: List[str] = []
    last_error: JsonApiError | None = None
    for selected in provider_order:
        attempted.append(selected)
        if selected == "brave" and not brave_available:
            last_error = JsonApiError("Brave Place Search is not configured.")
            continue
        try:
            places = (
                await _search_places_brave(q, near_value, limit)
                if selected == "brave"
                else await _search_places_nominatim(q, near_value, limit)
            )
        except JsonApiError as exc:
            last_error = exc
            log.warning("place_search provider_failed", provider=selected, query=q, error=str(exc))
            continue
        if places or selected == provider_order[-1]:
            return _place_search_output(
                query=q,
                near=near_value,
                places=places,
                provider=selected,
                requested_provider=requested,
                providers_attempted=attempted,
            )
    if last_error is not None:
        raise last_error
    return _place_search_output(
        query=q,
        near=near_value,
        places=[],
        provider=attempted[-1] if attempted else requested,
        requested_provider=requested,
        providers_attempted=attempted,
    )
