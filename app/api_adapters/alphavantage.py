from __future__ import annotations

from typing import Any, Dict

from app.api_adapters.base import APIAdapter, AdapterExecutionResult
from app.api_errors import APIRequestError
from app.json_api import JsonApiError, fetch_json

_SUPPORTED_INTRADAY_INTERVALS = {"1min", "5min", "15min", "30min", "60min"}


def _raise_for_alpha_payload(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise APIRequestError("Alpha Vantage returned an invalid response shape.")
    if isinstance(raw.get("Note"), str) and raw.get("Note"):
        raise APIRequestError(str(raw.get("Note")))
    if isinstance(raw.get("Information"), str) and raw.get("Information"):
        raise APIRequestError(str(raw.get("Information")))
    if isinstance(raw.get("Error Message"), str) and raw.get("Error Message"):
        raise APIRequestError(str(raw.get("Error Message")))


def _normalize_global_quote(raw: Any) -> Dict[str, Any]:
    _raise_for_alpha_payload(raw)
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


def _format_global_quote(normalized: Dict[str, Any], deduped: bool) -> str:
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


def _normalize_time_series_daily(raw: Any, *, limit: int) -> Dict[str, Any]:
    _raise_for_alpha_payload(raw)
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


def _format_time_series_daily(normalized: Dict[str, Any], deduped: bool) -> str:
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


def _normalize_time_series_intraday(
    raw: Any,
    *,
    interval: str,
    limit: int,
    extended_hours: bool,
) -> Dict[str, Any]:
    _raise_for_alpha_payload(raw)
    meta = raw.get("Meta Data")
    series_key = f"Time Series ({interval})"
    series = raw.get(series_key)
    if not isinstance(meta, dict) or not isinstance(series, dict) or not series:
        raise APIRequestError("Alpha Vantage response did not include a valid intraday time series payload.")

    symbol = str(meta.get("2. Symbol") or "").strip()
    if not symbol:
        raise APIRequestError("Alpha Vantage intraday series response did not include a symbol.")

    bars: list[dict[str, Any]] = []
    for timestamp in sorted(series.keys(), reverse=True):
        item = series.get(timestamp)
        if not isinstance(item, dict):
            continue
        try:
            bars.append(
                {
                    "timestamp": timestamp,
                    "open": float(str(item.get("1. open") or "").strip()),
                    "high": float(str(item.get("2. high") or "").strip()),
                    "low": float(str(item.get("3. low") or "").strip()),
                    "close": float(str(item.get("4. close") or "").strip()),
                    "volume": int(float(str(item.get("5. volume") or "0").strip() or 0)),
                }
            )
        except ValueError:
            continue
        if len(bars) >= limit:
            break

    if not bars:
        raise APIRequestError("Alpha Vantage intraday series response did not include usable bar data.")

    return {
        "symbol": symbol,
        "interval": interval,
        "extended_hours": extended_hours,
        "bars": bars,
    }


def _format_time_series_intraday(normalized: Dict[str, Any], deduped: bool) -> str:
    symbol = str(normalized.get("symbol") or "").strip() or "UNKNOWN"
    interval = str(normalized.get("interval") or "").strip() or "UNKNOWN"
    if deduped:
        return f"No new intraday {interval} bar changes for {symbol} since the last successful check."

    bars = list(normalized.get("bars") or [])
    if not bars:
        return f"No intraday bars found for {symbol}."

    lines = [f"Alpha Vantage intraday time series for {symbol} ({interval}):", ""]
    for idx, item in enumerate(bars, start=1):
        lines.append(
            f"[{idx}] timestamp={item['timestamp']} | open={item['open']:.2f} | high={item['high']:.2f} | "
            f"low={item['low']:.2f} | close={item['close']:.2f} | volume={int(item['volume'])}"
        )
    return "\n".join(lines)


async def _fetch_time_series_daily(
    *,
    symbol: str,
    api_key: str,
    limit: int,
    outputsize: str = "compact",
) -> tuple[Dict[str, Any], Dict[str, Any]]:
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

    return _normalize_time_series_daily(raw, limit=limit), raw


async def _fetch_time_series_intraday(
    *,
    symbol: str,
    api_key: str,
    interval: str,
    limit: int,
    outputsize: str = "compact",
    extended_hours: bool = False,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        raw = await fetch_json(
            url="https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "extended_hours": "true" if extended_hours else "false",
                "apikey": api_key,
            },
        )
    except JsonApiError as exc:
        raise APIRequestError(str(exc)) from exc

    return (
        _normalize_time_series_intraday(
            raw,
            interval=interval,
            limit=limit,
            extended_hours=extended_hours,
        ),
        raw,
    )


class AlphaVantageAdapter(APIAdapter):
    service_name = "alphavantage"

    async def execute(
        self,
        *,
        endpoint: str,
        query_params: Dict[str, Any],
        api_key: str | None,
    ) -> AdapterExecutionResult:
        endpoint_name = str(endpoint or "").strip().lower()
        params = dict(query_params or {})
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
            normalized = _normalize_global_quote(raw)
            return AdapterExecutionResult(
                normalized=normalized,
                formatter=_format_global_quote,
                raw_payload=raw,
            )

        if endpoint_name == "time_series_daily":
            outputsize = str(params.get("outputsize") or "compact").strip().lower()
            if outputsize not in {"compact", "full"}:
                raise APIRequestError("Alpha Vantage daily series outputsize must be 'compact' or 'full'.")
            limit = max(1, min(int(params.get("limit") or 30), 100))
            normalized, raw = await _fetch_time_series_daily(
                symbol=symbol,
                api_key=api_key,
                limit=limit,
                outputsize=outputsize,
            )
            return AdapterExecutionResult(
                normalized=normalized,
                formatter=_format_time_series_daily,
                raw_payload=raw,
            )

        if endpoint_name == "time_series_intraday":
            interval = str(params.get("interval") or "").strip().lower()
            if interval not in _SUPPORTED_INTRADAY_INTERVALS:
                raise APIRequestError(
                    "Alpha Vantage intraday interval must be one of: 1min, 5min, 15min, 30min, 60min."
                )
            outputsize = str(params.get("outputsize") or "compact").strip().lower()
            if outputsize not in {"compact", "full"}:
                raise APIRequestError("Alpha Vantage intraday outputsize must be 'compact' or 'full'.")
            limit = max(1, min(int(params.get("limit") or 30), 100))
            extended_hours = bool(params.get("extended_hours", False))
            normalized, raw = await _fetch_time_series_intraday(
                symbol=symbol,
                api_key=api_key,
                interval=interval,
                limit=limit,
                outputsize=outputsize,
                extended_hours=extended_hours,
            )
            return AdapterExecutionResult(
                normalized=normalized,
                formatter=_format_time_series_intraday,
                raw_payload=raw,
            )

        raise APIRequestError(f"Unsupported endpoint '{endpoint}'.")


async def fetch_daily_payload(
    *,
    symbol: str,
    api_key: str,
    limit: int,
    outputsize: str = "compact",
) -> Dict[str, Any]:
    normalized, _raw = await _fetch_time_series_daily(
        symbol=symbol,
        api_key=api_key,
        limit=limit,
        outputsize=outputsize,
    )
    return normalized


async def fetch_intraday_payload(
    *,
    symbol: str,
    api_key: str,
    interval: str,
    limit: int,
    outputsize: str = "compact",
    extended_hours: bool = False,
) -> Dict[str, Any]:
    normalized, _raw = await _fetch_time_series_intraday(
        symbol=symbol,
        api_key=api_key,
        interval=interval,
        limit=limit,
        outputsize=outputsize,
        extended_hours=extended_hours,
    )
    return normalized
