"""
FruitcakeAI v5 — Minimal MCP server surface for external agent testing.

This exposes a bounded authenticated HTTP MCP surface so external MCP clients
can inspect and exercise core Fruitcake behaviors directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.agent.context import UserContext
from app.auth.dependencies import get_current_user
from app.db.models import User

router = APIRouter()
_ARTIFACT_TEXT_LIMIT = 4000
_SUMMARY_PREVIEW_LIMIT = 280


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: Dict[str, Any] | None = None


def _jsonrpc_result(request_id: int | str | None, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(
    request_id: int | str | None,
    code: int,
    message: str,
    *,
    data: Any | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = data
    return payload


_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "fruitcake_list_tasks",
        "description": "List the current user's Fruitcake tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "fruitcake_get_task",
        "description": "Get one Fruitcake task by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "fruitcake_propose_task_draft",
        "description": "Build a normalized Fruitcake task draft without saving it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "instruction": {"type": "string"},
                "persona": {"type": "string"},
                "profile": {"type": "string"},
                "recipe_family": {"type": "string"},
                "recipe_params": {"type": "object"},
                "llm_model_override": {"type": "string"},
                "task_type": {"type": "string", "enum": ["one_shot", "recurring"], "default": "one_shot"},
                "schedule": {"type": "string"},
                "deliver": {"type": "boolean", "default": True},
                "requires_approval": {"type": "boolean", "default": True},
                "active_hours_start": {"type": "string"},
                "active_hours_end": {"type": "string"},
                "active_hours_tz": {"type": "string"},
            },
            "required": ["title", "instruction"],
        },
    },
    {
        "name": "fruitcake_create_task",
        "description": "Create a Fruitcake task immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "instruction": {"type": "string"},
                "persona": {"type": "string"},
                "profile": {"type": "string"},
                "recipe_family": {"type": "string"},
                "recipe_params": {"type": "object"},
                "llm_model_override": {"type": "string"},
                "task_type": {"type": "string", "enum": ["one_shot", "recurring"], "default": "one_shot"},
                "schedule": {"type": "string"},
                "deliver": {"type": "boolean", "default": True},
                "requires_approval": {"type": "boolean", "default": True},
                "active_hours_start": {"type": "string"},
                "active_hours_end": {"type": "string"},
                "active_hours_tz": {"type": "string"},
            },
            "required": ["title", "instruction"],
        },
    },
    {
        "name": "fruitcake_update_task",
        "description": "Update an existing Fruitcake task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "title": {"type": "string"},
                "instruction": {"type": "string"},
                "persona": {"type": "string"},
                "profile": {"type": "string"},
                "recipe_family": {"type": "string"},
                "recipe_params": {"type": "object"},
                "llm_model_override": {"type": "string"},
                "task_type": {"type": "string", "enum": ["one_shot", "recurring"]},
                "schedule": {"type": "string"},
                "deliver": {"type": "boolean"},
                "requires_approval": {"type": "boolean"},
                "active_hours_start": {"type": "string"},
                "active_hours_end": {"type": "string"},
                "active_hours_tz": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "fruitcake_list_library_documents",
        "description": "List documents accessible in the current user's Fruitcake library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 25},
                "scope_filter": {"type": "string", "enum": ["personal", "family", "shared"]},
            },
        },
    },
    {
        "name": "fruitcake_search_library",
        "description": "Search the current user's Fruitcake document library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fruitcake_summarize_document",
        "description": "Summarize a document from the current user's Fruitcake library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_name": {"type": "string"},
            },
            "required": ["document_name"],
        },
    },
    {
        "name": "fruitcake_get_scheduler_health",
        "description": "Return Fruitcake task scheduler dispatch health.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "fruitcake_list_task_runs",
        "description": "List recent runs for the current user's Fruitcake tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "task_id": {"type": "integer"},
                "status": {"type": "string"},
                "profile": {"type": "string"},
                "has_memory_candidates": {"type": "boolean"},
            },
        },
    },
    {
        "name": "fruitcake_get_task_run_artifacts",
        "description": "Return artifacts for a task run owned by the current user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "run_id": {"type": "integer"},
                "detail": {"type": "string", "enum": ["summary", "raw"], "default": "summary"},
            },
            "required": ["task_id", "run_id"],
        },
    },
    {
        "name": "fruitcake_get_memory_candidates",
        "description": "Return decoded memory candidates for a user-owned task run when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "run_id": {"type": "integer"},
            },
            "required": ["task_id", "run_id"],
        },
    },
    {
        "name": "fruitcake_inspect_task_run",
        "description": "Inspect one recent task run with summarized artifacts, diagnostics, and memory candidates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "run_id": {"type": "integer"},
                "mode": {
                    "type": "string",
                    "enum": ["latest", "latest_with_artifacts", "latest_with_memory_candidates"],
                    "default": "latest",
                },
                "include_raw_artifacts": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "fruitcake_get_task_health_rollup",
        "description": "Summarize task run health over a recent time window for one user-owned task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "window_hours": {"type": "integer", "default": 24},
                "window": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
]


def _decode_json_like(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list, int, float, bool)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _truncate_text(value: str | None, *, limit: int = _ARTIFACT_TEXT_LIMIT) -> tuple[str | None, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return (text or None), False
    return text[:limit], True


def _bool_arg(arguments: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _window_hours_arg(arguments: Dict[str, Any], *, default: int = 24) -> int:
    raw = arguments.get("window_hours")
    if raw is None:
        raw = arguments.get("window")
    if raw is None:
        return default

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        hours = int(raw)
        return max(1, min(24 * 14, hours))

    value = str(raw).strip().lower()
    aliases = {
        "24h": 24,
        "48h": 48,
        "72h": 72,
        "7d": 24 * 7,
        "14d": 24 * 14,
        "rollup_24h": 24,
        "rollup_48h": 48,
        "rollup_72h": 72,
        "rollup_7d": 24 * 7,
        "rollup_14d": 24 * 14,
    }
    if value in aliases:
        return aliases[value]
    try:
        if value.endswith("h"):
            return max(1, min(24 * 14, int(value[:-1].strip())))
        if value.endswith("d"):
            return max(1, min(24 * 14, int(value[:-1].strip()) * 24))
        return max(1, min(24 * 14, int(value)))
    except Exception:
        return default


def _preview(value: str | None, *, limit: int = _SUMMARY_PREVIEW_LIMIT) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _serialize_task(task: Any) -> Dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "instruction": task.instruction,
        "persona": task.persona,
        "profile": task.profile,
        "task_type": task.task_type,
        "status": task.status,
        "schedule": task.schedule,
        "deliver": task.deliver,
        "requires_approval": task.requires_approval,
        "active_hours_start": task.active_hours_start,
        "active_hours_end": task.active_hours_end,
        "active_hours_tz": task.active_hours_tz,
        "created_at": _iso(task.created_at),
        "last_run_at": _iso(task.last_run_at),
        "next_run_at": _iso(task.next_run_at),
    }


def _serialize_run(run: Any, task: Any) -> Dict[str, Any]:
    duration_seconds = None
    if run.started_at is not None and run.finished_at is not None:
        duration_seconds = round((run.finished_at - run.started_at).total_seconds(), 3)
    return {
        "run_id": run.id,
        "task_id": task.id,
        "task_title": task.title,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_seconds": duration_seconds,
        "summary": run.summary,
        "summary_preview": _preview(run.summary),
        "error": run.error,
        "session_id": run.session_id,
    }


def _parse_memory_candidates(payload: Any) -> list[dict[str, Any]]:
    decoded = _decode_json_like(payload)
    if not isinstance(decoded, dict):
        return []
    candidates = decoded.get("candidates") or []
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _summarize_prepared_dataset(payload: Any) -> Dict[str, Any]:
    decoded = _decode_json_like(payload)
    if not isinstance(decoded, dict):
        return {"kind": "prepared_dataset", "summary": None}
    rss_items = decoded.get("rss_items") or []
    source_inventory = decoded.get("source_inventory") or {}
    active_sources = source_inventory.get("active_sources") if isinstance(source_inventory, dict) else []
    top_titles = []
    if isinstance(rss_items, list):
        for item in rss_items[:5]:
            if isinstance(item, dict) and item.get("title"):
                top_titles.append(str(item["title"]))
    return {
        "kind": "prepared_dataset",
        "rss_item_count": len(rss_items) if isinstance(rss_items, list) else 0,
        "active_source_count": len(active_sources) if isinstance(active_sources, list) else 0,
        "topic": decoded.get("topic"),
        "notes": decoded.get("notes"),
        "top_titles": top_titles,
    }


def _summarize_validation_payload(payload: Any) -> Dict[str, Any]:
    decoded = _decode_json_like(payload)
    if not isinstance(decoded, dict):
        return {"kind": "validation_report"}
    return {
        "kind": "validation_report",
        "fatal": bool(decoded.get("fatal")),
        "declared_tool": decoded.get("declared_tool"),
        "declared_tool_called": decoded.get("declared_tool_called"),
        "exact_output_match": decoded.get("exact_output_match"),
        "output_replaced_with_tool_result": decoded.get("output_replaced_with_tool_result"),
        "memory_candidate_emitted": decoded.get("memory_candidate_emitted"),
        "memory_candidate_type": decoded.get("memory_candidate_type"),
        "unexpected_tool_calls": decoded.get("unexpected_tool_calls") or [],
    }


def _summarize_run_diagnostics_payload(payload: Any) -> Dict[str, Any]:
    decoded = _decode_json_like(payload)
    if not isinstance(decoded, dict):
        return {"kind": "run_diagnostics"}
    dataset_stats = decoded.get("dataset_stats") or {}
    refresh_stats = decoded.get("refresh_stats") or {}
    return {
        "kind": "run_diagnostics",
        "active_skills": decoded.get("active_skills") or [],
        "skill_selection_mode": decoded.get("skill_selection_mode"),
        "dataset_stats": dataset_stats if isinstance(dataset_stats, dict) else {},
        "refresh_stats": refresh_stats if isinstance(refresh_stats, dict) else {},
        "suppression_events": decoded.get("suppression_events") or [],
        "unexpected_tool_calls": decoded.get("unexpected_tool_calls") or [],
    }


def _summarize_artifact(artifact: Any, *, detail: str = "summary", include_raw: bool = False) -> Dict[str, Any]:
    content_json = _decode_json_like(artifact.content_json)
    text_value, text_truncated = _truncate_text(artifact.content_text)
    base = {
        "id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "created_at": _iso(artifact.created_at),
    }
    if detail == "raw":
        base["content_json"] = content_json
        base["content_text"] = artifact.content_text
        return base

    if artifact.artifact_type == "final_output":
        base["summary"] = {
            "kind": "final_output",
            "text": text_value,
            "text_truncated": text_truncated,
        }
    elif artifact.artifact_type == "validation_report":
        base["summary"] = _summarize_validation_payload(content_json)
    elif artifact.artifact_type == "run_diagnostics":
        base["summary"] = _summarize_run_diagnostics_payload(content_json)
    elif artifact.artifact_type == "prepared_dataset":
        base["summary"] = _summarize_prepared_dataset(content_json)
    elif artifact.artifact_type == "memory_candidates":
        candidates = _parse_memory_candidates(content_json)
        base["summary"] = {
            "kind": "memory_candidates",
            "count": len(candidates),
            "candidates": candidates,
        }
    else:
        json_preview = content_json if include_raw else _preview(json.dumps(content_json, ensure_ascii=False)) if content_json is not None else None
        base["summary"] = {
            "kind": artifact.artifact_type,
            "content_text_preview": _preview(text_value),
            "content_json_preview": json_preview,
        }
    if include_raw:
        base["raw"] = {
            "content_json": content_json,
            "content_text": artifact.content_text,
        }
    return base


def _artifact_map(artifacts: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        artifact["artifact_type"]: artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("artifact_type")
    }


def _normalize_overlap_text(value: str | None) -> str:
    import re

    text = re.sub(r"[^a-z0-9\s]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _token_overlap_ratio(a: str | None, b: str | None) -> float:
    a_tokens = {token for token in _normalize_overlap_text(a).split() if len(token) > 2}
    b_tokens = {token for token in _normalize_overlap_text(b).split() if len(token) > 2}
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / float(len(a_tokens | b_tokens))


def _extract_memory_theme_clause(content: str | None) -> str:
    import re

    text = re.sub(r"\s+", " ", str(content or "").strip())
    if not text:
        return ""
    match = re.search(
        r"\bindicated\s+(.*?)(?:,\s*based on coverage from\b|[.]?$)",
        text,
        flags=re.IGNORECASE,
    )
    clause = match.group(1).strip() if match else text
    return _normalize_overlap_text(clause)


def _memory_candidate_source_findings(
    *,
    artifacts: list[Dict[str, Any]],
    memory_candidates: list[dict[str, Any]],
) -> list[str]:
    findings: list[str] = []
    if not memory_candidates:
        return findings

    artifact_by_type = _artifact_map(artifacts)
    prepared_dataset = (artifact_by_type.get("prepared_dataset") or {}).get("summary") or {}
    final_output = (artifact_by_type.get("final_output") or {}).get("summary") or {}

    source_text_parts: list[str] = []
    if isinstance(prepared_dataset, dict):
        source_text_parts.extend(str(title) for title in (prepared_dataset.get("top_titles") or []) if str(title).strip())
        if prepared_dataset.get("notes"):
            source_text_parts.append(str(prepared_dataset.get("notes")))
        if prepared_dataset.get("topic"):
            source_text_parts.append(str(prepared_dataset.get("topic")))
    if isinstance(final_output, dict) and final_output.get("text"):
        source_text_parts.append(str(final_output.get("text")))
    source_text = " ".join(source_text_parts).strip()
    if not source_text:
        return findings

    for candidate in memory_candidates:
        theme = _extract_memory_theme_clause(candidate.get("content"))
        if not theme:
            continue
        overlap = _token_overlap_ratio(theme, source_text)
        if overlap < 0.08:
            findings.append("memory_candidate_low_source_overlap")
            break
    return findings


def _inspection_findings(
    *,
    task: Any,
    run: Any,
    artifacts: list[Dict[str, Any]],
    memory_candidates: list[dict[str, Any]],
) -> list[str]:
    findings: list[str] = []
    artifact_types = {artifact.get("artifact_type") for artifact in artifacts}
    artifact_by_type = _artifact_map(artifacts)
    final_output = artifact_by_type.get("final_output", {}).get("summary", {})
    validation = artifact_by_type.get("validation_report", {}).get("summary", {})

    if run.status == "completed" and not artifacts:
        findings.append("completed_run_has_no_artifacts")
    if run.status == "failed" and not artifacts:
        findings.append("failed_run_has_no_artifacts")
    if run.status == "cancelled" and not artifacts:
        findings.append("cancelled_run_has_no_artifacts")
    if run.status == "completed" and isinstance(final_output, dict) and not str(final_output.get("text") or "").strip():
        findings.append("completed_run_has_empty_final_output")
    if isinstance(validation, dict) and bool(validation.get("fatal")):
        findings.append("validation_report_marked_fatal")
    if memory_candidates and "memory_candidates" not in artifact_types:
        findings.append("memory_candidates_missing_artifact_record")

    final_text = str((final_output or {}).get("text") or "").lower()
    for candidate in memory_candidates:
        topic = str(candidate.get("topic") or "").strip().lower()
        content = str(candidate.get("content") or "").strip().lower()
        if final_text and topic and topic not in final_text and content and content not in final_text:
            findings.append("memory_candidate_not_reflected_in_final_output")
            break
    findings.extend(_memory_candidate_source_findings(artifacts=artifacts, memory_candidates=memory_candidates))
    return findings


async def _get_owned_task(task_id: int, user_id: int, db: Any) -> Any | None:
    from sqlalchemy import select

    from app.db.models import Task

    result = await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    return result.scalar_one_or_none()


async def _get_owned_task_and_run(task_id: int, run_id: int, user_id: int, db: Any) -> tuple[Any, Any] | None:
    from sqlalchemy import select

    from app.db.models import Task, TaskRun

    result = await db.execute(
        select(Task, TaskRun)
        .join(TaskRun, TaskRun.task_id == Task.id)
        .where(Task.id == task_id, TaskRun.id == run_id, Task.user_id == user_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def _select_task_run_for_inspection(task: Any, arguments: Dict[str, Any], db: Any) -> tuple[Any | None, str]:
    from sqlalchemy import desc, exists, select

    from app.db.models import TaskRun, TaskRunArtifact

    run_id = arguments.get("run_id")
    if run_id is not None:
        try:
            normalized_run_id = int(run_id)
        except Exception:
            return None, "run_id must be an integer."
        result = await db.execute(
            select(TaskRun)
            .where(TaskRun.id == normalized_run_id, TaskRun.task_id == task.id)
            .limit(1)
        )
        return result.scalar_one_or_none(), ""

    mode = str(arguments.get("mode") or "latest").strip() or "latest"
    if mode not in {"latest", "latest_with_artifacts", "latest_with_memory_candidates"}:
        return None, "mode must be one of: latest, latest_with_artifacts, latest_with_memory_candidates."

    query = select(TaskRun).where(TaskRun.task_id == task.id)
    if mode == "latest_with_artifacts":
        query = query.where(
            exists(
                select(TaskRunArtifact.id).where(TaskRunArtifact.task_run_id == TaskRun.id)
            )
        )
    elif mode == "latest_with_memory_candidates":
        query = query.where(
            exists(
                select(TaskRunArtifact.id).where(
                    TaskRunArtifact.task_run_id == TaskRun.id,
                    TaskRunArtifact.artifact_type == "memory_candidates",
                )
            )
        )
    query = query.order_by(desc(TaskRun.started_at), desc(TaskRun.id)).limit(1)
    result = await db.execute(query)
    return result.scalar_one_or_none(), ""


async def _load_run_artifacts(run_id: int, db: Any) -> list[Any]:
    from sqlalchemy import select

    from app.db.models import TaskRunArtifact

    result = await db.execute(
        select(TaskRunArtifact)
        .where(TaskRunArtifact.task_run_id == run_id)
        .order_by(TaskRunArtifact.created_at.desc(), TaskRunArtifact.id.desc())
    )
    return list(result.scalars().all())


def _collect_memory_candidates_from_artifacts(artifacts: list[Any]) -> list[dict[str, Any]]:
    for artifact in artifacts:
        if getattr(artifact, "artifact_type", None) == "memory_candidates":
            return _parse_memory_candidates(getattr(artifact, "content_json", None))
    return []


def _diagnostics_summary(artifacts: list[Dict[str, Any]]) -> Dict[str, Any]:
    artifact_by_type = _artifact_map(artifacts)
    validation = artifact_by_type.get("validation_report", {}).get("summary", {})
    run_diagnostics = artifact_by_type.get("run_diagnostics", {}).get("summary", {})
    dataset_stats = run_diagnostics.get("dataset_stats") if isinstance(run_diagnostics, dict) else {}
    refresh_stats = run_diagnostics.get("refresh_stats") if isinstance(run_diagnostics, dict) else {}
    return {
        "validation_report": validation if isinstance(validation, dict) else {},
        "run_diagnostics": run_diagnostics if isinstance(run_diagnostics, dict) else {},
        "grounding_fatal": bool((validation or {}).get("fatal")) if isinstance(validation, dict) else False,
        "active_skills": list((run_diagnostics or {}).get("active_skills") or []) if isinstance(run_diagnostics, dict) else [],
        "dataset_stats": dataset_stats if isinstance(dataset_stats, dict) else {},
        "refresh_stats": refresh_stats if isinstance(refresh_stats, dict) else {},
    }


def _inspection_payload(
    *,
    task: Any,
    run: Any,
    raw_artifacts: list[Any],
    include_raw_artifacts: bool,
) -> Dict[str, Any]:
    artifacts = [
        _summarize_artifact(artifact, detail="summary", include_raw=include_raw_artifacts)
        for artifact in raw_artifacts
    ]
    memory_candidates = _collect_memory_candidates_from_artifacts(raw_artifacts)
    diagnostics = _diagnostics_summary(artifacts)
    findings = _inspection_findings(task=task, run=run, artifacts=artifacts, memory_candidates=memory_candidates)
    return {
        "found": True,
        "task": _serialize_task(task),
        "run": _serialize_run(run, task),
        "artifacts": {
            "count": len(artifacts),
            "types": [artifact["artifact_type"] for artifact in artifacts],
            "items": artifacts,
        },
        "memory_candidates": {
            "count": len(memory_candidates),
            "candidates": memory_candidates,
        },
        "diagnostics": diagnostics,
        "quality_signals": {
            "has_artifacts": bool(artifacts),
            "has_memory_candidates": bool(memory_candidates),
            "has_validation_warnings": bool(findings),
            "grounding_fatal": bool(diagnostics.get("grounding_fatal")),
            "candidate_count": len(memory_candidates),
        },
        "inspection_findings": findings,
    }


async def _tool_list_tasks(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _list_tasks

    return await _list_tasks(arguments, UserContext.from_user(user))


async def _tool_get_task(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _get_task

    return await _get_task(arguments, UserContext.from_user(user))


async def _tool_propose_task_draft(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _propose_task_draft

    return await _propose_task_draft(arguments, UserContext.from_user(user))


async def _tool_create_task(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _create_task

    return await _create_task(arguments, UserContext.from_user(user))


async def _tool_update_task(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _update_task

    return await _update_task(arguments, UserContext.from_user(user))


async def _tool_list_library_documents(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _list_library_documents

    return await _list_library_documents(arguments, UserContext.from_user(user))


async def _tool_search_library(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _search_library

    return await _search_library(arguments, UserContext.from_user(user))


async def _tool_summarize_document(arguments: Dict[str, Any], user: User) -> str:
    from app.agent.tools import _summarize_document

    return await _summarize_document(arguments, UserContext.from_user(user))


async def _tool_get_scheduler_health(arguments: Dict[str, Any], user: User) -> Dict[str, Any]:
    _ = arguments
    from sqlalchemy import asc, or_, select, func

    from app.autonomy.scheduler import get_llm_dispatch_health
    from app.config import settings
    from app.db.models import Task
    from app.db.session import AsyncSessionLocal

    state = get_llm_dispatch_health()
    stale_running_count = 0
    next_due_task_id = None
    next_due_task_title = None
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, int(settings.scheduler_stale_running_recovery_minutes or 20)))

    async with AsyncSessionLocal() as db:
        next_due = await db.execute(
            select(Task)
            .where(Task.user_id == user.id, Task.status == "pending", Task.next_run_at.is_not(None))
            .order_by(asc(Task.next_run_at), asc(Task.id))
            .limit(1)
        )
        next_task = next_due.scalar_one_or_none()
        if next_task is not None:
            next_due_task_id = next_task.id
            next_due_task_title = next_task.title

        stale_result = await db.execute(
            select(func.count(Task.id)).where(
                Task.user_id == user.id,
                Task.status == "running",
                or_(Task.last_run_at.is_(None), Task.last_run_at < cutoff),
            )
        )
        stale_running_count = int(stale_result.scalar() or 0)

    blocked = bool(state.get("blocked"))
    summary = "Scheduler dispatch is blocked by the LLM health gate."
    if not blocked:
        summary = "Scheduler dispatch is ready."
    if next_due_task_title:
        summary += f" Next task: {next_due_task_title}."
    elif next_due_task_id is None:
        summary += " No scheduled pending task is queued for this user."

    return {
        "status": "blocked" if blocked else "ready",
        "blocked": blocked,
        "unhealthy_until": state.get("unhealthy_until"),
        "last_error": state.get("last_error"),
        "last_dispatch_at": state.get("last_dispatch_at"),
        "next_due_task_id": next_due_task_id,
        "next_due_task_title": next_due_task_title,
        "stale_running_count": stale_running_count,
        "summary": summary,
    }


async def _tool_list_task_runs(arguments: Dict[str, Any], user: User) -> Dict[str, Any]:
    from sqlalchemy import desc, exists, select

    from app.db.models import Task, TaskRun, TaskRunArtifact
    from app.db.session import AsyncSessionLocal

    try:
        limit = max(1, min(50, int(arguments.get("limit", 10))))
    except Exception:
        limit = 10
    task_id = arguments.get("task_id")
    status = str(arguments.get("status") or "").strip()
    profile = str(arguments.get("profile") or "").strip()
    has_memory_candidates = arguments.get("has_memory_candidates")

    async with AsyncSessionLocal() as db:
        query = (
            select(TaskRun, Task)
            .join(Task, TaskRun.task_id == Task.id)
            .where(Task.user_id == user.id)
        )
        if task_id is not None:
            try:
                normalized_task_id = int(task_id)
            except Exception:
                return "task_id must be an integer."
            query = query.where(Task.id == normalized_task_id)
        if status:
            query = query.where(TaskRun.status == status)
        if profile:
            query = query.where(Task.profile == profile)
        if has_memory_candidates is not None:
            memory_exists = exists(
                select(TaskRunArtifact.id).where(
                    TaskRunArtifact.task_run_id == TaskRun.id,
                    TaskRunArtifact.artifact_type == "memory_candidates",
                )
            )
            query = query.where(memory_exists if _bool_arg(arguments, "has_memory_candidates") else ~memory_exists)
        query = query.order_by(desc(TaskRun.started_at), desc(TaskRun.id)).limit(limit)
        rows = (await db.execute(query)).all()

    runs = [
        _serialize_run(run, task)
        for run, task in rows
    ]
    return {"count": len(runs), "runs": runs}


async def _tool_get_task_run_artifacts(arguments: Dict[str, Any], user: User) -> Dict[str, Any] | str:

    from app.db.session import AsyncSessionLocal

    try:
        task_id = int(arguments.get("task_id"))
        run_id = int(arguments.get("run_id"))
    except Exception:
        return "task_id and run_id are required integers."

    async with AsyncSessionLocal() as db:
        owned = await _get_owned_task_and_run(task_id, run_id, user.id, db)
        if owned is None:
            return "Task run not found."
        _, run = owned
        detail = str(arguments.get("detail") or "summary").strip().lower()
        if detail not in {"summary", "raw"}:
            return "detail must be 'summary' or 'raw'."
        artifact_rows = await _load_run_artifacts(run.id, db)

    artifacts = [_summarize_artifact(artifact, detail=detail) for artifact in artifact_rows]
    return {"count": len(artifacts), "detail": detail, "artifacts": artifacts}


async def _tool_get_memory_candidates(arguments: Dict[str, Any], user: User) -> Dict[str, Any] | str:
    from app.db.session import AsyncSessionLocal
    from app.memory.review_service import decode_proposal_payload, latest_memory_candidates_artifact

    try:
        task_id = int(arguments.get("task_id"))
        run_id = int(arguments.get("run_id"))
    except Exception:
        return "task_id and run_id are required integers."

    async with AsyncSessionLocal() as db:
        owned = await _get_owned_task_and_run(task_id, run_id, user.id, db)
        if owned is None:
            return "Task run not found."

        artifact = await latest_memory_candidates_artifact(db, task_run_id=run_id)
        if artifact is None:
            return {"count": 0, "candidates": []}

        payload = decode_proposal_payload(artifact.content_json)
        candidates = payload.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []

    return {"count": len(candidates), "candidates": candidates}


async def _tool_inspect_task_run(arguments: Dict[str, Any], user: User) -> Dict[str, Any] | str:
    from app.db.session import AsyncSessionLocal

    try:
        task_id = int(arguments.get("task_id"))
    except Exception:
        return "task_id is required and must be an integer."

    include_raw_artifacts = _bool_arg(arguments, "include_raw_artifacts", False)

    async with AsyncSessionLocal() as db:
        task = await _get_owned_task(task_id, user.id, db)
        if task is None:
            return {"found": False, "message": "Task not found."}

        run, error = await _select_task_run_for_inspection(task, arguments, db)
        if error:
            return error
        if run is None:
            return {
                "found": False,
                "message": "No matching task run found.",
                "task": _serialize_task(task),
            }

        artifact_rows = await _load_run_artifacts(run.id, db)

    return _inspection_payload(
        task=task,
        run=run,
        raw_artifacts=artifact_rows,
        include_raw_artifacts=include_raw_artifacts,
    )


async def _tool_get_task_health_rollup(arguments: Dict[str, Any], user: User) -> Dict[str, Any] | str:
    from collections import Counter
    from sqlalchemy import desc, select

    from app.db.models import Task, TaskRun, TaskRunArtifact
    from app.db.session import AsyncSessionLocal

    try:
        task_id = int(arguments.get("task_id"))
    except Exception:
        return "task_id is required and must be an integer."
    window_hours = _window_hours_arg(arguments, default=24)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    async with AsyncSessionLocal() as db:
        task = await _get_owned_task(task_id, user.id, db)
        if task is None:
            return {"found": False, "message": "Task not found."}

        run_rows = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.task_id == task.id, TaskRun.started_at >= window_start)
                .order_by(desc(TaskRun.started_at), desc(TaskRun.id))
            )
        ).scalars().all()

        run_ids = [run.id for run in run_rows]
        artifact_run_ids: set[int] = set()
        memory_candidate_run_ids: set[int] = set()
        artifact_rows_by_run: dict[int, list[Any]] = {}
        if run_ids:
            artifact_rows = (
                await db.execute(
                    select(TaskRunArtifact)
                    .where(TaskRunArtifact.task_run_id.in_(run_ids))
                    .order_by(desc(TaskRunArtifact.created_at), desc(TaskRunArtifact.id))
                )
            ).scalars().all()
            for artifact in artifact_rows:
                task_run_id = int(artifact.task_run_id)
                artifact_rows_by_run.setdefault(task_run_id, []).append(artifact)
                artifact_run_ids.add(task_run_id)
                if artifact.artifact_type == "memory_candidates":
                    memory_candidate_run_ids.add(task_run_id)

    status_counts = Counter(str(run.status or "") for run in run_rows)
    total_runs = len(run_rows)
    completed_count = int(status_counts.get("completed", 0))
    failed_count = int(status_counts.get("failed", 0))
    cancelled_count = int(status_counts.get("cancelled", 0))
    waiting_approval_count = int(status_counts.get("waiting_approval", 0))
    running_count = int(status_counts.get("running", 0))
    no_artifact_failures = sum(1 for run in run_rows if run.status in {"failed", "cancelled"} and int(run.id) not in artifact_run_ids)

    durations = [
        (run.finished_at - run.started_at).total_seconds()
        for run in run_rows
        if run.started_at is not None and run.finished_at is not None
    ]
    repeated_errors = Counter(str(run.error or "").strip() for run in run_rows if str(run.error or "").strip())
    repeated_error_messages = [
        {"error": error, "count": count}
        for error, count in repeated_errors.most_common(3)
    ]

    success_rate = round((completed_count / total_runs), 3) if total_runs else None
    cancellation_rate = round((cancelled_count / total_runs), 3) if total_runs else None
    average_duration_seconds = round(sum(durations) / len(durations), 3) if durations else None

    summary_bits = [f"{total_runs} run(s) in the last {window_hours}h"]
    if total_runs:
        summary_bits.append(f"{completed_count} completed")
        if failed_count:
            summary_bits.append(f"{failed_count} failed")
        if cancelled_count:
            summary_bits.append(f"{cancelled_count} cancelled")

    findings: list[str] = []
    per_run_findings: Counter[str] = Counter()
    for run in run_rows:
        raw_artifacts = artifact_rows_by_run.get(int(run.id), [])
        artifacts = [
            _summarize_artifact(artifact, detail="summary", include_raw=False)
            for artifact in raw_artifacts
        ]
        memory_candidates = _collect_memory_candidates_from_artifacts(raw_artifacts)
        for finding in _inspection_findings(
            task=task,
            run=run,
            artifacts=artifacts,
            memory_candidates=memory_candidates,
        ):
            per_run_findings[finding] += 1

    if failed_count and repeated_error_messages and repeated_error_messages[0]["count"] >= 2:
        findings.append("repeated_error_pattern_detected")
    if cancelled_count >= 2:
        findings.append("cancellation_churn_detected")
    if no_artifact_failures:
        findings.append("failed_or_cancelled_runs_missing_artifacts")
    if per_run_findings.get("memory_candidate_low_source_overlap", 0):
        findings.append("memory_candidate_source_contradiction_detected")

    return {
        "found": True,
        "task": _serialize_task(task),
        "window_hours": window_hours,
        "window_start": _iso(window_start),
        "window_end": _iso(now),
        "run_count": total_runs,
        "status_counts": dict(status_counts),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "waiting_approval_count": waiting_approval_count,
        "running_count": running_count,
        "memory_candidate_run_count": len(memory_candidate_run_ids),
        "inspection_finding_counts": dict(per_run_findings),
        "success_rate": success_rate,
        "cancellation_rate": cancellation_rate,
        "average_duration_seconds": average_duration_seconds,
        "repeated_error_messages": repeated_error_messages,
        "findings": findings,
        "summary": ", ".join(summary_bits) + "." if summary_bits else f"No runs in the last {window_hours}h.",
    }


_TOOL_HANDLERS: dict[str, Callable[[Dict[str, Any], User], Awaitable[str]]] = {
    "fruitcake_list_tasks": _tool_list_tasks,
    "fruitcake_get_task": _tool_get_task,
    "fruitcake_propose_task_draft": _tool_propose_task_draft,
    "fruitcake_create_task": _tool_create_task,
    "fruitcake_update_task": _tool_update_task,
    "fruitcake_list_library_documents": _tool_list_library_documents,
    "fruitcake_search_library": _tool_search_library,
    "fruitcake_summarize_document": _tool_summarize_document,
    "fruitcake_get_scheduler_health": _tool_get_scheduler_health,
    "fruitcake_list_task_runs": _tool_list_task_runs,
    "fruitcake_get_task_run_artifacts": _tool_get_task_run_artifacts,
    "fruitcake_get_memory_candidates": _tool_get_memory_candidates,
    "fruitcake_inspect_task_run": _tool_inspect_task_run,
    "fruitcake_get_task_health_rollup": _tool_get_task_health_rollup,
}


@router.post("/initialize")
async def initialize(
    body: MCPRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    return _jsonrpc_result(
        body.id,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "FruitcakeAI MCP",
                "version": "0.7.10",
                "user": current_user.username,
            },
        },
    )


@router.post("/tools/list")
async def list_tools(
    body: MCPRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _ = current_user
    return _jsonrpc_result(body.id, {"tools": _TOOL_SCHEMAS})


@router.post("/tools/call")
async def call_tool(
    body: MCPRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    params = body.params or {}
    tool_name = str(params.get("name") or "").strip()
    arguments = params.get("arguments") or {}

    if not tool_name:
        return _jsonrpc_error(body.id, -32602, "Tool name is required.")
    if not isinstance(arguments, dict):
        return _jsonrpc_error(body.id, -32602, "Tool arguments must be an object.")

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return _jsonrpc_error(body.id, -32601, f"Unknown tool: {tool_name}")

    try:
        result = await handler(arguments, current_user)
    except Exception as exc:
        return _jsonrpc_error(body.id, -32000, f"Tool execution failed: {exc.__class__.__name__}", data=str(exc))

    structured = None
    text: str
    if isinstance(result, (dict, list)):
        structured = result
        text = json.dumps(result, ensure_ascii=False)
    else:
        result_text = str(result)
        try:
            structured = json.loads(result_text)
            text = json.dumps(structured, ensure_ascii=False)
        except Exception:
            text = result_text

    payload = {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ]
    }
    if structured is not None:
        payload["structuredContent"] = structured
    return _jsonrpc_result(body.id, payload)
