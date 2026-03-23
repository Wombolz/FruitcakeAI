"""
FruitcakeAI v5 — Tool registry
Defines tool schemas (OpenAI function-calling format) and dispatches
tool calls from the agent loop to the appropriate service.

Sprint 1.4: search_library wired to RAG service.
Sprint 2.1: MCP tools added here automatically via registry.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from typing import Any, Dict, List, Optional

import structlog

from app.agent.context import UserContext
from app.autonomy.approval import ApprovalRequired

log = structlog.get_logger(__name__)

_tool_execution_records: contextvars.ContextVar[List[Dict[str, str]]] = contextvars.ContextVar(
    "tool_execution_records",
    default=[],
)


def reset_tool_execution_records() -> contextvars.Token:
    return _tool_execution_records.set([])


def get_tool_execution_records() -> List[Dict[str, str]]:
    return list(_tool_execution_records.get())


def restore_tool_execution_records(token: contextvars.Token) -> None:
    _tool_execution_records.reset(token)


def normalize_document_name_query(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    text = text.strip().strip("\"'`")
    lowered = text.lower()
    prefix_patterns = [
        r"^(?:please\s+)?summarize\s+",
        r"^(?:please\s+)?give me an overview of\s+",
        r"^(?:please\s+)?give me a summary of\s+",
        r"^(?:please\s+)?what is in\s+",
        r"^(?:please\s+)?tell me about\s+",
    ]
    for pattern in prefix_patterns:
        lowered = re.sub(pattern, "", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"^the\s+", "", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\b(?:from|in)\s+my\s+library\b", "", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\b(?:the|this|that)\s+document\b", "", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\b(?:document|file|doc)\b", "", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\s+", " ", lowered).strip(" .,:;!?-")
    return lowered


def _tokenize_document_name(value: str) -> str:
    normalized = normalize_document_name_query(value).lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\.(pdf|md|txt|docx)\b", r" \1", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def resolve_document_name(query: str, filenames: List[str]) -> tuple[str | None, List[str]]:
    candidate = normalize_document_name_query(query)
    if not candidate:
        return None, []
    filenames = [f for f in filenames if f]
    lower_map = {f.lower(): f for f in filenames}
    if candidate.lower() in lower_map:
        return lower_map[candidate.lower()], []

    token_candidate = _tokenize_document_name(candidate)
    if not token_candidate:
        return None, []

    exact_token_matches = []
    prefix_matches = []
    substring_matches = []
    for filename in filenames:
        token_name = _tokenize_document_name(filename)
        stem_name = _tokenize_document_name(filename.rsplit(".", 1)[0])
        if token_candidate in {token_name, stem_name}:
            exact_token_matches.append(filename)
        if stem_name.startswith(token_candidate) or token_name.startswith(token_candidate):
            prefix_matches.append(filename)
        elif token_candidate and token_candidate in token_name:
            substring_matches.append(filename)

    if len(exact_token_matches) > 1:
        return None, exact_token_matches
    if len(prefix_matches) > 1:
        return None, prefix_matches
    if len(exact_token_matches) == 1 and len(prefix_matches) <= 1:
        return exact_token_matches[0], []
    if len(prefix_matches) == 1:
        return prefix_matches[0], []
    if len(substring_matches) > 1:
        return None, substring_matches
    if len(substring_matches) == 1:
        return substring_matches[0], []
    return None, []

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
                "Call this for stable personal facts, durable preferences, recurring household procedures, or meaningful near-term events that should persist beyond the current turn. "
                "Do not call it for trivial one-off chatter, temporary reasoning, or facts already present in visible memory context."
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
            "name": "create_memory_entities",
            "description": (
                "Create one or more graph-memory entities for durable relationship structure. "
                "Use this for named people, places, organizations, projects, or concepts that should participate in a memory graph. "
                "This does not replace normal flat memories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "entity_type": {"type": "string"},
                                "aliases": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "number", "default": 0.5},
                            },
                            "required": ["name"],
                        },
                    }
                },
                "required": ["entities"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_memory_relations",
            "description": (
                "Create one or more graph-memory relations between existing memory entities. "
                "Use this to record explicit relationships without changing flat memory recall."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from_entity_id": {"type": "integer"},
                                "to_entity_id": {"type": "integer"},
                                "relation_type": {"type": "string"},
                                "confidence": {"type": "number", "default": 0.5},
                                "source_memory_id": {"type": "integer"},
                                "source_task_id": {"type": "integer"},
                            },
                            "required": ["from_entity_id", "to_entity_id", "relation_type"],
                        },
                    }
                },
                "required": ["relations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_memory_observations",
            "description": (
                "Add one or more graph-memory observations to an existing entity. "
                "Prefer referencing a source memory when the observation comes from existing flat memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity_id": {"type": "integer"},
                                "content": {"type": "string"},
                                "observed_at": {"type": "string"},
                                "confidence": {"type": "number", "default": 0.5},
                                "source_memory_id": {"type": "integer"},
                                "source_task_id": {"type": "integer"},
                            },
                            "required": ["entity_id"],
                        },
                    }
                },
                "required": ["observations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory_graph",
            "description": (
                "Search graph-memory entities by name or alias. "
                "Use this when the user asks how known people, places, or projects relate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_memory_graph_nodes",
            "description": (
                "Open one graph-memory entity and return its relations and observations. "
                "Use after search_memory_graph or when the entity id is already known."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "integer"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task_plan",
            "description": (
                "Create an ordered step-by-step plan for an existing task. "
                "Use this before running complex tasks that need multiple stages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "ID of an existing task owned by the user"},
                    "goal": {"type": "string", "description": "High-level goal for the plan"},
                    "max_steps": {"type": "integer", "description": "Maximum number of steps to generate", "default": 8},
                    "notes": {"type": "string", "description": "Optional constraints or context"},
                    "style": {"type": "string", "description": "Optional style hint: concise or thorough", "default": "concise"},
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_and_run_task_plan",
            "description": (
                "Create an ordered task plan and immediately enqueue execution. "
                "Use this when the user asks you to both plan and run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Optional existing task ID owned by the user"},
                    "goal": {"type": "string", "description": "High-level goal for the plan"},
                    "max_steps": {"type": "integer", "description": "Maximum number of steps to generate", "default": 8},
                    "notes": {"type": "string", "description": "Optional constraints or context"},
                    "style": {"type": "string", "description": "Optional style hint: concise or thorough", "default": "concise"},
                },
                "required": ["goal"],
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
    {
        "type": "function",
        "function": {
            "name": "list_library_documents",
            "description": (
                "List accessible uploaded documents in the user's document library. "
                "Use for prompts about uploaded library documents, PDFs, school papers, "
                "manuals, or 'documents in my library'. "
                "Do not use this for sandbox workspace folders or files created by filesystem tools; "
                "use workspace filesystem tools like list_directory or find_files instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum documents to return (1-100).",
                        "default": 25,
                    },
                    "scope_filter": {
                        "type": "string",
                        "description": "Optional scope: personal, family, shared.",
                    },
                },
                "required": [],
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
    allowed_cap = set(user_context.allowed_tool_cap or [])

    tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] not in blocked]

    registry = get_mcp_registry()
    if registry._is_ready:
        mcp_tools = registry.get_tools_for_agent()
        has_web_search = any(t["function"]["name"] == "web_search" for t in mcp_tools)
        for tool in mcp_tools:
            tool_name = tool["function"]["name"]
            if tool_name in blocked:
                continue
            # Prefer internal web_search when available to avoid flaky generic
            # docker search tools hijacking normal web lookup prompts.
            if has_web_search and tool_name == "search":
                continue
            tools.append(tool)

    if allowed_cap:
        tools = [t for t in tools if t["function"]["name"] in allowed_cap]

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
        except ApprovalRequired:
            # Let the TaskRunner handle approval gating; do not downgrade this
            # to a tool error string or append a tool result message.
            raise
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

        records = list(_tool_execution_records.get())
        records.append({"tool": tool_name, "result_summary": str(result_content)})
        _tool_execution_records.set(records)

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

    if name == "create_memory_entities":
        return await _create_memory_entities(arguments, user_context)

    if name == "create_memory_relations":
        return await _create_memory_relations(arguments, user_context)

    if name == "add_memory_observations":
        return await _add_memory_observations(arguments, user_context)

    if name == "search_memory_graph":
        return await _search_memory_graph(arguments, user_context)

    if name == "open_memory_graph_nodes":
        return await _open_memory_graph_nodes(arguments, user_context)

    if name == "search_library":
        return await _search_library(arguments, user_context)

    if name == "summarize_document":
        return await _summarize_document(arguments, user_context)

    if name == "list_library_documents":
        return await _list_library_documents(arguments, user_context)

    if name == "create_task_plan":
        return await _create_task_plan(arguments, user_context)

    if name == "create_and_run_task_plan":
        return await _create_and_run_task_plan(arguments, user_context)

    # Route all other tool calls through the MCP registry
    from app.mcp.registry import get_mcp_registry
    registry = get_mcp_registry()

    # Compatibility bridge: if a model still emits generic "search", route it
    # through internal web_search when available.
    if name == "search" and registry.knows_tool("web_search"):
        mapped_args: Dict[str, Any] = {
            "query": arguments.get("query", ""),
            "max_results": arguments.get("max_results", arguments.get("limit", 5)),
        }
        if "region" in arguments:
            mapped_args["region"] = arguments.get("region")
        return await registry.call_tool("web_search", mapped_args, user_context)

    if registry.knows_tool(name):
        return await registry.call_tool(name, arguments, user_context)

    return f"Unknown tool: {name}"


def _parse_iso_datetime(value: str):
    from datetime import datetime, timezone

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


async def _list_library_documents(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    """Return deterministic metadata for accessible library documents."""
    from sqlalchemy import or_, select

    from app.db.models import Document
    from app.db.session import AsyncSessionLocal

    try:
        limit = int(arguments.get("limit", 25))
    except Exception:
        limit = 25
    limit = max(1, min(limit, 100))

    scope_filter = str(arguments.get("scope_filter", "") or "").strip().lower()
    if scope_filter and scope_filter not in {"personal", "family", "shared"}:
        return "scope_filter must be one of: personal, family, shared."

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Document)
            .where(
                or_(
                    Document.owner_id == user_context.user_id,
                    Document.scope.in_(["family", "shared"]),
                )
            )
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
        if scope_filter:
            stmt = stmt.where(Document.scope == scope_filter)
        rows = (await db.execute(stmt)).scalars().all()

    docs = [
        {
            "id": d.id,
            "filename": d.original_filename or d.filename,
            "scope": d.scope,
            "processing_status": d.processing_status,
            "created_at": d.created_at.isoformat() if d.created_at else "",
        }
        for d in rows
    ]
    return json.dumps({"count": len(docs), "documents": docs}, ensure_ascii=False)


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

    raw_doc_name = arguments.get("document_name", "").strip()
    doc_name = normalize_document_name_query(raw_doc_name)
    if not doc_name:
        return "No document name provided."

    async with AsyncSessionLocal() as db:
        all_docs = await db.execute(
            select(Document)
            .where(Document.owner_id == user_context.user_id)
            .order_by(Document.created_at.desc())
        )
        docs = all_docs.scalars().all()
        filenames = [d.original_filename for d in docs if d.original_filename]
        resolved_name, ambiguous = resolve_document_name(doc_name, filenames)
        doc = next((d for d in docs if d.original_filename == resolved_name), None) if resolved_name else None
        if not doc:
            if ambiguous:
                doc_list = "\n".join(f"- {f}" for f in ambiguous)
                return (
                    f"Multiple documents match '{raw_doc_name}':\n{doc_list}\n"
                    "Call summarize_document again with the exact filename from this list."
                )
            if filenames:
                doc_list = "\n".join(f"- {f}" for f in filenames)
                return (
                    f"No document found matching '{raw_doc_name}'. "
                    f"The documents actually in this user's library are:\n{doc_list}\n"
                    "Call summarize_document again with the exact filename from this list."
                )
            return (
                f"No document found matching '{raw_doc_name}'. "
                "The library is empty — no documents have been uploaded yet."
            )
        if doc.processing_status != "ready":
            return (
                f"Document '{doc.original_filename}' is not ready yet "
                f"(status: {doc.processing_status})."
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
            expires_at = _parse_iso_datetime(expires_at_str)
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
            return result
        memory_id = result.id  # capture before session closes to avoid DetachedInstanceError

    return f"Memory saved (id={memory_id}, type={memory_type})."


async def _create_memory_entities(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.memory.graph_service import get_graph_memory_service

    entities = arguments.get("entities") or []
    if not isinstance(entities, list) or not entities:
        return "entities is required."

    svc = get_graph_memory_service()
    created: list[str] = []
    async with AsyncSessionLocal() as db:
        for item in entities:
            try:
                entity, is_new = await svc.find_or_create_entity(
                    db=db,
                    user_id=user_context.user_id,
                    name=str(item.get("name", "")).strip(),
                    entity_type=str(item.get("entity_type", "unknown")).strip() or "unknown",
                    aliases=item.get("aliases") or [],
                    confidence=float(item.get("confidence", 0.5)),
                )
            except Exception as exc:
                await db.rollback()
                return f"Failed to create memory entities: {exc}"
            created.append(f"{entity.name} (id={entity.id}, {'created' if is_new else 'existing'})")
        await db.commit()
    return "Memory graph entities: " + "; ".join(created)


async def _create_memory_relations(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.memory.graph_service import get_graph_memory_service

    relations = arguments.get("relations") or []
    if not isinstance(relations, list) or not relations:
        return "relations is required."

    svc = get_graph_memory_service()
    created_ids: list[str] = []
    async with AsyncSessionLocal() as db:
        for item in relations:
            try:
                relation = await svc.create_relation(
                    db=db,
                    user_id=user_context.user_id,
                    from_entity_id=int(item.get("from_entity_id")),
                    to_entity_id=int(item.get("to_entity_id")),
                    relation_type=str(item.get("relation_type", "")).strip(),
                    confidence=float(item.get("confidence", 0.5)),
                    source_memory_id=item.get("source_memory_id"),
                    source_session_id=user_context.session_id,
                    source_task_id=item.get("source_task_id"),
                )
            except Exception as exc:
                await db.rollback()
                return f"Failed to create memory relations: {exc}"
            created_ids.append(str(relation.id))
        await db.commit()
    return "Memory graph relations created: " + ", ".join(created_ids)


async def _add_memory_observations(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.memory.graph_service import get_graph_memory_service

    observations = arguments.get("observations") or []
    if not isinstance(observations, list) or not observations:
        return "observations is required."

    svc = get_graph_memory_service()
    created_ids: list[str] = []
    async with AsyncSessionLocal() as db:
        for item in observations:
            observed_at = None
            if item.get("observed_at"):
                try:
                    observed_at = _parse_iso_datetime(str(item["observed_at"]))
                except ValueError:
                    return f"Invalid observed_at format: '{item['observed_at']}'. Use ISO 8601."
            try:
                observation = await svc.add_observation(
                    db=db,
                    user_id=user_context.user_id,
                    entity_id=int(item.get("entity_id")),
                    content=item.get("content"),
                    observed_at=observed_at,
                    confidence=float(item.get("confidence", 0.5)),
                    source_memory_id=item.get("source_memory_id"),
                    source_session_id=user_context.session_id,
                    source_task_id=item.get("source_task_id"),
                )
            except Exception as exc:
                await db.rollback()
                return f"Failed to add memory observations: {exc}"
            created_ids.append(str(observation.id))
        await db.commit()
    return "Memory graph observations created: " + ", ".join(created_ids)


async def _search_memory_graph(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.memory.graph_service import get_graph_memory_service

    query = str(arguments.get("query", "")).strip()
    if not query:
        return "query is required."

    svc = get_graph_memory_service()
    async with AsyncSessionLocal() as db:
        entities = await svc.search_entities(
            db=db,
            user_id=user_context.user_id,
            query=query,
            limit=int(arguments.get("limit", 10)),
        )
    if not entities:
        return f"No memory graph entities found for '{query}'."
    lines = [f"- id={entity.id} | {entity.name} ({entity.entity_type})" for entity in entities]
    return "Memory graph search results:\n" + "\n".join(lines)


async def _open_memory_graph_nodes(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.memory.graph_service import get_graph_memory_service

    try:
        entity_id = int(arguments.get("entity_id"))
    except Exception:
        return "entity_id is required."

    svc = get_graph_memory_service()
    async with AsyncSessionLocal() as db:
        try:
            graph = await svc.open_entity_graph(db=db, user_id=user_context.user_id, entity_id=entity_id)
        except Exception as exc:
            return f"Failed to open memory graph node: {exc}"

    entity = graph["entity"]
    relations = graph["relations"]
    observations = graph["observations"]
    lines = [f"Entity: {entity.name} (id={entity.id}, type={entity.entity_type})"]
    if relations:
        lines.append("Relations:")
        for rel in relations:
            direction = "outgoing" if rel.from_entity_id == entity.id else "incoming"
            other_id = rel.to_entity_id if direction == "outgoing" else rel.from_entity_id
            lines.append(f"- {direction}: {rel.relation_type} -> entity_id={other_id} (confidence={rel.confidence:.2f})")
    if observations:
        lines.append("Observations:")
        for obs in observations[:10]:
            content = obs.content or f"(linked to memory_id={obs.source_memory_id})"
            lines.append(f"- {content}")
    return "\n".join(lines)


async def _create_task_plan(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    return await _plan_task_common(arguments, user_context, run_after=False)


async def _create_and_run_task_plan(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    return await _plan_task_common(arguments, user_context, run_after=True)


async def _plan_task_common(
    arguments: Dict[str, Any],
    user_context: UserContext,
    run_after: bool,
) -> str:
    """Create TaskStep rows for a task; optionally enqueue immediate execution."""
    from app.agent.persona_router import infer_persona_for_task
    from app.config import settings
    from app.autonomy.planner import create_task_plan_for_user
    from app.autonomy.runner import get_task_runner
    from app.db.models import Task
    from app.db.session import AsyncSessionLocal
    from datetime import datetime, timezone

    task_id_raw = arguments.get("task_id")
    task_id: Optional[int] = None
    if task_id_raw is not None:
        try:
            task_id = int(task_id_raw)
        except Exception:
            return "Invalid task_id."

    goal = str(arguments.get("goal", "")).strip()
    if not goal:
        return "goal is required."

    try:
        max_steps = int(arguments.get("max_steps", settings.task_plan_default_steps))
    except Exception:
        max_steps = settings.task_plan_default_steps
    notes = str(arguments.get("notes", "") or "")
    style = str(arguments.get("style", "concise") or "concise")

    async with AsyncSessionLocal() as db:
        created_new_task = False
        if task_id is None:
            # Chat-first ergonomics: allow planning from goal alone.
            inferred_persona, _, _ = infer_persona_for_task(goal, notes.strip() or goal)
            task = Task(
                user_id=user_context.user_id,
                title=(goal[:255] or "Planned task"),
                instruction=notes.strip() or goal,
                persona=inferred_persona,
                task_type="one_shot",
                status="pending",
                deliver=True,
                requires_approval=False,
            )
            db.add(task)
            await db.flush()
            task_id = task.id
            created_new_task = True

        try:
            result = await create_task_plan_for_user(
                db,
                task_id=task_id,
                user_id=user_context.user_id,
                goal=goal,
                max_steps=max_steps,
                notes=notes,
                style=style,
            )
            await db.commit()
        except ValueError as exc:
            await db.rollback()
            return str(exc)

    if run_after:
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is not None and task.status != "running":
                task.status = "pending"
                task.next_run_at = datetime.now(timezone.utc)
                await db.commit()
                asyncio.create_task(get_task_runner().execute(task))
                result["run_enqueued"] = True
            else:
                result["run_enqueued"] = False
    else:
        result["run_enqueued"] = False

    result["created_task"] = created_new_task
    return json.dumps(result)
