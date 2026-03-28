from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_adapters import get_api_adapter
from app.api_adapters.alphavantage import fetch_daily_payload, fetch_intraday_payload
from app.api_errors import APIRequestError
from app.db.models import TaskAPIState
from app.secrets_service import resolve_secret_value


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
    return await fetch_daily_payload(
        symbol=clean_symbol,
        api_key=api_key,
        limit=limit,
        outputsize="compact" if limit <= 100 else "full",
    )


async def fetch_intraday_market_data_payload(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    symbol: str,
    interval: str,
    bars: int,
    extended_hours: bool,
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

    limit = max(1, min(int(bars or 30), 100))
    return await fetch_intraday_payload(
        symbol=clean_symbol,
        api_key=api_key,
        interval=str(interval or "").strip().lower(),
        limit=limit,
        outputsize="compact" if limit <= 100 else "full",
        extended_hours=bool(extended_hours),
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

    adapter = get_api_adapter(service_name)
    if adapter is None:
        raise APIRequestError(f"Unsupported service '{service}'.")
    execution = await adapter.execute(
        endpoint=endpoint_name,
        query_params=params,
        api_key=api_key,
    )
    normalized = execution.normalized
    formatter = execution.formatter
    raw_payload = execution.raw_payload

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
            payload={"fingerprint": fp, "normalized": normalized, "raw_payload": raw_payload},
        )

    return formatter(normalized, deduped=deduped)
