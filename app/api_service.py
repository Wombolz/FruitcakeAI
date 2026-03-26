from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskAPIState
from app.json_api import JsonApiError, fetch_json
from app.secrets_service import resolve_secret_value


class APIRequestError(RuntimeError):
    pass


def _state_fingerprint(payload: Any) -> str:
    normalized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _load_task_api_state(db: AsyncSession, *, task_id: int, state_key: str) -> Optional[TaskAPIState]:
    result = await db.execute(
        select(TaskAPIState).where(TaskAPIState.task_id == task_id, TaskAPIState.state_key == state_key)
    )
    return result.scalar_one_or_none()


async def _store_task_api_state(
    db: AsyncSession,
    *,
    task_id: int,
    state_key: str,
    payload: Dict[str, Any],
) -> None:
    row = await _load_task_api_state(db, task_id=task_id, state_key=state_key)
    if row is None:
        row = TaskAPIState(task_id=task_id, state_key=state_key)
        db.add(row)
    row.value_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()


def _normalize_n2yo_visual_passes(raw: Any) -> Dict[str, Any]:
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


def _filter_n2yo_visual_passes(
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


def _format_n2yo_visual_passes(normalized: Dict[str, Any], *, deduped: bool) -> str:
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


async def execute_api_request(
    db: AsyncSession,
    *,
    user_id: int,
    service: str,
    endpoint: str,
    query_params: Dict[str, Any] | None = None,
    secret_name: str | None = None,
    response_mode: str | None = None,
    task_id: int | None = None,
) -> str:
    service_name = str(service or "").strip().lower()
    endpoint_name = str(endpoint or "").strip().lower()
    params = dict(query_params or {})

    if service_name != "n2yo":
        raise APIRequestError(f"Unsupported service '{service}'.")
    if endpoint_name != "iss_visual_passes":
        raise APIRequestError(f"Unsupported endpoint '{endpoint}'.")

    api_key = None
    if secret_name:
        api_key = await resolve_secret_value(db, user_id=user_id, name=str(secret_name).strip(), mark_used=True)
        if not api_key:
            raise APIRequestError(f"Secret '{secret_name}' was not found or is inactive.")

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

    del response_mode  # reserved for future adapter-specific formatting modes
    normalized = _normalize_n2yo_visual_passes(raw)
    normalized = _filter_n2yo_visual_passes(
        normalized,
        min_max_elevation_deg=params.get("min_max_elevation_deg"),
    )
    deduped = False
    if task_id is not None:
        state_key = f"{service_name}:{endpoint_name}"
        fp = _state_fingerprint(normalized)
        existing = await _load_task_api_state(db, task_id=task_id, state_key=state_key)
        if existing and existing.value_json:
            try:
                old = json.loads(existing.value_json)
            except Exception:
                old = {}
            if old.get("fingerprint") == fp:
                deduped = True
        await _store_task_api_state(
            db,
            task_id=task_id,
            state_key=state_key,
            payload={"fingerprint": fp, "normalized": normalized},
        )

    return _format_n2yo_visual_passes(normalized, deduped=deduped)
