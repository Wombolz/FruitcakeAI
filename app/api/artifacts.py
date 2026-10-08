"""Authenticated artifact resource bridges."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.mcp.registry import get_mcp_registry

router = APIRouter()


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
