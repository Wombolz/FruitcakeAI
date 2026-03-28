from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import APIRequestError
from app.api_service import fetch_daily_market_data_payload, fetch_intraday_market_data_payload
from app.config import settings
from app.db.models import Document
from app.rag.job_runner import enqueue_document_ingest


def _normalize_output_format(value: str | None) -> str:
    fmt = str(value or "table").strip().lower()
    if fmt not in {"csv", "json", "table"}:
        raise APIRequestError("output_format must be one of: csv, json, table")
    return fmt


def _provider_display_name(provider: str) -> str:
    if str(provider).strip().lower() == "alphavantage":
        return "Alpha Vantage"
    return str(provider).strip().title()


def _render_daily_market_data(
    *,
    symbol: str,
    provider: str,
    days: List[Dict[str, Any]],
    output_format: str,
) -> str:
    if output_format == "json":
        return json.dumps(
            {
                "provider": provider,
                "symbol": symbol,
                "days": days,
            },
            indent=2,
            ensure_ascii=True,
        )

    if output_format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for item in days:
            writer.writerow(
                [
                    item["date"],
                    f"{float(item['open']):.2f}",
                    f"{float(item['high']):.2f}",
                    f"{float(item['low']):.2f}",
                    f"{float(item['close']):.2f}",
                    int(item["volume"]),
                ]
            )
        return buffer.getvalue().strip()

    lines = [f"{_provider_display_name(provider)} daily market data for {symbol}:", ""]
    for idx, item in enumerate(days, start=1):
        lines.append(
            f"[{idx}] date={item['date']} | open={float(item['open']):.2f} | "
            f"high={float(item['high']):.2f} | low={float(item['low']):.2f} | "
            f"close={float(item['close']):.2f} | volume={int(item['volume'])}"
        )
    return "\n".join(lines)


def _render_intraday_market_data(
    *,
    symbol: str,
    provider: str,
    interval: str,
    bars: List[Dict[str, Any]],
    output_format: str,
) -> str:
    if output_format == "json":
        return json.dumps(
            {
                "provider": provider,
                "symbol": symbol,
                "interval": interval,
                "bars": bars,
            },
            indent=2,
            ensure_ascii=True,
        )

    if output_format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for item in bars:
            writer.writerow(
                [
                    item["timestamp"],
                    f"{float(item['open']):.2f}",
                    f"{float(item['high']):.2f}",
                    f"{float(item['low']):.2f}",
                    f"{float(item['close']):.2f}",
                    int(item["volume"]),
                ]
            )
        return buffer.getvalue().strip()

    lines = [f"{_provider_display_name(provider)} intraday market data for {symbol} ({interval}):", ""]
    for idx, item in enumerate(bars, start=1):
        lines.append(
            f"[{idx}] timestamp={item['timestamp']} | open={float(item['open']):.2f} | "
            f"high={float(item['high']):.2f} | low={float(item['low']):.2f} | "
            f"close={float(item['close']):.2f} | volume={int(item['volume'])}"
        )
    return "\n".join(lines)


def _generated_filename(*, symbol: str, provider: str, output_format: str, count: int) -> str:
    ext = {"csv": "csv", "json": "json", "table": "md"}[output_format]
    return f"{symbol.lower()}_{provider.lower()}_daily_{count}.{ext}"


def _generated_intraday_filename(
    *,
    symbol: str,
    provider: str,
    interval: str,
    output_format: str,
    count: int,
) -> str:
    ext = {"csv": "csv", "json": "json", "table": "md"}[output_format]
    clean_interval = interval.replace("min", "m")
    return f"{symbol.lower()}_{provider.lower()}_{clean_interval}_intraday_{count}.{ext}"


async def _save_generated_market_document(
    db: AsyncSession,
    *,
    user_id: int,
    original_filename: str,
    rendered_content: str,
    output_format: str,
    title: str,
) -> Document:
    user_storage = Path(settings.storage_dir) / str(user_id) / "generated"
    user_storage.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4()}_{original_filename}"
    file_path = user_storage / stored_name
    file_path.write_text(rendered_content, encoding="utf-8")

    mime_type = {
        "csv": "text/csv",
        "json": "application/json",
        "table": "text/markdown",
    }[output_format]

    doc = Document(
        owner_id=user_id,
        filename=stored_name,
        original_filename=original_filename,
        file_path=str(file_path),
        file_size_bytes=file_path.stat().st_size,
        mime_type=mime_type,
        scope="personal",
        processing_status="pending",
        title=title,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    await enqueue_document_ingest(db, document=doc)
    return doc


async def get_daily_market_data(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    days: int,
    provider: str = "alphavantage",
    save_to_library: bool = False,
    output_format: str = "table",
) -> Dict[str, Any]:
    normalized_provider = str(provider or "alphavantage").strip().lower()
    if normalized_provider != "alphavantage":
        raise APIRequestError("provider must be 'alphavantage' for now.")

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        raise APIRequestError("symbol is required.")

    day_limit = max(1, min(int(days or 30), 100))
    fmt = _normalize_output_format(output_format)

    payload = await fetch_daily_market_data_payload(
        db,
        user_id=user_id,
        provider=normalized_provider,
        symbol=clean_symbol,
        days=day_limit,
        secret_name="alphavantage_api_key",
    )
    rendered = _render_daily_market_data(
        symbol=payload["symbol"],
        provider=normalized_provider,
        days=list(payload["days"]),
        output_format=fmt,
    )

    saved_document = None
    if save_to_library:
        saved_document = await _save_generated_market_document(
            db,
            user_id=user_id,
            original_filename=_generated_filename(
                symbol=payload["symbol"],
                provider=normalized_provider,
                output_format=fmt,
                count=len(payload["days"]),
            ),
            rendered_content=rendered,
            output_format=fmt,
            title=f"{payload['symbol'].upper()} daily market data",
        )

    return {
        "symbol": payload["symbol"],
        "provider": normalized_provider,
        "days": list(payload["days"]),
        "output_format": fmt,
        "rendered": rendered,
        "saved_document_id": saved_document.id if saved_document is not None else None,
        "saved_document_name": saved_document.original_filename if saved_document is not None else None,
    }


async def get_intraday_market_data(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    interval: str,
    bars: int,
    provider: str = "alphavantage",
    save_to_library: bool = False,
    output_format: str = "table",
    extended_hours: bool = False,
) -> Dict[str, Any]:
    normalized_provider = str(provider or "alphavantage").strip().lower()
    if normalized_provider != "alphavantage":
        raise APIRequestError("provider must be 'alphavantage' for now.")

    clean_symbol = str(symbol or "").strip().upper()
    clean_interval = str(interval or "").strip().lower()
    if not clean_symbol:
        raise APIRequestError("symbol is required.")
    if not clean_interval:
        raise APIRequestError("interval is required.")

    bar_limit = max(1, min(int(bars or 30), 100))
    fmt = _normalize_output_format(output_format)

    payload = await fetch_intraday_market_data_payload(
        db,
        user_id=user_id,
        provider=normalized_provider,
        symbol=clean_symbol,
        interval=clean_interval,
        bars=bar_limit,
        extended_hours=bool(extended_hours),
        secret_name="alphavantage_api_key",
    )
    rendered = _render_intraday_market_data(
        symbol=payload["symbol"],
        provider=normalized_provider,
        interval=payload["interval"],
        bars=list(payload["bars"]),
        output_format=fmt,
    )

    saved_document = None
    if save_to_library:
        saved_document = await _save_generated_market_document(
            db,
            user_id=user_id,
            original_filename=_generated_intraday_filename(
                symbol=payload["symbol"],
                provider=normalized_provider,
                interval=payload["interval"],
                output_format=fmt,
                count=len(payload["bars"]),
            ),
            rendered_content=rendered,
            output_format=fmt,
            title=f"{payload['symbol'].upper()} intraday market data",
        )

    return {
        "symbol": payload["symbol"],
        "provider": normalized_provider,
        "interval": payload["interval"],
        "bars": list(payload["bars"]),
        "output_format": fmt,
        "rendered": rendered,
        "saved_document_id": saved_document.id if saved_document is not None else None,
        "saved_document_name": saved_document.original_filename if saved_document is not None else None,
    }
