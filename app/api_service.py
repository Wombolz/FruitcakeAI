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


def _normalize_alphavantage_global_quote(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIRequestError("Alpha Vantage returned an invalid response shape.")
    if isinstance(raw.get("Note"), str) and raw.get("Note"):
        raise APIRequestError("Alpha Vantage rate limit reached. Try again later.")
    if isinstance(raw.get("Information"), str) and raw.get("Information"):
        raise APIRequestError(str(raw.get("Information")))
    if isinstance(raw.get("Error Message"), str) and raw.get("Error Message"):
        raise APIRequestError(str(raw.get("Error Message")))

    quote = raw.get("Global Quote")
    if not isinstance(quote, dict) or not quote:
        raise APIRequestError("Alpha Vantage response did not include a valid Global Quote payload.")

    symbol = str(quote.get("01. symbol") or "").strip()
    if not symbol:
        raise APIRequestError("Alpha Vantage response did not include a symbol.")

    def _float_value(key: str) -> float | None:
        value = str(quote.get(key) or "").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    return {
        "symbol": symbol,
        "open": _float_value("02. open"),
        "high": _float_value("03. high"),
        "low": _float_value("04. low"),
        "price": _float_value("05. price"),
        "volume": int(str(quote.get("06. volume") or "0").strip() or 0),
        "latest_trading_day": str(quote.get("07. latest trading day") or "").strip(),
        "previous_close": _float_value("08. previous close"),
        "change": _float_value("09. change"),
        "change_percent": str(quote.get("10. change percent") or "").strip(),
    }


def _format_alphavantage_global_quote(normalized: Dict[str, Any], *, deduped: bool) -> str:
    if deduped:
        return f"No new quote changes for {normalized.get('symbol') or 'the requested symbol'} since the last successful check."

    symbol = str(normalized.get("symbol") or "").strip() or "UNKNOWN"
    lines = [f"Alpha Vantage global quote for {symbol}:", ""]
    if normalized.get("price") is not None:
        lines.append(f"price={float(normalized['price']):.2f}")
    if normalized.get("change") is not None:
        change_percent = str(normalized.get("change_percent") or "").strip()
        suffix = f" ({change_percent})" if change_percent else ""
        lines.append(f"change={float(normalized['change']):.2f}{suffix}")
    if normalized.get("previous_close") is not None:
        lines.append(f"previous_close={float(normalized['previous_close']):.2f}")
    if normalized.get("open") is not None:
        lines.append(f"open={float(normalized['open']):.2f}")
    if normalized.get("high") is not None and normalized.get("low") is not None:
        lines.append(f"range={float(normalized['low']):.2f} - {float(normalized['high']):.2f}")
    if normalized.get("volume"):
        lines.append(f"volume={int(normalized['volume'])}")
    latest_day = str(normalized.get("latest_trading_day") or "").strip()
    if latest_day:
        lines.append(f"latest_trading_day={latest_day}")
    return "\n".join(lines)


def _normalize_alphavantage_time_series_daily(raw: Any, *, limit: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIRequestError("Alpha Vantage returned an invalid response shape.")
    if isinstance(raw.get("Note"), str) and raw.get("Note"):
        raise APIRequestError("Alpha Vantage rate limit reached. Try again later.")
    if isinstance(raw.get("Information"), str) and raw.get("Information"):
        raise APIRequestError(str(raw.get("Information")))
    if isinstance(raw.get("Error Message"), str) and raw.get("Error Message"):
        raise APIRequestError(str(raw.get("Error Message")))

    meta = raw.get("Meta Data")
    series = raw.get("Time Series (Daily)")
    if not isinstance(meta, dict) or not isinstance(series, dict) or not series:
        raise APIRequestError("Alpha Vantage response did not include a valid daily time series payload.")

    symbol = str(meta.get("2. Symbol") or "").strip()
    if not symbol:
        raise APIRequestError("Alpha Vantage daily series response did not include a symbol.")

    days: list[dict[str, Any]] = []
    for day in sorted(series.keys(), reverse=True):
        item = series.get(day)
        if not isinstance(item, dict):
            continue
        try:
            days.append(
                {
                    "date": day,
                    "open": float(str(item.get("1. open") or "").strip()),
                    "high": float(str(item.get("2. high") or "").strip()),
                    "low": float(str(item.get("3. low") or "").strip()),
                    "close": float(str(item.get("4. close") or "").strip()),
                    "volume": int(float(str(item.get("5. volume") or "0").strip() or 0)),
                }
            )
        except ValueError:
            continue
        if len(days) >= limit:
            break

    if not days:
        raise APIRequestError("Alpha Vantage daily series response did not include usable bar data.")

    return {"symbol": symbol, "days": days}


def _format_alphavantage_time_series_daily(normalized: Dict[str, Any], *, deduped: bool) -> str:
    symbol = str(normalized.get("symbol") or "").strip() or "UNKNOWN"
    if deduped:
        return f"No new daily bar changes for {symbol} since the last successful check."

    days = list(normalized.get("days") or [])
    if not days:
        return f"No daily bars found for {symbol}."

    lines = [f"Alpha Vantage daily time series for {symbol}:", ""]
    for idx, item in enumerate(days, start=1):
        lines.append(
            f"[{idx}] date={item['date']} | open={item['open']:.2f} | high={item['high']:.2f} | "
            f"low={item['low']:.2f} | close={item['close']:.2f} | volume={int(item['volume'])}"
        )
    return "\n".join(lines)


async def _fetch_alphavantage_daily_with_key(
    *,
    symbol: str,
    api_key: str,
    limit: int,
    outputsize: str = "compact",
) -> Dict[str, Any]:
    try:
        raw = await fetch_json(
            url="https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": outputsize,
                "apikey": api_key,
            },
        )
    except JsonApiError as exc:
        raise APIRequestError(str(exc)) from exc

    return _normalize_alphavantage_time_series_daily(raw, limit=limit)


async def fetch_daily_market_data_payload(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    symbol: str,
    days: int,
    secret_name: str,
) -> Dict[str, Any]:
    provider_name = str(provider or "").strip().lower()
    clean_symbol = str(symbol or "").strip().upper()
    if provider_name != "alphavantage":
        raise APIRequestError(f"Unsupported provider '{provider}'.")
    if not clean_symbol:
        raise APIRequestError("symbol is required.")

    api_key = await resolve_secret_value(db, user_id=user_id, name=str(secret_name).strip(), mark_used=True)
    if not api_key:
        raise APIRequestError(f"Secret '{secret_name}' was not found or is inactive.")

    limit = max(1, min(int(days or 30), 100))
    return await _fetch_alphavantage_daily_with_key(
        symbol=clean_symbol,
        api_key=api_key,
        limit=limit,
        outputsize="compact" if limit <= 100 else "full",
    )


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

    api_key = None
    if secret_name:
        api_key = await resolve_secret_value(db, user_id=user_id, name=str(secret_name).strip(), mark_used=True)
        if not api_key:
            raise APIRequestError(f"Secret '{secret_name}' was not found or is inactive.")

    del response_mode  # reserved for future adapter-specific formatting modes

    if service_name == "n2yo":
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

        normalized = _normalize_n2yo_visual_passes(raw)
        normalized = _filter_n2yo_visual_passes(
            normalized,
            min_max_elevation_deg=params.get("min_max_elevation_deg"),
        )
        formatter = _format_n2yo_visual_passes
    elif service_name == "alphavantage":
        symbol = str(params.get("symbol") or "").strip().upper()
        if not symbol:
            raise APIRequestError("Missing required query params: symbol")
        if not api_key:
            raise APIRequestError("Alpha Vantage requests require a named secret.")
        if endpoint_name == "global_quote":
            try:
                raw = await fetch_json(
                    url="https://www.alphavantage.co/query",
                    params={
                        "function": "GLOBAL_QUOTE",
                        "symbol": symbol,
                        "apikey": api_key,
                    },
                )
            except JsonApiError as exc:
                raise APIRequestError(str(exc)) from exc

            normalized = _normalize_alphavantage_global_quote(raw)
            formatter = _format_alphavantage_global_quote
        elif endpoint_name == "time_series_daily":
            outputsize = str(params.get("outputsize") or "compact").strip().lower()
            if outputsize not in {"compact", "full"}:
                raise APIRequestError("Alpha Vantage daily series outputsize must be 'compact' or 'full'.")
            limit = max(1, min(int(params.get("limit") or 30), 100))
            normalized = await _fetch_alphavantage_daily_with_key(
                symbol=symbol,
                api_key=api_key,
                limit=limit,
                outputsize=outputsize,
            )
            formatter = _format_alphavantage_time_series_daily
        else:
            raise APIRequestError(f"Unsupported endpoint '{endpoint}'.")
    else:
        raise APIRequestError(f"Unsupported service '{service}'.")

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

    return formatter(normalized, deduped=deduped)
