# 🍰 FruitcakeAI v5 — Rebuild Roadmap

**Version**: 5.1  
**Status**: Phase 1 Complete ✅ · Phase 2 In Progress 🚧  
**Philosophy**: Agent-first. The AI orchestrates the tools — not the other way around.  
**Build Location**: `/Users/jwomble/Development/fruitcake_v5/`  
**Last Updated**: March 2026

---

## Executive Summary

FruitcakeAI v5 is a clean rebuild that preserves the best ideas from v3/v4 — hybrid RAG retrieval, multi-user/persona support, MCP tool integration — while discarding the complexity that made v3/v4 cumbersome: the ServiceOrchestrator, PolicyRouter, intent detection keyword system, and enterprise-scale infrastructure aspirations.

The core mental model shift:

> **v3/v4**: A platform that contains an AI  
> **v5**: An AI agent that has tools

Orchestration moves from hand-written rules into the LLM itself. New capabilities are added via MCP configuration, not code. Multi-user support is injected context, not an enforcement layer.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              React Frontend                 │
│         (keep existing — rewire API)        │
└─────────────────────┬───────────────────────┘
                      │ WebSocket / REST
┌─────────────────────▼───────────────────────┐
│           FastAPI — Thin Layer              │
│   Auth (JWT) · File Upload · Chat API       │
│   User/Session Management                  │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│              Agent Core                     │
│   LiteLLM (model-agnostic)                  │
│   System prompt = user context + persona    │
│   Tool-calling drives all orchestration     │
└──────┬───────────┬───────────┬──────────────┘
       │           │           │
┌──────▼───┐ ┌─────▼────┐ ┌───▼─────────────┐
│   RAG    │ │Calendar  │ │  Web / RSS / etc │
│LlamaIndex│ │  MCP     │ │   MCP Servers    │
│pgvector  │ │          │ │ (Docker, stdio)  │
└──────┬───┘ └──────────┘ └─────────────────┘
       │
┌──────▼────────────────────┐
│   PostgreSQL + pgvector   │
│   Redis (optional, Phase 4)│
└───────────────────────────┘
```

---

## Project Structure

```
fruitcake_v5/
├── app/
│   ├── main.py                    # FastAPI app, startup, routers
│   ├── config.py                  # Pydantic settings from .env
│   ├── auth/
│   │   ├── router.py              # /auth/login, /auth/me, /auth/register
│   │   ├── models.py              # User, Session DB models
│   │   └── jwt.py                 # JWT encode/decode helpers
│   ├── agent/
│   │   ├── core.py                # Agent loop — LiteLLM + tool dispatch
│   │   ├── context.py             # UserContext builder (persona injection)
│   │   ├── tools.py               # Tool registry (wraps MCP + internal tools)
│   │   └── prompts.py             # System prompt templates
│   ├── rag/
│   │   ├── service.py             # LlamaIndex setup, query engine
│   │   ├── ingest.py              # Document ingestion pipeline
│   │   ├── retriever.py           # Hybrid BM25 + vector + RRF
│   │   └── config.yaml            # LlamaIndex configuration
│   ├── mcp/
│   │   ├── client.py              # MCP stdio/Docker client (from v4)
│   │   ├── registry.py            # Auto-discovery from mcp_config.yaml
│   │   └── servers/
│   │       ├── mcp_config.yaml    # All MCP server definitions
│   │       ├── calendar.py        # Calendar MCP wrapper
│   │       ├── web_research.py    # Web research MCP wrapper
│   │       └── rss.py             # RSS MCP wrapper
│   ├── api/
│   │   ├── chat.py                # /chat/sessions, /chat/messages (WebSocket)
│   │   ├── library.py             # /library/ingest, /library/query
│   │   └── admin.py               # /admin/health, /admin/metrics (simple)
│   └── db/
│       ├── models.py              # SQLAlchemy models (users, sessions, docs)
│       ├── session.py             # Async DB session
│       └── migrations/            # Alembic migrations
├── frontend/                      # Existing React app (rewire API calls)
├── config/
│   ├── mcp_config.yaml            # MCP server definitions
│   ├── personas.yaml              # User persona definitions
│   └── users.yaml                 # Default user seed config
├── tests/
│   ├── test_agent.py
│   ├── test_rag.py
│   ├── test_auth.py
│   └── test_mcp.py
├── scripts/
│   ├── start.sh                   # One-command startup (includes Ollama health check)
│   └── reset.sh                   # Wipe and reseed DB for development
├── docker-compose.yml
├── .env / .env.example
├── requirements.txt
└── README.md
```

---

## ⚠️ Ground Truth: Verified Working Configuration

This is the actual working setup as of Phase 1 completion. All code examples in this document reflect these findings — not the original roadmap sketches.

### Hardware
- **Machine**: M1 Max, 64GB RAM (macOS)
- **Verified LLM**: `qwen2.5:14b` via Ollama ✅
- **Attempted**: `llama3.3:70b` (~43GB) — **crashes Ollama** due to memory pressure alongside embedding model + macOS overhead. May work with all other apps closed, or use `qwen2.5:32b` (~20GB) as a middle-ground option.

### LiteLLM / Ollama Critical Notes

**Use `ollama_chat/` prefix, not `ollama/`**
```env
# ✅ CORRECT — uses /api/chat, supports tool/function calling
LLM_MODEL=ollama_chat/qwen2.5:14b

# ❌ WRONG — uses /api/generate, tool calls silently broken
LLM_MODEL=ollama/qwen2.5:14b
```

**Always pass `api_base` explicitly**  
LiteLLM may attempt cloud endpoints without it. Use the `_litellm_kwargs()` helper in `app/agent/core.py` which strips the `/v1` suffix:
```python
def _litellm_kwargs(self) -> dict:
    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return {"api_base": base, "model": settings.llm_model}
```

**Check `message.tool_calls`, not `finish_reason`**  
Ollama with qwen2.5:14b returns `finish_reason="stop"` even when tool calls are present. The agent loop must check directly:
```python
# ✅ CORRECT
while message.tool_calls:
    ...

# ❌ WRONG — misses tool calls with Ollama
while response.choices[0].finish_reason == "tool_calls":
    ...
```

**`_normalize_tool_calls()` is required**  
After `message.model_dump(exclude_none=True)`, the `arguments` field is converted from a JSON string to a Python dict. On the next LiteLLM call, the token counter crashes: `TypeError: can only concatenate str (not "dict") to str`. Fix: re-serialize before appending to history:
```python
def _normalize_tool_calls(message_dict: dict) -> dict:
    if "tool_calls" in message_dict:
        for tc in message_dict["tool_calls"]:
            if isinstance(tc.get("function", {}).get("arguments"), dict):
                tc["function"]["arguments"] = json.dumps(
                    tc["function"]["arguments"]
                )
    return message_dict
```

### Package Versions (requirements.txt — actual)
```
litellm>=1.82.0          # 1.55.3 has token_counter crash with Ollama tool schemas
bcrypt>=3.1,<4.1
email-validator>=2.0
aiosqlite>=0.19.0
```

> ⚠️ **Known conflict**: `litellm>=1.82.0` pulls `openai>=2.24.0`, which conflicts with `llama-index-llms-openai` requiring `openai<2`. Since we disable LlamaIndex's LLM entirely (`Settings.llm = None`), this is currently harmless. Resolve in Sprint 2.1 with a package pin.

### Alembic Configuration
```ini
# alembic.ini — must use asyncpg, not psycopg2
sqlalchemy.url = postgresql+asyncpg://user:pass@localhost/fruitcake_v5
```
`DATABASE_URL_SYNC` (psycopg2) is kept in `.env` for other sync uses but is not consumed by Alembic.

### LlamaIndex Startup Warning
```
LLM is explicitly disabled. Using MockLLM.
```
This is **expected and harmless**. We set `Settings.llm = None` intentionally — all inference goes through LiteLLM, not LlamaIndex's LLM layer.

### BM25 on Empty Docstore
BM25Retriever throws `RuntimeWarning: max() arg is an empty sequence` on fresh startup. This is handled by the `has_docs` guard in `app/rag/retriever.py` — BM25 is skipped until the first document is ingested, then activated via `_rebuild_retriever()`. RuntimeWarnings are suppressed with `warnings.catch_warnings()` during BM25 initialization.

---

## Phase 1: Agent Core + RAG Foundation ✅ COMPLETE
**Completed**: March 2026  
**Actual Duration**: ~2 weeks

### What Was Built
- FastAPI thin layer → LiteLLM agent core → LlamaIndex RAG + pgvector
- JWT auth with roles (admin/parent/child/guest)
- Persona/UserContext injected as system prompt
- `search_library` as first agent tool, wired to hybrid RAG
- REST chat API + WebSocket streaming chat
- Hybrid BM25 + vector retriever with RRF fusion

### Verified Endpoints
```
POST /auth/register  → 201 Created
POST /auth/login     → 200 { access_token, refresh_token }
POST /chat/sessions  → 201 { id, title, persona }
POST /chat/sessions/{id}/messages → LLM responds as family_assistant persona
  - With documents ingested: agent calls search_library, RAG retrieves chunks,
    LLM synthesizes answer with source attribution ✅
POST /library/ingest → 201 { id, filename, chunks, status: ready }
GET  /library/query  → semantic search results ✅
```

### Sprint 1.1 — Project Bootstrap ✅
### Sprint 1.2 — Auth System ✅
### Sprint 1.3 — LlamaIndex RAG Service ✅
### Sprint 1.4 — Agent Core ✅

---

## Phase 2: MCP Tools + Multi-User Polish 🚧
**Duration**: 2 weeks  
**Goal**: All v3 service capabilities restored as MCP tools. Multi-user context fully working.  
**Success Metric**: Different family members get appropriately scoped responses; calendar/web/RSS tools work.

---

### Pre-Sprint: Tech Debt Cleanup
**Before starting Sprint 2.1, clear the Phase 1 tech debt. These are small fixes — budget half a day.**

- [ ] **`send_message` missing `db.commit()`** — REST chat endpoint relies on implicit session cleanup. Add explicit `await db.commit()` after saving the message. WebSocket handler already does this correctly.

- [ ] **`scope` Form field annotation** — In `POST /library/ingest`, the `scope` multipart field isn't being parsed correctly alongside `UploadFile`. Add `Form(...)` annotation:
  ```python
  async def ingest_document(
      file: UploadFile = File(...),
      scope: str = Form("personal"),   # ← add Form(...) annotation
      ...
  ):
  ```
  Without this, documents always default to `"personal"` regardless of what the client sends.

- [ ] **Package conflict pin** — Add to `requirements.txt` to prevent the `openai>=2` conflict from becoming a runtime issue:
  ```
  # Prevent llama-index-llms-openai conflict (we use LiteLLM not LlamaIndex LLM)
  llama-index-llms-openai==0.4.1   # pin to openai<2 compatible version
  ```
  Or alternatively, remove `llama-index-llms-openai` from dependencies entirely since we don't use it.

---

### Sprint 2.1 — MCP Infrastructure (Days 1-3)

Port Docker-based MCP client from v4 — the transport code is production-proven, port directly, don't rewrite.

**Tasks**:
- [ ] Copy `app/services/mcp_client/` from v4 → `app/mcp/client.py`
  - Retain Docker stdio transport pattern
  - Retain on-demand container execution (`docker run -i --rm`)
  - Retain tool discovery via MCP server metadata
- [ ] Create `app/mcp/registry.py` — auto-discovery from `config/mcp_config.yaml`:
  ```python
  class MCPRegistry:
      async def load_from_config(self, config_path: str):
          # Auto-register servers without code changes
          # Each server provides its own tool schemas via MCP metadata
      
      async def get_tools_for_agent(self) -> list[dict]:
          # Returns LiteLLM-compatible tool schemas for all enabled servers
  ```
- [ ] Create `config/mcp_config.yaml`:
  ```yaml
  mcp_servers:
    calendar:
      type: internal_python
      module: app.mcp.servers.calendar
      enabled: true

    web_research:
      type: internal_python
      module: app.mcp.servers.web_research
      enabled: true
      providers: [brave, duckduckgo, newsapi]

    rss:
      type: internal_python
      module: app.mcp.servers.rss
      enabled: true

    python_refactoring:
      type: docker_stdio
      image: mcp/mcp-python-refactoring
      enabled: true
      priority: 10

    sequential_thinking:
      type: docker_stdio
      image: mcp/sequentialthinking
      enabled: true
      priority: 15

    filesystem:
      type: docker_stdio
      image: mcp/mcp-filesystem
      enabled: false    # Enable when needed
  ```
- [ ] Update `app/agent/tools.py` to pull full tool list from MCP registry at session start
- [ ] Tool schemas auto-generated from MCP server metadata — no hard-coding tool signatures
- [ ] Add `GET /admin/tools` endpoint listing all registered tools and their status

**Acceptance Criteria**: `GET /admin/tools` lists all enabled tools; adding a new entry to `mcp_config.yaml` makes it available to the agent without code changes; Docker MCP servers launch on demand.

---

### Sprint 2.2 — Calendar, Web Research, RSS Tools (Days 4-8)

Port service logic from v4, repackage as clean MCP tool wrappers. Reference the v4 service files directly in Cursor (`@app/services/calendar/service.py`, etc.).

**Tasks**:

- [ ] **`app/mcp/servers/calendar.py`**:
  - Port from: `v4/app/services/calendar/service.py` + `providers.py`
  - Tools to expose:
    - `list_events(start_date, end_date, calendar_id?)` → formatted event list
    - `create_event(title, start, end, calendar_id, description?)` → confirmation
    - `search_events(query, days_back?)` → matching events
  - Providers: Google Calendar, Apple Calendar (EventKit)
  - User scoping: filter by `user_context.calendar_access` — users only see their permitted calendars
  - Return structured dicts, not raw API objects

- [ ] **`app/mcp/servers/web_research.py`**:
  - Port from: `v4/app/services/web_research/service.py`
  - Tools to expose:
    - `web_search(query, num_results?)` → list of `{title, url, snippet}`
    - `fetch_page(url)` → cleaned text content of a URL
  - Providers: Brave (primary), DuckDuckGo (fallback), NewsAPI (news queries)
  - Provider selected automatically based on query type

- [ ] **`app/mcp/servers/rss.py`**:
  - Port from: `v4/app/services/rss/service.py`
  - Tools to expose:
    - `get_feed_items(feed_url, limit?)` → recent items with title/link/summary
    - `search_feeds(query, feed_urls)` → items matching query across feeds
  - Bias analysis: skip for now, add back in Phase 3 if desired

**Acceptance Criteria**:
- "What's on the family calendar this week?" → agent calls `list_events`, returns formatted schedule ✅
- "Search the web for X" → agent calls `web_search`, summarizes results ✅
- Child persona cannot call `web_research` tools (blocked by persona config) ✅

---

### Sprint 2.3 — Persona System (Days 9-11)

Replaces the v4 PolicyRouter entirely. Configuration-driven, inspectable, easy to extend.

**Tasks**:
- [ ] Create `config/personas.yaml`:
  ```yaml
  personas:
    family_assistant:
      description: General family assistant with access to all shared resources
      tone: friendly and helpful
      library_scopes: [family_docs, recipes, household]
      calendar_access: [family, personal]
      blocked_tools: []

    kids_assistant:
      description: Safe, age-appropriate assistant for children
      tone: encouraging and simple
      library_scopes: [kids_books, homework]
      calendar_access: [family]
      content_filter: strict
      blocked_tools: [web_research, web_search, fetch_page]

    work_assistant:
      description: Focused on professional tasks
      tone: professional and concise
      library_scopes: [work_docs, projects]
      calendar_access: [work, personal]
      blocked_tools: []
  ```

- [ ] Update `config/users.yaml` — assign default persona per user:
  ```yaml
  users:
    - username: james
      role: admin
      default_persona: family_assistant
    - username: sarah
      role: parent
      default_persona: family_assistant
      allergies: [lactose]        # example of user-specific context
    - username: kids
      role: child
      default_persona: kids_assistant
  ```
  Note: dietary restrictions / personal context like allergies can be surfaced in the system prompt for relevant queries.

- [ ] Update `UserContext` to include persona fields: `blocked_tools`, `content_filter`, `calendar_access`
- [ ] `app/agent/tools.py`: filter tool list at session start using `persona.blocked_tools`
- [ ] Support persona switching via chat command: `/persona work_assistant`
- [ ] Persona selection persists in the session record in DB

**Acceptance Criteria**: Child user cannot invoke web research tools; `/persona work_assistant` switches tone and scope mid-session; blocked tools are absent from the LiteLLM tool schema sent to the model.

---

### Sprint 2.4 — Multi-User Polish (Days 12-14)

**Tasks**:
- [ ] User management API:
  - `GET /admin/users` — list all users with roles and personas
  - `POST /admin/users` — create new user
  - `PATCH /admin/users/{id}` — update role, persona, or scopes
- [ ] Document ownership model:
  - Every document has `owner_id` and `scope`: `personal | family | shared`
  - `personal` → visible only to owner
  - `family` → visible to all users in the household
  - `shared` → same as family (reserved for future multi-household use)
- [ ] Family shared library: documents with `scope=family` returned for any authenticated user's RAG queries
- [ ] Audit log table: every agent tool call logged with `user_id`, `tool_name`, `parameters_hash`, `timestamp`
- [ ] `GET /admin/audit` endpoint — admin can review recent tool activity
- [ ] `GET /admin/health` — check Ollama, PostgreSQL, embedding model all responding

**Acceptance Criteria**: Admin sees all users and audit log; shared documents returned for all users; personal documents scoped correctly; health endpoint reports accurate status.

---

## Phase 3: Frontend + Production Ready
**Duration**: 2 weeks  
**Goal**: Existing React frontend wired to v5 API. System stable enough for daily family use.  
**Success Metric**: Full daily use by real family members without intervention.

### Sprint 3.1 — Frontend API Migration (Days 1-5)

**Tasks**:
- [ ] Audit all API calls in existing React frontend — map v4 endpoints to v5 equivalents
- [ ] Update base URL and auth token handling (should be largely identical)
- [ ] Update chat interface to handle streaming WebSocket responses
- [ ] **Tool call visualization**: show a subtle indicator when agent is calling a tool (searching library, checking calendar, etc.) — improves perceived responsiveness during RAG lookups
- [ ] Document upload UI → `POST /library/ingest` with scope selector (personal/family)
- [ ] Persona switcher in UI sidebar — dropdown populated from `GET /personas`
- [ ] User management panel (admin role only)

**Acceptance Criteria**: Frontend connects to v5 API; streaming chat works; document upload with scope works; persona switching works; no v4 API dependencies remain.

---

### Sprint 3.2 — Stability & Developer Experience (Days 6-10)

**Tasks**:
- [ ] `GET /health` — detailed status: Ollama reachable, DB connected, embedding model loaded, MCP servers registered
- [ ] `GET /admin/metrics` — simple counters: total requests, token counts, p50/p95 latency, error rate. No Prometheus/Grafana yet — a JSON endpoint is enough.
- [ ] Structured JSON logging with `trace_id` on every request (already partially in place from Phase 1)
- [ ] Graceful startup in `scripts/start.sh`:
  1. Wait for PostgreSQL
  2. Wait for Ollama (`ollama list`)
  3. Run Alembic migrations
  4. Start uvicorn
- [ ] `scripts/reset.sh` — drop DB, recreate, run migrations, seed users
- [ ] User-friendly error responses — agent failures never expose stack traces to frontend

**Acceptance Criteria**: `./scripts/start.sh` brings up full system reliably; `/health` accurately reports dependency status; errors are logged with trace IDs and return clean messages to client.

---

### Sprint 3.3 — Testing & Documentation (Days 11-14)

**Tasks**:
- [ ] `tests/test_agent.py` — integration tests for agent tool-calling:
  - Query with ingested doc → verifies `search_library` is called
  - Calendar query → verifies `list_events` is called
  - Child user query → verifies blocked tools are not offered
- [ ] `tests/test_rag.py` — RAG retrieval quality:
  - Port golden dataset eval harness from v4
  - Target: Recall@10 > 0.7, NDCG@10 > 0.65
  - BM25 + vector hybrid outperforms vector-only baseline
- [ ] `tests/test_auth.py` — auth and user scoping:
  - Login, token validation, role enforcement
  - Document scoping: personal vs family
- [ ] `tests/test_mcp.py`:
  - MCP registry loads all enabled servers from config
  - Tool schemas are valid LiteLLM format
  - Docker MCP servers start and respond

- [ ] `README.md` — quick start guide, under 30 minutes to first working chat
- [ ] `docs/ADDING_MCP_TOOLS.md` — how to add a new MCP server (config only, no code)
- [ ] `docs/PERSONA_SYSTEM.md` — persona and user scope configuration guide
- [ ] `docs/LLM_BACKENDS.md` — how to switch between Ollama, Claude, OpenAI

**Acceptance Criteria**: `pytest tests/` passes; RAG quality targets met; README tested by someone who wasn't involved in the build.

---

## Future Phases

Good ideas that belong *after* daily use is proven. Add them in response to real friction, not speculation.

| Phase | Feature | Trigger to Start |
|-------|---------|-----------------|
| 4 | Redis caching (embeddings + sessions) | Noticeable latency in daily use |
| 4 | HNSW vector indexing | Library exceeds ~10k documents |
| 4 | Background ingestion queue | Large file uploads blocking chat |
| 5 | Voice interface | Actively wanted by family members |
| 5 | Mobile / PWA | Phone becomes primary access device |
| 6 | Multimodal (image/audio ingestion) | Specific use case identified |
| 6 | Email integration MCP | Actively requested |
| 7 | Enterprise fork | Home version proven, business interest confirmed |

---

## LLM Backend Configuration

Switch backends via `.env` — no code changes required.

```env
# ✅ DEFAULT — Local via Ollama (privacy-first, verified working on M1 Max 64GB)
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1

# Middle ground — more capable, still fits in RAM
# LLM_MODEL=ollama_chat/qwen2.5:32b

# Cloud — best quality, requires API key
# LLM_MODEL=claude-sonnet-4-5
# ANTHROPIC_API_KEY=sk-ant-...

# OpenAI
# LLM_MODEL=gpt-4o
# OPENAI_API_KEY=sk-...

# Embeddings — shared across all LLM backends
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

> ⚠️ **M1 Max 64GB note**: `llama3.3:70b` (~43GB) crashes Ollama at runtime due to memory pressure when running alongside the embedding model and macOS. `qwen2.5:14b` is the verified default. `qwen2.5:32b` is a reasonable step up if you close other applications first.

---

## Key Design Decisions

**Why `ollama_chat/` prefix?**  
The `ollama/` prefix routes to `/api/generate` (completion API) which does not support tool/function calling. The `ollama_chat/` prefix routes to `/api/chat` which does. Tool calling is central to the agent loop — using the wrong prefix means tools are silently ignored.

**Why LiteLLM instead of direct SDK calls?**  
Single interface for all LLM backends. Swap from local Qwen to Claude to GPT-4 via one env var. No code changes, no vendor lock-in. The `_litellm_kwargs()` helper handles backend-specific quirks (like the Ollama `api_base` requirement) in one place.

**Why MCP instead of the v4 service registry?**  
MCP is the emerging standard. New tools added via config, not code. Works with Cursor, Claude Desktop, and any MCP-compatible client — tooling investment compounds over time.

**Why is multi-user implemented as context injection?**  
Simpler to reason about, simpler to test, simpler to debug. You can read exactly what the LLM is being told. The v4 approach wove permissions through every layer, making it hard to trace why something was or wasn't accessible. Prompt-based scoping is also easy to audit — you can log the full system prompt per session if needed.

**Why no ServiceOrchestrator?**  
The LLM is the orchestrator. This is what GPT-4 function calling, Claude tool use, and every major agent framework has converged on. Hand-written routing rules are brittle and require constant maintenance as capabilities grow. The LLM routes to the right tool based on semantic understanding of the query.

---

## Migration Reference from v4

| v5 Component | Port From (v4) |
|-------------|----------------|
| `app/rag/retriever.py` | `app/services/library_manager/service.py` + `config/library_manager.yaml` |
| `app/mcp/client.py` | `app/services/mcp_client/python_refactoring_service.py` |
| `app/mcp/servers/calendar.py` | `app/services/calendar/service.py` + `providers.py` |
| `app/mcp/servers/web_research.py` | `app/services/web_research/service.py` |
| `app/mcp/servers/rss.py` | `app/services/rss/service.py` |
| `app/auth/` | `app/auth/` (mostly unchanged) |
| `app/db/models.py` | `app/db/models.py` (add `persona`, `scope` fields) |
| Frontend | `frontend/` (update API URLs and auth handling) |

---

## Cursor Usage Notes

- **Start each sprint** by reading the sprint section and identifying files to create vs. port
- **Use `@codebase`** to reference v4 source files when porting service logic
- **Pre-sprint tech debt** (top of Phase 2) — do this first, it's small and clears the way
- **MCP client**: the Docker stdio transport in v4 is production-proven — port it directly
- **Agent loop**: `app/agent/core.py` is the most sensitive file. Any changes should preserve the `_normalize_tool_calls()` and `message.tool_calls` check patterns — these fix real Ollama bugs
- **One sprint at a time**: resist building Phase 3 features during Phase 2
- **Test with documents**: the RAG path only activates after ingesting at least one document; always have a test PDF ready during agent development

---

*FruitcakeAI v5 — Simpler. Smarter. Still private.* 🍰  
*Phase 1 complete March 2026 — Phase 2 starting*
