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
from app.json_api import JsonApiError, extract_json_fields
from app.db.models import TaskAPIState
from app.secrets_service import SecretDecryptionError, audit_secret_access, resolve_secret


_SERVICE_PROVIDER_ALLOWLIST: dict[str, dict[str, Any]] = {
    "n2yo": {
        "provider": "n2yo",
        "allowed_secret_names": {"n2yo_api_key"},
    },
    "alphavantage": {
        "provider": "alphavantage",
        "allowed_secret_names": {"alphavantage_api_key"},
    },
    "weather": {
        "provider": "weather",
        "allowed_providers": {"weather", "openweathermap", "openweather"},
        "allowed_secret_names": {"openweathermap_api_key", "weather_api_key"},
        "provider_optional_for_allowed_names": True,
    },
}


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


async def _resolve_service_secret(
    db: AsyncSession,
    *,
    user_id: int,
    service_name: str,
    secret_name: str,
    tool_name: str,
    task_id: int | None = None,
) -> str:
    policy = _SERVICE_PROVIDER_ALLOWLIST.get(service_name)
    normalized_name = str(secret_name or "").strip().lower()
    if policy is None:
        raise APIRequestError(f"Unsupported service '{service_name}'.")
    allowed_names = set(policy.get("allowed_secret_names") or set())
    if normalized_name not in allowed_names:
        await audit_secret_access(
            db,
            user_id=user_id,
            secret_name=normalized_name or secret_name,
            tool_name=tool_name,
            task_id=task_id,
            success=False,
            error_class="SecretNameNotAllowed",
        )
        raise APIRequestError(
            f"Secret '{secret_name}' is not approved for service '{service_name}'."
        )

    resolved = await resolve_secret(
        db,
        user_id=user_id,
        name=normalized_name,
        mark_used=True,
        tool_name=tool_name,
        task_id=task_id,
        audit=True,
    )
    if resolved is None:
        raise APIRequestError(f"Secret '{secret_name}' was not found or is inactive.")

    provider = str(getattr(resolved.secret, "provider", "") or "").strip().lower()
    expected_provider = str(policy.get("provider") or "").strip().lower()
    allowed_providers = {
        str(item or "").strip().lower()
        for item in (policy.get("allowed_providers") or {expected_provider})
        if str(item or "").strip()
    }
    provider_optional = bool(policy.get("provider_optional_for_allowed_names"))
    if not provider_optional and provider not in allowed_providers:
        await audit_secret_access(
            db,
            user_id=user_id,
            secret_name=normalized_name,
            tool_name=tool_name,
            task_id=task_id,
            secret_id=int(resolved.secret.id) if resolved.secret.id is not None else None,
            success=False,
            error_class="SecretProviderMismatch",
        )
        raise APIRequestError(
            f"Secret '{secret_name}' is not a valid {service_name} credential."
        )
    return resolved.value


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

    api_key = await _resolve_service_secret(
        db,
        user_id=user_id,
        service_name=provider_name,
        secret_name=secret_name,
        tool_name="market_data_daily",
    )

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

    api_key = await _resolve_service_secret(
        db,
        user_id=user_id,
        service_name=provider_name,
        secret_name=secret_name,
        tool_name="market_data_intraday",
    )

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
    response_fields: Dict[str, str] | None = None,
    task_id: int | None = None,
) -> str:
    service_name = str(service or "").strip().lower()
    endpoint_name = str(endpoint or "").strip().lower()
    params = dict(query_params or {})

    try:
        api_key = None
        if secret_name:
            api_key = await _resolve_service_secret(
                db,
                user_id=user_id,
                service_name=service_name,
                secret_name=secret_name,
                tool_name=f"api_request:{service_name}:{endpoint_name}",
                task_id=task_id,
            )

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

        if response_fields:
            try:
                extracted = extract_json_fields(normalized, response_fields)
            except JsonApiError as exc:
                raise APIRequestError(str(exc)) from exc
            return json.dumps(
                {"deduped": deduped, "fields": extracted},
                ensure_ascii=True,
                sort_keys=True,
            )

        return formatter(normalized, deduped=deduped)
    except APIRequestError:
        raise
    except SecretDecryptionError as exc:
        raise APIRequestError(str(exc)) from exc
    except Exception as exc:
        message = str(exc).strip()
        if not message:
            message = exc.__class__.__name__
        raise APIRequestError(f"{exc.__class__.__name__}: {message}") from exc
