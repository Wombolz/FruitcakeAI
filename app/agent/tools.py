"""
FruitcakeAI v5 — Tool registry
Defines tool schemas (OpenAI function-calling format) and dispatches
tool calls from the agent loop to the appropriate service.

Sprint 1.4: search_library wired to RAG service.
Sprint 2.1: MCP tools added here automatically via registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import structlog

from app.agent.context import UserContext

log = structlog.get_logger(__name__)

# ── Tool schema definitions ───────────────────────────────────────────────────
# These are sent to the LLM so it knows what tools it can call.

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_memory",
            "description": (
                "Store a piece of information about the user or their preferences for future sessions. "
                "Use 'semantic' for persistent facts (e.g. 'The user has a daughter named Emma'). "
                "Use 'procedural' for behavioral rules (e.g. 'Always remind the user about medication at 9am'). "
                "Use 'episodic' for time-bound events (e.g. 'The family is visiting grandparents this weekend'). "
                "Only call this when you have learned something meaningful that should persist across sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_type": {
                        "type": "string",
                        "enum": ["semantic", "procedural", "episodic"],
                        "description": "semantic=persistent fact, procedural=behavioral rule, episodic=time-bound event",
                    },
                    "content": {
                        "type": "string",
                        "description": "The memory to store, written as a clear, self-contained statement.",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance score from 0.0 to 1.0 (default 0.5). Use 0.8+ for critical facts.",
                        "default": 0.5,
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for grouping (e.g. ['health', 'family'])",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Optional ISO 8601 datetime after which this memory expires (useful for episodic events).",
                    },
                },
                "required": ["memory_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_document",
            "description": (
                "Produce a comprehensive summary of an entire document in the user's library. "
                "Use this instead of search_library when the user asks to 'summarize', "
                "'give an overview of', or 'what is in' a specific document. "
                "This tool reads ALL sections of the document, not just the most relevant chunks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_name": {
                        "type": "string",
                        "description": "Partial or full filename of the document to summarize (case-insensitive)",
                    },
                },
                "required": ["document_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_library",
            "description": (
                "Search the user's personal document library. "
                "Use this whenever the user asks about uploaded documents, "
                "household information, school papers, recipes, or any topic "
                "that might be in their files. Always cite the source document. "
                "For full-document summaries or broad questions, use top_k=30-40 "
                "and call this tool multiple times with different query angles "
                "(e.g. 'key topics', 'people mentioned', 'dates and events') "
                "to ensure comprehensive coverage of long documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of chunks to return. Use 20 for targeted questions, 30-40 for full-document summaries.",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def get_tools_for_user(user_context: UserContext) -> List[Dict[str, Any]]:
    """
    Return the complete tool list for this user/persona.

    Merges built-in tools (search_library) with MCP tools from the registry,
    then filters out any tools blocked by the user's persona.
    """
    from app.mcp.registry import get_mcp_registry

    blocked = set(user_context.blocked_tools)

    tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] not in blocked]

    registry = get_mcp_registry()
    if registry._is_ready:
        for tool in registry.get_tools_for_agent():
            if tool["function"]["name"] not in blocked:
                tools.append(tool)

    return tools


# ── Tool dispatch ─────────────────────────────────────────────────────────────

async def dispatch_tool_calls(
    tool_calls: List[Any],
    user_context: UserContext,
) -> List[Dict[str, Any]]:
    """
    Execute all tool calls from a single LLM response turn.
    Returns a list of tool-result messages ready to append to the conversation.
    Every call is logged to the AuditLog table (fire-and-forget).
    """
    results = []
    for call in tool_calls:
        tool_name = call.function.name
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}

        log.info("Tool call", tool=tool_name, args=arguments, user_id=user_context.user_id)

        try:
            result_content = await _call_tool(tool_name, arguments, user_context)
        except Exception as e:
            log.error("Tool call failed", tool=tool_name, error=str(e))
            result_content = f"Tool {tool_name} failed: {e}"

        # Audit log — fire and forget, never blocks the agent loop
        asyncio.create_task(
            _write_audit_log(
                user_id=user_context.user_id,
                session_id=user_context.session_id,
                tool_name=tool_name,
                arguments=arguments,
                result_summary=str(result_content)[:500],
            )
        )

        results.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": result_content,
            }
        )

    return results


async def _write_audit_log(
    user_id: int,
    session_id: Optional[int],
    tool_name: str,
    arguments: Dict[str, Any],
    result_summary: str,
) -> None:
    """Write a single audit log entry using its own short-lived DB session."""
    from app.db.session import AsyncSessionLocal
    from app.db.models import AuditLog

    try:
        async with AsyncSessionLocal() as db:
            entry = AuditLog(
                user_id=user_id,
                session_id=session_id,
                tool=tool_name,
                arguments=json.dumps(arguments),
                result_summary=result_summary,
            )
            db.add(entry)
            await db.commit()
    except Exception as e:
        log.warning("Audit log write failed", tool=tool_name, error=str(e))


async def _call_tool(
    name: str, arguments: Dict[str, Any], user_context: UserContext
) -> str:
    """Route a tool call to its implementation."""
    # Approval gate — armed by TaskRunner for tasks with requires_approval=True.
    # Raises ApprovalRequired before executing; the runner catches it and pauses the task.
    from app.autonomy.approval import _approval_armed, APPROVAL_REQUIRED_TOOLS, ApprovalRequired
    if _approval_armed.get() and name in APPROVAL_REQUIRED_TOOLS:
        raise ApprovalRequired(name)

    if name == "create_memory":
        return await _create_memory(arguments, user_context)

    if name == "search_library":
        return await _search_library(arguments, user_context)

    if name == "summarize_document":
        return await _summarize_document(arguments, user_context)

    # Route all other tool calls through the MCP registry
    from app.mcp.registry import get_mcp_registry
    registry = get_mcp_registry()
    if registry.knows_tool(name):
        return await registry.call_tool(name, arguments, user_context)

    return f"Unknown tool: {name}"


async def _search_library(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    """Call the RAG service and format results as a citation-rich string."""
    from app.rag.service import get_rag_service

    query = arguments.get("query", "").strip()
    top_k = int(arguments.get("top_k", 20))

    if not query:
        return "No query provided."

    rag = get_rag_service()
    if not rag.is_ready:
        return "Document library is not available right now."

    results = await rag.query(
        query_str=query,
        user_id=user_context.user_id,
        accessible_scopes=["personal", "family", "shared"],
        top_k=top_k,
    )

    if not results:
        return f"No relevant documents found for: {query}"

    # Format as a readable block the LLM can cite
    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        filename = r["metadata"].get("filename", "unknown document")
        score = r["score"]
        lines.append(f"[{i}] Source: {filename} (relevance: {score})")
        lines.append(r["text"].strip())
        lines.append("")

    return "\n".join(lines)


async def _summarize_document(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    """
    Fetch chunks from LlamaIndex's data_document_chunks table and apply map-reduce.

    For very large documents (>64 chunks) evenly-spaced stride sampling is used so
    the summary covers the whole document rather than stalling on thousands of chunks.
    Bypasses semantic search entirely — position-based coverage, not similarity.
    """
    import litellm
    from app.agent.core import _litellm_kwargs
    from app.config import settings
    from app.db.session import AsyncSessionLocal
    from app.db.models import Document
    from sqlalchemy import select, text as sql_text

    # Max chunks to feed through map-reduce (keeps total LLM calls ≤ ~9)
    MAX_CHUNKS = 64
    BATCH_SIZE = 8

    doc_name = arguments.get("document_name", "").strip()
    if not doc_name:
        return "No document name provided."

    async with AsyncSessionLocal() as db:
        # Find document by partial filename match (owner only)
        result = await db.execute(
            select(Document)
            .where(
                Document.owner_id == user_context.user_id,
                Document.original_filename.ilike(f"%{doc_name}%"),
            )
            .limit(1)
        )
        doc = result.scalar_one_or_none()
        if not doc:
            # Return actual library contents so the LLM doesn't hallucinate filenames
            all_docs = await db.execute(
                select(Document.original_filename)
                .where(Document.owner_id == user_context.user_id)
                .order_by(Document.created_at.desc())
            )
            filenames = [r[0] for r in all_docs.fetchall() if r[0]]
            if filenames:
                doc_list = "\n".join(f"- {f}" for f in filenames)
                return (
                    f"No document found matching '{doc_name}'. "
                    f"The documents actually in this user's library are:\n{doc_list}\n"
                    "Call summarize_document again with the exact filename from this list."
                )
            return (
                f"No document found matching '{doc_name}'. "
                "The library is empty — no documents have been uploaded yet."
            )

        # Fetch all non-empty chunks from LlamaIndex's table, ordered by rowid
        rows = await db.execute(
            sql_text(
                "SELECT text FROM data_document_chunks "
                "WHERE metadata_->>'document_id' = :doc_id "
                "AND text IS NOT NULL AND text != '' "
                "ORDER BY id ASC"
            ),
            {"doc_id": str(doc.id)},
        )
        all_chunks = [r[0] for r in rows.fetchall() if r[0].strip()]

    total = len(all_chunks)
    if not total:
        return (
            f"Document '{doc.original_filename}' has no indexed content yet "
            "(still processing?)."
        )

    display_name = doc.original_filename or doc_name

    # Stride-sample if the document exceeds the chunk budget
    sampled = total <= MAX_CHUNKS
    if total > MAX_CHUNKS:
        stride = total // MAX_CHUNKS
        chunks = all_chunks[::stride][:MAX_CHUNKS]
        coverage_note = (
            f"Note: This document has {total} sections. "
            f"The summary covers {len(chunks)} evenly-spaced samples from throughout."
        )
    else:
        chunks = all_chunks
        coverage_note = ""

    # ── Map-reduce summarization ──────────────────────────────────────────────
    # 900-token chunks × 8 per batch ≈ 7,200 input tokens — safe for Qwen2.5:14b

    async def summarize_batch(texts: List[str], label: str = "") -> str:
        combined = "\n\n---\n\n".join(texts)
        suffix = f" ({label})" if label else ""
        prompt = (
            f"Summarize the following sections from '{display_name}'{suffix}. "
            "Be thorough — capture all key people, events, dates, decisions, "
            "and themes:\n\n" + combined
        )
        resp = await litellm.acompletion(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            **_litellm_kwargs(),
        )
        return resp.choices[0].message.content.strip()

    if len(chunks) <= BATCH_SIZE:
        summary = await summarize_batch(chunks)
    else:
        # Map: summarize each batch independently
        batch_summaries: List[str] = []
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i : i + BATCH_SIZE]
            label = f"sections {i + 1}–{min(i + BATCH_SIZE, len(chunks))} of {len(chunks)}"
            batch_summaries.append(await summarize_batch(batch, label))

        # Reduce: collapse mini-summaries recursively until one remains
        while len(batch_summaries) > BATCH_SIZE:
            next_level: List[str] = []
            for i in range(0, len(batch_summaries), BATCH_SIZE):
                group = batch_summaries[i : i + BATCH_SIZE]
                next_level.append(await summarize_batch(group, "combined sections"))
            batch_summaries = next_level

        summary = await summarize_batch(batch_summaries, "full document")

    header = f"**Summary of '{display_name}' ({total} total sections):**"
    if coverage_note:
        header += f"\n_{coverage_note}_"
    return f"{header}\n\n{summary}"


async def _create_memory(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    """Persist a new memory for the user via MemoryService."""
    from datetime import datetime, timezone
    from app.db.session import AsyncSessionLocal
    from app.memory.service import get_memory_service

    memory_type = arguments.get("memory_type", "semantic")
    content = arguments.get("content", "").strip()
    importance = float(arguments.get("importance", 0.5))
    tags = arguments.get("tags") or []
    expires_at_str = arguments.get("expires_at")

    if not content:
        return "Memory content is required."

    if memory_type not in ("semantic", "procedural", "episodic"):
        return f"Invalid memory_type '{memory_type}'. Must be semantic, procedural, or episodic."

    expires_at = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
        except ValueError:
            return f"Invalid expires_at format: '{expires_at_str}'. Use ISO 8601."

    svc = get_memory_service()
    async with AsyncSessionLocal() as db:
        result = await svc.create(
            db=db,
            user_id=user_context.user_id,
            memory_type=memory_type,
            content=content,
            importance=importance,
            tags=tags,
            expires_at=expires_at,
        )
        await db.commit()

    if isinstance(result, str):
        # Dedup suppression message
        return result

    return f"Memory saved (id={result.id}, type={memory_type})."
