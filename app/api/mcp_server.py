"""
FruitcakeAI v5 — Minimal MCP server surface for external agent testing.

This exposes a bounded authenticated HTTP MCP surface so external MCP clients
can inspect and exercise core Fruitcake behaviors directly.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.agent.context import UserContext
from app.auth.dependencies import get_current_user
from app.db.models import User

router = APIRouter()


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
            },
        },
    },
]


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


async def _tool_get_scheduler_health(arguments: Dict[str, Any], user: User) -> str:
    _ = arguments, user
    from app.autonomy.scheduler import get_llm_dispatch_health

    state = get_llm_dispatch_health()
    return json.dumps(
        {
            "status": "blocked" if state.get("blocked") else "ready",
            "blocked": bool(state.get("blocked")),
            "unhealthy_until": state.get("unhealthy_until"),
            "last_error": state.get("last_error"),
        },
        ensure_ascii=False,
        default=str,
    )


async def _tool_list_task_runs(arguments: Dict[str, Any], user: User) -> str:
    from sqlalchemy import desc, select

    from app.db.models import Task, TaskRun
    from app.db.session import AsyncSessionLocal

    try:
        limit = max(1, min(50, int(arguments.get("limit", 10))))
    except Exception:
        limit = 10
    task_id = arguments.get("task_id")

    async with AsyncSessionLocal() as db:
        query = (
            select(TaskRun, Task)
            .join(Task, TaskRun.task_id == Task.id)
            .where(Task.user_id == user.id)
            .order_by(desc(TaskRun.started_at))
            .limit(limit)
        )
        if task_id is not None:
            try:
                normalized_task_id = int(task_id)
            except Exception:
                return "task_id must be an integer."
            query = query.where(Task.id == normalized_task_id)
        rows = (await db.execute(query)).all()

    runs = [
        {
            "run_id": run.id,
            "task_id": task.id,
            "task_title": task.title,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at is not None else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at is not None else None,
            "summary": run.summary,
            "error": run.error,
            "session_id": run.session_id,
        }
        for run, task in rows
    ]
    return json.dumps({"count": len(runs), "runs": runs}, ensure_ascii=False)


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
        result_text = await handler(arguments, current_user)
    except Exception as exc:
        return _jsonrpc_error(body.id, -32000, f"Tool execution failed: {exc.__class__.__name__}", data=str(exc))

    try:
        parsed = json.loads(result_text)
        text = json.dumps(parsed, ensure_ascii=False)
    except Exception:
        text = result_text

    return _jsonrpc_result(
        body.id,
        {
            "content": [
                {
                    "type": "text",
                    "text": text,
                }
            ]
        },
    )
