from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.api_adapters.base import APIAdapter, AdapterExecutionResult
from app.api_errors import APIRequestError
from app.json_api import JsonApiError, fetch_json


def _normalize_visual_passes(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIRequestError("N2YO returned an invalid response shape.")
    passes = raw.get("passes")
    if not isinstance(passes, list):
        raise APIRequestError("N2YO response did not include a valid passes list.")

    normalized = []
    for item in passes:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "start_utc": int(item.get("startUTC") or 0),
                "duration_seconds": int(item.get("duration") or 0),
                "max_elevation_deg": float(item.get("maxEl") or 0.0),
                "start_az_deg": float(item.get("startAz") or 0.0),
                "end_az_deg": float(item.get("endAz") or 0.0),
            }
        )
    return {"passes": normalized}


def _filter_visual_passes(
    normalized: Dict[str, Any],
    *,
    min_max_elevation_deg: float | None = None,
) -> Dict[str, Any]:
    passes = list(normalized.get("passes") or [])
    if min_max_elevation_deg is None:
        return {"passes": passes}
    filtered = [
        item
        for item in passes
        if float(item.get("max_elevation_deg") or 0.0) >= float(min_max_elevation_deg)
    ]
    return {"passes": filtered}


def _format_visual_passes(normalized: Dict[str, Any], deduped: bool) -> str:
    passes = normalized.get("passes") or []
    if deduped:
        return "No new ISS pass changes since the last successful check."
    if not passes:
        return "No visible ISS passes found in the requested window."

    lines = ["ISS visible pass results:", ""]
    for idx, item in enumerate(passes, start=1):
        start_utc = int(item.get("start_utc") or 0)
        duration = int(item.get("duration_seconds") or 0)
        max_el = float(item.get("max_elevation_deg") or 0.0)
        when = datetime.fromtimestamp(start_utc, tz=timezone.utc).isoformat() if start_utc > 0 else "unknown"
        lines.append(
            f"[{idx}] start_utc={when} | duration_seconds={duration} | max_elevation_deg={max_el:.1f}"
        )
    return "\n".join(lines)


class N2YOAdapter(APIAdapter):
    service_name = "n2yo"

    async def execute(
        self,
        *,
        endpoint: str,
        query_params: Dict[str, Any],
        api_key: str | None,
    ) -> AdapterExecutionResult:
        endpoint_name = str(endpoint or "").strip().lower()
        params = dict(query_params or {})
        if endpoint_name != "iss_visual_passes":
            raise APIRequestError(f"Unsupported endpoint '{endpoint}'.")

        required = ("satellite_id", "lat", "lon", "alt_meters", "days", "min_visibility_seconds")
        missing = [name for name in required if params.get(name) in (None, "")]
        if missing:
            raise APIRequestError(f"Missing required query params: {', '.join(missing)}")
        if not api_key:
            raise APIRequestError("N2YO requests require a named secret.")

        path = (
            f"https://api.n2yo.com/rest/v1/satellite/visualpasses/"
            f"{int(params['satellite_id'])}/{params['lat']}/{params['lon']}/{params['alt_meters']}/"
            f"{int(params['days'])}/{int(params['min_visibility_seconds'])}/"
        )
        try:
            raw = await fetch_json(url=path, params={"apiKey": api_key})
        except JsonApiError as exc:
            raise APIRequestError(str(exc)) from exc

        normalized = _normalize_visual_passes(raw)
        normalized = _filter_visual_passes(
            normalized,
            min_max_elevation_deg=params.get("min_max_elevation_deg"),
        )
        return AdapterExecutionResult(
            normalized=normalized,
            formatter=_format_visual_passes,
            raw_payload=raw,
        )
