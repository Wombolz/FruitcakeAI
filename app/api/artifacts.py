"""Authenticated artifact resource bridges."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import AuditLog, User
from app.db.session import get_db
from app.mcp.registry import get_mcp_registry

router = APIRouter()
log = structlog.get_logger(__name__)


class MCPAppToolCallRequest(BaseModel):
    server: str = Field(min_length=1, max_length=160)
    resource_uri: str = Field(min_length=6, max_length=1000)
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict = Field(default_factory=dict)


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
    return response
