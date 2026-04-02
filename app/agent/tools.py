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
            "name": "get_daily_market_data",
            "description": (
                "Fetch normalized daily market data from an approved finance provider and optionally save it to the user's library. "
                "Use this for daily OHLC and volume history datasets instead of manually chaining API calls and post-processing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol, for example 'SPY' or 'KO'."},
                    "days": {"type": "integer", "description": "Number of daily bars to return (1-100).", "default": 30},
                    "provider": {"type": "string", "description": "Approved provider name. Currently only 'alphavantage' is supported.", "default": "alphavantage"},
                    "save_to_library": {"type": "boolean", "description": "Whether to save the dataset as a library document.", "default": False},
                    "output_format": {"type": "string", "description": "Output format for the dataset: csv, json, or table.", "default": "table"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_intraday_market_data",
            "description": (
                "Fetch normalized intraday market data from an approved finance provider and optionally save it to the user's library. "
                "Use this for bounded intraday OHLC and volume datasets instead of manually chaining API calls and post-processing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol, for example 'SPY' or 'KO'."},
                    "interval": {
                        "type": "string",
                        "description": "Intraday interval. Alpha Vantage supports 1min, 5min, 15min, 30min, or 60min.",
                    },
                    "bars": {"type": "integer", "description": "Number of intraday bars to return (1-100).", "default": 30},
                    "provider": {"type": "string", "description": "Approved provider name. Currently only 'alphavantage' is supported.", "default": "alphavantage"},
                    "save_to_library": {"type": "boolean", "description": "Whether to save the dataset as a library document.", "default": False},
                    "output_format": {"type": "string", "description": "Output format for the dataset: csv, json, or table.", "default": "table"},
                    "extended_hours": {"type": "boolean", "description": "Whether to include pre-market and after-hours bars when the provider supports it.", "default": False},
                },
                "required": ["symbol", "interval"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "api_request",
                "description": (
                    "Call an approved backend-owned JSON API integration. "
                    "Use this for structured external API workflows instead of shell or web search. "
                    "Current supported service/endpoint combinations: n2yo + iss_visual_passes; alphavantage + global_quote, time_series_daily, or time_series_intraday; weather + current_conditions."
                ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Approved service name, for example 'n2yo' or 'alphavantage'."},
                    "endpoint": {"type": "string", "description": "Approved endpoint name, for example 'iss_visual_passes', 'global_quote', 'time_series_daily', or 'time_series_intraday'."},
                    "query_params": {
                        "type": "object",
                        "description": "Service-specific query parameters for the approved endpoint.",
                    },
                    "secret_name": {
                        "type": "string",
                        "description": "Optional secret name for auth, for example 'n2yo_api_key'.",
                    },
                    "auth_mode": {
                        "type": "string",
                        "description": "Optional auth mode hint. Current adapters ignore unsupported values.",
                    },
                    "response_mode": {
                        "type": "string",
                        "description": "Optional response hint. Current adapters return normalized text output.",
                    },
                    "response_fields": {
                        "type": "object",
                        "description": "Optional named JSON field selectors for deterministic backend extraction.",
                    },
                },
                "required": ["service", "endpoint", "query_params"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": (
                "Look up structured place and address results using a maps/geocoding data source. "
                "Use this for business addresses, place names, geocoding, and direct location lookups instead of web_search when the user wants addresses or locations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Business or place name to look up."},
                    "near": {"type": "string", "description": "Optional city, state, ZIP, or region to narrow the lookup."},
                    "limit": {"type": "integer", "description": "Maximum number of results to return (default 5, max 8).", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
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
            "name": "list_tasks",
            "description": (
                "List the current user's tasks with enough detail to inspect schedules, profiles, delivery, and active hours. "
                "Use this to verify what tasks exist before creating a new one or to confirm the result of a task update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "status": {"type": "string", "description": "Optional status filter."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": (
                "Get one task owned by the current user with its current schedule, profile, delivery settings, and active hours. "
                "Use this after create_task or update_task when you need to verify the exact saved state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_task_now",
            "description": (
                "Queue an existing task owned by the current user for immediate execution. "
                "Use this when the user asks to run an already-saved task now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a real persistent task for the current user. "
                "Use this only after the user has explicitly confirmed the task details. "
                "Use it for recurring watchers, briefings, maintenance tasks, or one-shot tasks the user wants saved. "
                "This creates the task row only; plan it afterward with create_task_plan unless the task is already deterministic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["one_shot", "recurring"], "default": "one_shot"},
                    "schedule": {"type": "string", "description": "Optional schedule like every:2h, cron, or ISO timestamp."},
                    "deliver": {"type": "boolean", "default": True},
                    "profile": {"type": "string", "description": "Optional built-in task profile name."},
                    "recipe_family": {
                        "type": "string",
                        "enum": [
                            "topic_watcher",
                            "daily_research_briefing",
                            "morning_briefing",
                            "iss_pass_watcher",
                            "weather_conditions",
                            "maintenance",
                        ],
                        "description": "Optional high-confidence internal recipe family for stronger task normalization.",
                    },
                    "recipe_params": {
                        "type": "object",
                        "description": "Optional structured task recipe params such as topic, path, location, or timezone.",
                    },
                    "llm_model_override": {"type": "string", "description": "Optional explicit model override for all LLM stages of this task."},
                    "persona": {"type": "string", "description": "Optional persona override."},
                    "requires_approval": {"type": "boolean", "default": True},
                    "active_hours_start": {"type": "string"},
                    "active_hours_end": {"type": "string"},
                    "active_hours_tz": {"type": "string"},
                },
                "required": ["title", "instruction", "task_type", "deliver"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Update an existing task owned by the current user. "
                "Use this after the user explicitly confirms a task change such as schedule, instruction, profile, or delivery settings. "
                "If the updated fields materially change execution, regenerate the plan afterward with create_task_plan. "
                "Use this instead of create_task when the user is modifying a task you just created."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "schedule": {"type": "string"},
                    "deliver": {"type": "boolean"},
                    "profile": {"type": "string"},
                    "recipe_family": {
                        "type": "string",
                        "enum": [
                            "topic_watcher",
                            "daily_research_briefing",
                            "morning_briefing",
                            "iss_pass_watcher",
                            "weather_conditions",
                            "maintenance",
                        ],
                    },
                    "recipe_params": {"type": "object"},
                    "llm_model_override": {"type": "string"},
                    "persona": {"type": "string"},
                    "requires_approval": {"type": "boolean"},
                    "active_hours_start": {"type": "string"},
                    "active_hours_end": {"type": "string"},
                    "active_hours_tz": {"type": "string"},
                },
                "required": ["task_id"],
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

    if name == "api_request":
        return await _api_request(arguments, user_context)

    if name == "get_daily_market_data":
        return await _get_daily_market_data(arguments, user_context)

    if name == "get_intraday_market_data":
        return await _get_intraday_market_data(arguments, user_context)

    if name == "search_places":
        return await _search_places(arguments, user_context)

    if name == "summarize_document":
        return await _summarize_document(arguments, user_context)

    if name == "list_library_documents":
        return await _list_library_documents(arguments, user_context)

    if name == "create_task_plan":
        return await _create_task_plan(arguments, user_context)

    if name == "create_and_run_task_plan":
        return await _create_and_run_task_plan(arguments, user_context)

    if name == "create_task":
        return await _create_task(arguments, user_context)

    if name == "update_task":
        return await _update_task(arguments, user_context)

    if name == "run_task_now":
        return await _run_task_now(arguments, user_context)

    if name == "list_tasks":
        return await _list_tasks(arguments, user_context)

    if name == "get_task":
        return await _get_task(arguments, user_context)

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


async def _api_request(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    from app.api_service import APIRequestError, execute_api_request
    from app.db.session import AsyncSessionLocal

    service = str(arguments.get("service", "") or "").strip()
    endpoint = str(arguments.get("endpoint", "") or "").strip()
    query_params = arguments.get("query_params") or {}
    secret_name = str(arguments.get("secret_name", "") or "").strip()
    response_mode = str(arguments.get("response_mode", "") or "").strip()
    response_fields = arguments.get("response_fields")

    if not service:
        return "service is required."
    if not endpoint:
        return "endpoint is required."
    if not isinstance(query_params, dict):
        return "query_params must be an object."
    if response_fields is not None and not isinstance(response_fields, dict):
        return "response_fields must be an object."

    if not query_params:
        reserved = {
            "service",
            "endpoint",
            "query_params",
            "secret_name",
            "auth_mode",
            "response_mode",
            "response_fields",
        }
        lifted = {
            key: value
            for key, value in arguments.items()
            if key not in reserved
        }
        if lifted:
            query_params = lifted

    async with AsyncSessionLocal() as db:
        try:
            result = await execute_api_request(
                db,
                user_id=user_context.user_id,
                service=service,
                endpoint=endpoint,
                query_params=query_params,
                secret_name=secret_name or None,
                response_mode=response_mode or None,
                response_fields=response_fields or None,
                task_id=getattr(user_context, "task_id", None),
            )
            await db.commit()
            return result
        except APIRequestError as exc:
            await db.rollback()
            return str(exc)
        except Exception as exc:
            await db.rollback()
            error_type = exc.__class__.__name__
            error_message = str(exc).strip() or error_type
            log.warning(
                "api_request failed",
                user_id=user_context.user_id,
                task_id=getattr(user_context, "task_id", None),
                service=service,
                endpoint=endpoint,
                error_type=error_type,
                error=error_message,
            )
            return f"API request failed: {error_type}"


async def _search_places(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    from app.json_api import JsonApiError, search_places

    query = str(arguments.get("query", "") or "").strip()
    near = str(arguments.get("near", "") or "").strip()
    try:
        limit = int(arguments.get("limit", 5))
    except Exception:
        limit = 5

    if not query:
        return "No place query provided."

    try:
        return await search_places(query=query, near=near or None, limit=limit)
    except JsonApiError as exc:
        log.warning(
            "search_places failed",
            user_id=user_context.user_id,
            query=query,
            near=near or None,
            error=str(exc),
        )
        return str(exc)


async def _get_daily_market_data(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    from app.api_errors import APIRequestError
    from app.db.session import AsyncSessionLocal
    from app.market_data_service import get_daily_market_data

    symbol = str(arguments.get("symbol", "") or "").strip()
    provider = str(arguments.get("provider", "alphavantage") or "alphavantage").strip()
    output_format = str(arguments.get("output_format", "table") or "table").strip()
    save_to_library = bool(arguments.get("save_to_library", False))
    try:
        days = int(arguments.get("days", 30))
    except Exception:
        days = 30

    if not symbol:
        return "symbol is required."

    async with AsyncSessionLocal() as db:
        try:
            payload = await get_daily_market_data(
                db,
                user_id=user_context.user_id,
                symbol=symbol,
                days=days,
                provider=provider,
                save_to_library=save_to_library,
                output_format=output_format,
            )
            await db.commit()
        except APIRequestError as exc:
            await db.rollback()
            return str(exc)
        except Exception as exc:
            await db.rollback()
            log.warning(
                "get_daily_market_data failed",
                user_id=user_context.user_id,
                symbol=symbol,
                provider=provider,
                error=str(exc),
            )
            return "Daily market data request failed."

    rendered = str(payload.get("rendered") or "").strip()
    if payload.get("saved_document_id"):
        rendered = (
            f"{rendered}\n\nSaved to library as {payload.get('saved_document_name')} "
            f"(document_id={payload.get('saved_document_id')}, scope=personal)."
        ).strip()
    return rendered


async def _get_intraday_market_data(
    arguments: Dict[str, Any], user_context: UserContext
) -> str:
    from app.api_errors import APIRequestError
    from app.db.session import AsyncSessionLocal
    from app.market_data_service import get_intraday_market_data

    symbol = str(arguments.get("symbol", "") or "").strip()
    interval = str(arguments.get("interval", "") or "").strip()
    provider = str(arguments.get("provider", "alphavantage") or "alphavantage").strip()
    output_format = str(arguments.get("output_format", "table") or "table").strip()
    save_to_library = bool(arguments.get("save_to_library", False))
    extended_hours = bool(arguments.get("extended_hours", False))
    try:
        bars = int(arguments.get("bars", 30))
    except Exception:
        bars = 30

    if not symbol:
        return "symbol is required."
    if not interval:
        return "interval is required."

    async with AsyncSessionLocal() as db:
        try:
            payload = await get_intraday_market_data(
                db,
                user_id=user_context.user_id,
                symbol=symbol,
                interval=interval,
                bars=bars,
                provider=provider,
                save_to_library=save_to_library,
                output_format=output_format,
                extended_hours=extended_hours,
            )
            await db.commit()
        except APIRequestError as exc:
            await db.rollback()
            return str(exc)
        except Exception as exc:
            await db.rollback()
            log.warning(
                "get_intraday_market_data failed",
                user_id=user_context.user_id,
                symbol=symbol,
                interval=interval,
                provider=provider,
                error=str(exc),
            )
            return "Intraday market data request failed."

    rendered = str(payload.get("rendered") or "").strip()
    if payload.get("saved_document_id"):
        rendered = (
            f"{rendered}\n\nSaved to library as {payload.get('saved_document_name')} "
            f"(document_id={payload.get('saved_document_id')}, scope=personal)."
        ).strip()
    return rendered


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
    from app.llm_usage import record_llm_usage_event
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
        await record_llm_usage_event(
            resp,
            user_id=user_context.user_id,
            source="tool_document_summary",
            stage="tool_document_summary",
            model=settings.llm_model,
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


async def _create_task(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.session import AsyncSessionLocal
    from app.task_recipes import build_task_confirmation_text, build_task_recipe_summary
    from app.task_service import TaskValidationError, create_task_record

    title = str(arguments.get("title", "") or "").strip()
    instruction = str(arguments.get("instruction", "") or "").strip()
    task_type = str(arguments.get("task_type", "one_shot") or "one_shot").strip()

    if not title:
        return "title is required."
    if not instruction:
        return "instruction is required."
    if task_type not in {"one_shot", "recurring"}:
        return "task_type must be one_shot or recurring."

    async with AsyncSessionLocal() as db:
        try:
            task = await create_task_record(
                db,
                user_id=user_context.user_id,
                title=title,
                instruction=instruction,
                persona=arguments.get("persona"),
                profile=arguments.get("profile"),
                recipe_family=arguments.get("recipe_family"),
                recipe_params=arguments.get("recipe_params"),
                llm_model_override=arguments.get("llm_model_override"),
                task_type=task_type,
                schedule=arguments.get("schedule"),
                deliver=bool(arguments.get("deliver", True)),
                requires_approval=bool(arguments.get("requires_approval", True)),
                active_hours_start=arguments.get("active_hours_start"),
                active_hours_end=arguments.get("active_hours_end"),
                active_hours_tz=arguments.get("active_hours_tz"),
                user_timezone=user_context.timezone,
            )
            await db.commit()
        except TaskValidationError as exc:
            await db.rollback()
            return str(exc)

    return json.dumps(
        {
            "created": True,
            "task_id": task.id,
            "title": task.title,
            "instruction": task.instruction,
            "persona": task.persona,
            "profile": task.profile,
            "task_recipe": task.task_recipe or None,
            "task_summary": build_task_recipe_summary(
                title=task.title,
                task_type=task.task_type,
                schedule=task.schedule,
                task_recipe=task.task_recipe,
                profile=task.profile,
            ),
            "task_confirmation": build_task_confirmation_text(
                title=task.title,
                task_type=task.task_type,
                schedule=task.schedule,
                task_recipe=task.task_recipe,
                profile=task.profile,
            ),
            "llm_model_override": task.llm_model_override,
            "task_type": task.task_type,
            "schedule": task.schedule,
            "deliver": task.deliver,
            "requires_approval": task.requires_approval,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at is not None else None,
        }
    )


async def _update_task(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.models import Task
    from app.db.session import AsyncSessionLocal
    from app.task_recipes import build_task_confirmation_text, build_task_recipe_summary
    from app.task_service import TaskValidationError, UNSET, update_task_record

    task_id_raw = arguments.get("task_id")
    try:
        task_id = int(task_id_raw)
    except Exception:
        return "Invalid task_id."

    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None or int(task.user_id) != int(user_context.user_id):
            return "Task not found."

        try:
            update_result = await update_task_record(
                db,
                task,
                title=arguments["title"] if "title" in arguments else UNSET,
                instruction=arguments["instruction"] if "instruction" in arguments else UNSET,
                persona=arguments["persona"] if "persona" in arguments else UNSET,
                profile=arguments["profile"] if "profile" in arguments else UNSET,
                recipe_family=arguments["recipe_family"] if "recipe_family" in arguments else UNSET,
                recipe_params=arguments["recipe_params"] if "recipe_params" in arguments else UNSET,
                llm_model_override=arguments["llm_model_override"] if "llm_model_override" in arguments else UNSET,
                schedule=arguments["schedule"] if "schedule" in arguments else UNSET,
                deliver=arguments["deliver"] if "deliver" in arguments else UNSET,
                requires_approval=arguments["requires_approval"] if "requires_approval" in arguments else UNSET,
                active_hours_start=arguments["active_hours_start"] if "active_hours_start" in arguments else UNSET,
                active_hours_end=arguments["active_hours_end"] if "active_hours_end" in arguments else UNSET,
                active_hours_tz=arguments["active_hours_tz"] if "active_hours_tz" in arguments else UNSET,
                user_timezone=user_context.timezone,
            )
            await db.commit()
        except TaskValidationError as exc:
            await db.rollback()
            return str(exc)

    return json.dumps(
        {
            "updated": True,
            "task_id": task.id,
            "title": task.title,
            "instruction": task.instruction,
            "persona": task.persona,
            "profile": task.profile,
            "task_recipe": task.task_recipe or None,
            "task_summary": build_task_recipe_summary(
                title=task.title,
                task_type=task.task_type,
                schedule=task.schedule,
                task_recipe=task.task_recipe,
                profile=task.profile,
            ),
            "task_confirmation": build_task_confirmation_text(
                title=task.title,
                task_type=task.task_type,
                schedule=task.schedule,
                task_recipe=task.task_recipe,
                profile=task.profile,
            ),
            "llm_model_override": task.llm_model_override,
            "task_type": task.task_type,
            "schedule": task.schedule,
            "deliver": task.deliver,
            "requires_approval": task.requires_approval,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at is not None else None,
            "plan_inputs_changed": update_result.plan_inputs_changed,
        }
    )


async def _run_task_now(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.autonomy.runner import get_task_runner
    from app.db.models import Task, TaskRun
    from app.db.session import AsyncSessionLocal

    task_id_raw = arguments.get("task_id")
    try:
        task_id = int(task_id_raw)
    except Exception:
        return "Invalid task_id."

    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None or int(task.user_id) != int(user_context.user_id):
            return "Task not found."
        run_rows = await db.execute(
            select(TaskRun)
            .where(
                TaskRun.task_id == task.id,
                TaskRun.status.in_(["running", "waiting_approval"]),
            )
            .order_by(TaskRun.id.desc())
        )
        if task.status == "running" or run_rows.scalars().first() is not None:
            return json.dumps({"queued": False, "task_id": task.id, "detail": "Task is already running"})

        task.next_run_at = datetime.now(timezone.utc)
        task.status = "pending"
        await db.commit()
        asyncio.create_task(get_task_runner().execute(task))

        return json.dumps(
            {
                "queued": True,
                "task_id": task.id,
                "title": task.title,
                "schedule": task.schedule,
            }
        )


async def _list_tasks(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from sqlalchemy import desc, select

    from app.db.models import Task
    from app.db.session import AsyncSessionLocal

    try:
        limit = max(1, min(100, int(arguments.get("limit", 20))))
    except Exception:
        limit = 20
    status_filter = str(arguments.get("status", "") or "").strip()

    async with AsyncSessionLocal() as db:
        query = (
            select(Task)
            .where(Task.user_id == user_context.user_id)
            .order_by(desc(Task.created_at))
            .limit(limit)
        )
        if status_filter:
            query = query.where(Task.status == status_filter)
        rows = await db.execute(query)
        tasks = rows.scalars().all()

    return json.dumps(
        {
            "count": len(tasks),
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "profile": task.profile,
                    "task_type": task.task_type,
                    "status": task.status,
                    "schedule": task.schedule,
                    "deliver": task.deliver,
                    "requires_approval": task.requires_approval,
                    "active_hours_start": task.active_hours_start,
                    "active_hours_end": task.active_hours_end,
                    "active_hours_tz": task.active_hours_tz,
                    "created_at": task.created_at.isoformat() if task.created_at is not None else None,
                    "next_run_at": task.next_run_at.isoformat() if task.next_run_at is not None else None,
                }
                for task in tasks
            ],
        }
    )


async def _get_task(arguments: Dict[str, Any], user_context: UserContext) -> str:
    from app.db.models import Task
    from app.db.session import AsyncSessionLocal

    task_id_raw = arguments.get("task_id")
    try:
        task_id = int(task_id_raw)
    except Exception:
        return "Invalid task_id."

    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None or int(task.user_id) != int(user_context.user_id):
            return "Task not found."

    return json.dumps(
        {
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
            "created_at": task.created_at.isoformat() if task.created_at is not None else None,
            "last_run_at": task.last_run_at.isoformat() if task.last_run_at is not None else None,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at is not None else None,
        }
    )


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
