"""Authenticated artifact resource bridges."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import AuditLog, User
from app.db.session import get_db
from app.mcp.registry import MCPAppApprovalRequired, get_mcp_registry

router = APIRouter()
log = structlog.get_logger(__name__)


class MCPAppToolCallRequest(BaseModel):
    server: str = Field(min_length=1, max_length=160)
    resource_uri: str = Field(min_length=6, max_length=1000)
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict = Field(default_factory=dict)


class MCPAppToolApprovalDecision(BaseModel):
    approved: bool


_MCP_APP_APPROVAL_PENDING = "MCP App mutation awaiting approval"
_MCP_APP_APPROVAL_EXECUTING = "MCP App mutation approved; execution started"


def _approval_audit_tool(server: str, tool: str) -> str:
    return f"mcp_app:{server}:{tool}"[:100]


@router.get("/mcp-app-resource")
async def get_mcp_app_resource(
    server: str = Query(..., min_length=1, max_length=160),
    uri: str = Query(..., min_length=6, max_length=1000),
    current_user: User = Depends(get_current_user),
):
    """Resolve an MCP App UI resource without exposing server credentials."""
    del current_user  # Authentication is the boundary; resources remain read-only in v1.
    try:
        return await get_mcp_registry().read_mcp_app_resource(server, uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/mcp-app-tool")
async def call_mcp_app_tool(
    request: MCPAppToolCallRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mediate a same-server app-only read call without exposing credentials."""
    try:
        response = await get_mcp_registry().call_mcp_app_tool(
            server_name=request.server,
            resource_uri=request.resource_uri,
            tool_name=request.tool,
            arguments=request.arguments,
            user_context=current_user,
        )
    except ValueError as exc:
        log.warning(
            "mcp_app.tool_call_failed",
            server=request.server,
            tool=request.tool,
            status_code=400,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        log.warning(
            "mcp_app.tool_call_failed",
            server=request.server,
            tool=request.tool,
            status_code=404,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MCPAppApprovalRequired as exc:
        intent = {
            "kind": "mcp_app_mutation",
            "server": request.server,
            "resource_uri": request.resource_uri,
            "tool": request.tool,
            "arguments": request.arguments,
        }
        pending = AuditLog(
            user_id=current_user.id,
            tool=_approval_audit_tool(request.server, request.tool),
            arguments=json.dumps(intent, ensure_ascii=True, separators=(",", ":")),
            result_summary=_MCP_APP_APPROVAL_PENDING,
        )
        db.add(pending)
        await db.commit()
        await db.refresh(pending)
        return {
            "state": "waiting_approval",
            "approval": {
                "id": pending.id,
                "server": request.server,
                "resource_uri": request.resource_uri,
                "tool": request.tool,
                "title": exc.title,
                "reason": exc.description,
                "arguments": request.arguments,
                "destructive": exc.destructive,
            },
        }
    except PermissionError as exc:
        log.warning(
            "mcp_app.tool_call_failed",
            server=request.server,
            tool=request.tool,
            status_code=403,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.warning(
            "mcp_app.tool_call_failed",
            server=request.server,
            tool=request.tool,
            status_code=502,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    db.add(AuditLog(
        user_id=current_user.id,
        tool=f"mcp_app:{request.server}:{request.tool}",
        arguments=json.dumps(request.arguments, ensure_ascii=True)[:20_000],
        result_summary="Read-only MCP App tool completed",
    ))
    await db.commit()
    return {"state": "completed", **response}


@router.post("/mcp-app-tool/{approval_id}/approval")
async def resolve_mcp_app_tool_approval(
    approval_id: int,
    decision: MCPAppToolApprovalDecision,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve and, when approved, execute one exact app-originated mutation."""
    row = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.id == approval_id, AuditLog.user_id == current_user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or not row.tool.startswith("mcp_app:"):
        raise HTTPException(status_code=404, detail="MCP App approval request not found")
    if row.result_summary != _MCP_APP_APPROVAL_PENDING:
        raise HTTPException(status_code=409, detail="MCP App approval request is already resolved")

    try:
        intent = json.loads(row.arguments or "{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="MCP App approval request is invalid") from exc
    if not isinstance(intent, dict) or intent.get("kind") != "mcp_app_mutation":
        raise HTTPException(status_code=409, detail="MCP App approval request is invalid")

    if not decision.approved:
        row.result_summary = "MCP App mutation denied"
        await db.commit()
        return {"state": "denied", "approval_id": approval_id}

    row.result_summary = _MCP_APP_APPROVAL_EXECUTING
    await db.commit()
    try:
        response = await get_mcp_registry().call_mcp_app_tool(
            server_name=str(intent.get("server") or ""),
            resource_uri=str(intent.get("resource_uri") or ""),
            tool_name=str(intent.get("tool") or ""),
            arguments=intent.get("arguments") if isinstance(intent.get("arguments"), dict) else {},
            user_context=current_user,
            approved=True,
        )
    except (ValueError, LookupError, PermissionError, RuntimeError) as exc:
        row.result_summary = f"MCP App mutation failed: {str(exc)[:300]}"
        await db.commit()
        raise HTTPException(status_code=502, detail="Approved MCP App action failed") from exc

    row.result_summary = "MCP App mutation approved and completed"
    await db.commit()
    return {"state": "completed", "approval_id": approval_id, **response}
