# 🍰 FruitcakeAI v5 — Rebuild Roadmap

**Version**: 5.0  
**Status**: Planning  
**Philosophy**: Agent-first. The AI orchestrates the tools — not the other way around.  
**Estimated Timeline**: 6 weeks to feature parity with v3, then iterative

---

## Executive Summary

FruitcakeAI v5 is a clean rebuild that preserves the best ideas from v3/v4 — hybrid RAG retrieval, multi-user/persona support, MCP tool integration — while discarding the complexity that made v3/v4 cumbersome: the ServiceOrchestrator, PolicyRouter, intent detection keyword system, and enterprise-scale infrastructure aspirations.

The core mental model shift:

> **v3/v4**: A platform that contains an AI  
> **v5**: An AI agent that has tools

Orchestration moves from hand-written rules into the LLM itself. New capabilities are added via MCP configuration, not code. Multi-user support is injected context, not an enforcement layer.

---

## What We're Keeping from v3/v4

| Component | Status | Notes |
|-----------|--------|-------|
| Hybrid RAG (BM25 + vector + RRF) | ✅ Keep | Migrate to LlamaIndex — config already designed in v4 roadmap |
| PostgreSQL + pgvector schema | ✅ Keep | Data layer stays as-is |
| MCP Docker infrastructure | ✅ Keep | Python Refactoring + Sequential Thinking MCPs reusable |
| React frontend | ✅ Keep | Wire to new thin FastAPI layer |
| Multi-user / persona concept | ✅ Keep | Reimplemented as injected agent context |
| BGE embeddings config | ✅ Keep | Reuse existing embedding setup |
| Calendar / Web Research / RSS logic | ✅ Keep | Repackage as MCP tools |

## What We're Dropping

| Component | Reason |
|-----------|--------|
| ServiceRegistry | Replaced by MCP auto-discovery |
| ServiceOrchestrator | Replaced by LLM tool-calling |
| PolicyRouter | Replaced by user context injection |
| Keyword-based intent detection | Replaced by LLM reasoning |
| Celery / RQ job queue | Premature — add back if needed |
| ELK / Loki / OpenTelemetry stack | Premature — structured logs are enough for now |
| Kubernetes / Istio / Kong plans | Wrong project scope |
| SOC 2 / enterprise compliance targets | Wrong project scope |

---

## Target Architecture

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
└──────┬──────────┬──────────┬────────────────┘
       │          │          │
┌──────▼──┐ ┌────▼────┐ ┌───▼──────────────┐
│   RAG   │ │Calendar │ │  Web / RSS / etc  │
│LlamaIndex│ │  MCP    │ │    MCP Servers    │
│pgvector │ │         │ │  (Docker, stdio)  │
└──────┬──┘ └─────────┘ └──────────────────┘
       │
┌──────▼────────────────────┐
│   PostgreSQL + pgvector   │
│   Redis (optional, Phase 2)│
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
│   │   └── servers/               # MCP server configs
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
│   └── personas.yaml              # User persona definitions
├── tests/
│   ├── test_agent.py
│   ├── test_rag.py
│   └── test_mcp.py
├── docker-compose.yml             # postgres, redis (optional), app
├── .env.example
├── requirements.txt
└── README.md
```

---

## Phase 1: Agent Core + RAG Foundation
**Duration**: 2 weeks  
**Goal**: Working end-to-end chat with document retrieval. No frontend yet — API only.  
**Success Metric**: Ask a question, get an answer grounded in an uploaded document, with correct user scoping.

### Sprint 1.1 — Project Bootstrap (Days 1-2)

**Tasks**:
- [ ] Initialize new repo `fruitcake_v5` (or branch)
- [ ] Set up FastAPI app skeleton (`app/main.py`, `app/config.py`)
- [ ] Configure `pyproject.toml` or `requirements.txt`:
  ```
  fastapi
  uvicorn[standard]
  litellm
  llama-index
  llama-index-vector-stores-postgres
  llama-index-embeddings-huggingface
  llama-index-retrievers-bm25
  sqlalchemy[asyncio]
  asyncpg
  alembic
  python-jose[cryptography]
  passlib[bcrypt]
  pydantic-settings
  httpx
  ```
- [ ] Copy existing PostgreSQL schema, update Alembic migrations
- [ ] Configure `.env.example`:
  ```env
  DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/fruitcake_v5
  JWT_SECRET_KEY=your-secret-key
  JWT_ALGORITHM=HS256
  
  # LLM Backend — pick one
  LLM_BACKEND=anthropic          # or: openai, openai_compat, local
  ANTHROPIC_API_KEY=sk-ant-...
  LLM_MODEL=claude-sonnet-4-5
  
  # Local fallback (Ollama / llama.cpp)
  LOCAL_API_BASE=http://localhost:11434/v1
  LOCAL_MODEL=qwen2.5:14b
  
  # Embeddings
  EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
  ```
- [ ] Docker Compose with postgres + pgvector:
  ```yaml
  services:
    postgres:
      image: pgvector/pgvector:pg16
      environment:
        POSTGRES_DB: fruitcake_v5
        POSTGRES_USER: fruitcake
        POSTGRES_PASSWORD: ${DB_PASSWORD}
      volumes:
        - pgdata:/var/lib/postgresql/data
      ports:
        - "5432:5432"
  ```

**Acceptance Criteria**: `docker-compose up` starts postgres; `uvicorn app.main:app` starts without errors; `/health` returns 200.

---

### Sprint 1.2 — Auth System (Days 3-4)

**Tasks**:
- [ ] Port JWT auth from v4 (`app/auth/`)
- [ ] User model with roles: `admin`, `parent`, `child`, `guest`
- [ ] `POST /auth/login` → returns JWT
- [ ] `GET /auth/me` → returns current user
- [ ] Auth dependency for FastAPI route protection
- [ ] Seed script: create default users from `config/users.yaml`

**Key File — `app/auth/jwt.py`**:
```python
from jose import jwt
from datetime import datetime, timedelta
from app.config import settings

def create_access_token(user_id: int, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")
```

**Acceptance Criteria**: Login returns token; protected routes reject unauthenticated requests; user roles stored in DB.

---

### Sprint 1.3 — LlamaIndex RAG Service (Days 5-8)

This is the heart of v5. Port your hybrid retrieval config directly.

**Tasks**:
- [ ] Create `app/rag/service.py` — LlamaIndex setup with pgvector store
- [ ] Create `app/rag/retriever.py` — hybrid BM25 + vector with RRF fusion
- [ ] Create `app/rag/ingest.py` — document ingestion pipeline
- [ ] Create `config/rag_config.yaml` (port from v4):
  ```yaml
  vector_store:
    type: postgres
    table_name: document_chunks
    embed_dim: 384

  embedding:
    model_name: BAAI/bge-small-en-v1.5
    batch_size: 32

  retrieval:
    vector_top_k: 40
    bm25_top_k: 40
    fusion: rrf
    similarity_cutoff: 0.28
    rerank_top_n: 10
    rerank_model: cross-encoder/ms-marco-MiniLM-L-6-v2

  chunking:
    chunk_size: 900
    chunk_overlap: 120
    strategy: semantic_then_fallback
  ```

**Key File — `app/rag/retriever.py`**:
```python
from llama_index.core.retrievers import VectorIndexRetriever, BM25Retriever
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.core.postprocessor import SentenceTransformerRerank
from llama_index.core.response_synthesizers import get_response_synthesizer

def build_hybrid_retriever(index, config):
    vector_retriever = VectorIndexRetriever(
        index=index,
        similarity_top_k=config.retrieval.vector_top_k
    )
    bm25_retriever = BM25Retriever.from_defaults(
        index=index,
        similarity_top_k=config.retrieval.bm25_top_k
    )
    reranker = SentenceTransformerRerank(
        top_n=config.retrieval.rerank_top_n,
        model=config.retrieval.rerank_model
    )
    # Reciprocal Rank Fusion
    from llama_index.core.retrievers import QueryFusionRetriever
    fusion_retriever = QueryFusionRetriever(
        retrievers=[vector_retriever, bm25_retriever],
        similarity_top_k=config.retrieval.rerank_top_n,
        num_queries=1,
        mode="reciprocal_rerank",
    )
    return fusion_retriever, reranker
```

- [ ] Library API endpoints:
  - `POST /library/ingest` — upload + chunk + embed document
  - `GET /library/query?q=...` — semantic search, returns citations
  - `GET /library/documents` — list user's documents
  - `DELETE /library/documents/{id}` — remove document

**Acceptance Criteria**: Upload a PDF; query returns relevant chunks with source citations; user A cannot see user B's documents.

---

### Sprint 1.4 — Agent Core (Days 9-14)

**Tasks**:
- [ ] Create `app/agent/core.py` — main agent loop using LiteLLM
- [ ] Create `app/agent/context.py` — builds system prompt from user context
- [ ] Create `app/agent/tools.py` — tool registry, dispatches to RAG or MCP
- [ ] Wire RAG as first tool: `search_library(query, user_id)`
- [ ] Chat API: `POST /chat/sessions`, `POST /chat/messages`
- [ ] Basic WebSocket support for streaming responses

**Key File — `app/agent/context.py`**:
```python
from dataclasses import dataclass
from typing import List

@dataclass
class UserContext:
    user_id: int
    username: str
    role: str                       # admin | parent | child | guest
    persona: str                    # e.g. "family_assistant"
    library_scopes: List[str]       # which doc collections user can access
    calendar_access: List[str]      # which calendars user can see

    def to_system_prompt(self) -> str:
        return f"""You are FruitcakeAI, a private, local-first AI assistant for the {self.username} household.

Current user: {self.username} (role: {self.role})
Persona: {self.persona}

You have access to the following tools:
- search_library: Search the family document library
- (more tools added as MCP servers come online)

Always cite sources when using library search results.
Only access documents and calendars within the user's permitted scopes: {self.library_scopes}
Be helpful, concise, and privacy-conscious."""
```

**Key File — `app/agent/core.py`**:
```python
import litellm
from app.agent.tools import get_tools_for_user
from app.agent.context import UserContext

async def run_agent(
    messages: list,
    user_context: UserContext,
    stream: bool = True
):
    system_prompt = user_context.to_system_prompt()
    tools = get_tools_for_user(user_context)

    response = await litellm.acompletion(
        model=settings.llm_model,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        tools=tools,
        stream=stream
    )

    # Handle tool calls in a loop until final response
    while response.choices[0].finish_reason == "tool_calls":
        tool_results = await dispatch_tool_calls(response, user_context)
        messages = messages + [response.choices[0].message] + tool_results
        response = await litellm.acompletion(
            model=settings.llm_model,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            tools=tools,
            stream=stream
        )

    return response
```

**Acceptance Criteria**: Send a chat message; agent calls `search_library` when relevant; response includes citations; conversation history maintained per session.

---

## Phase 2: MCP Tools + Multi-User Polish
**Duration**: 2 weeks  
**Goal**: All v3 service capabilities restored as MCP tools. Multi-user context fully working.  
**Success Metric**: Different family members get appropriately scoped responses; calendar/web/RSS tools work.

### Sprint 2.1 — MCP Infrastructure (Days 1-3)

Port Docker-based MCP client from v4 — this work is already done.

**Tasks**:
- [ ] Copy `app/services/mcp_client/` from v4 → `app/mcp/client.py`
- [ ] Create `app/mcp/registry.py` — auto-discovery from `config/mcp_config.yaml`
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
- [ ] Register all MCP tools in `app/agent/tools.py` — agent gets full tool list at session start
- [ ] Tool schema auto-generated from MCP server metadata (no hard-coding)

**Acceptance Criteria**: `GET /admin/tools` lists all registered tools; adding a new MCP server to config makes it available without code changes.

---

### Sprint 2.2 — Calendar, Web Research, RSS Tools (Days 4-8)

Port service logic from v4, repackage as clean MCP tool wrappers.

**Tasks**:
- [ ] `app/mcp/servers/calendar.py`:
  - Tools: `list_events(start, end, calendar_id)`, `create_event(...)`, `search_events(query)`
  - Providers: Google Calendar, Apple Calendar (port from v4)
  - User scoping: respect `user_context.calendar_access`
- [ ] `app/mcp/servers/web_research.py`:
  - Tools: `web_search(query, num_results)`, `fetch_page(url)`
  - Providers: Brave, DuckDuckGo, NewsAPI (port from v4)
- [ ] `app/mcp/servers/rss.py`:
  - Tools: `get_feed_items(feed_url, limit)`, `search_feeds(query)`
  - Bias analysis optional — add back in Phase 3 if wanted

**Acceptance Criteria**: Ask "what's on the family calendar this week?" → agent calls `list_events`, returns formatted schedule.

---

### Sprint 2.3 — Persona System (Days 9-11)

Simple but powerful — replaces the PolicyRouter entirely.

**Tasks**:
- [ ] Create `config/personas.yaml`:
  ```yaml
  personas:
    family_assistant:
      description: General family assistant with access to all shared resources
      tone: friendly and helpful
      library_scopes: [family_docs, recipes, household]
      
    kids_assistant:
      description: Safe, age-appropriate assistant for children
      tone: encouraging and simple
      library_scopes: [kids_books, homework]
      content_filter: strict
      blocked_tools: [web_research]  # no open web for kids
      
    work_assistant:
      description: Focused on professional tasks
      tone: professional and concise
      library_scopes: [work_docs, projects]
  ```
- [ ] Assign default persona per user role in `config/users.yaml`
- [ ] Allow users to switch persona via chat command: `/persona work_assistant`
- [ ] Persona definition injected into system prompt via `UserContext`
- [ ] `blocked_tools` in persona config filters tool list at session start

**Acceptance Criteria**: Child user cannot access web research tool; switching persona changes tone and scope; persona selection persists across sessions.

---

### Sprint 2.4 — Multi-User Polish (Days 12-14)

**Tasks**:
- [ ] User management API: `GET /admin/users`, `POST /admin/users`, `PATCH /admin/users/{id}`
- [ ] Document ownership: every document tagged with `owner_id` and `scope` (personal | family | shared)
- [ ] Family "shared library" concept: documents any family member can query
- [ ] Audit log: every agent tool call logged with `user_id`, `tool`, `timestamp`
- [ ] `GET /admin/audit` endpoint for reviewing activity

**Acceptance Criteria**: Admin can see all users and activity; shared documents visible to all; personal documents visible only to owner.

---

## Phase 3: Frontend + Production Ready
**Duration**: 2 weeks  
**Goal**: Existing React frontend wired to v5 API. System stable enough for daily family use.  
**Success Metric**: Full daily use by real family members without intervention.

### Sprint 3.1 — Frontend API Migration (Days 1-5)

**Tasks**:
- [ ] Audit all API calls in existing React frontend
- [ ] Update base URL and auth token handling
- [ ] Update chat interface to handle streaming WebSocket responses
- [ ] Tool call visualization: show when agent is searching library, checking calendar, etc.
- [ ] Document upload UI connects to `POST /library/ingest`
- [ ] Persona switcher in UI sidebar
- [ ] User management panel (admin only)

**Key frontend changes** (likely minimal — mostly URL updates):
```typescript
// Before (v4)
const API_BASE = 'http://localhost:8000/api'

// After (v5 — same port, same structure)  
const API_BASE = 'http://localhost:8000/api'

// Auth header handling stays identical
// Chat API endpoint paths should match v4 where possible
```

**Acceptance Criteria**: Frontend connects to v5 API; chat works end-to-end; document upload works; no v4 API dependencies remain.

---

### Sprint 3.2 — Stability & Developer Experience (Days 6-10)

**Tasks**:
- [ ] Health check endpoint: `GET /health` returns status of all dependencies
- [ ] Simple metrics: `GET /admin/metrics` returns token counts, latency p50/p95, error rates (no Prometheus needed yet)
- [ ] Structured JSON logging with `trace_id` per request
- [ ] Graceful startup: wait for DB and embedding model before accepting traffic
- [ ] Graceful shutdown: finish in-flight requests
- [ ] `./scripts/start.sh` — one command to start everything
- [ ] `./scripts/reset.sh` — wipe and reseed DB for development
- [ ] Error handling: agent failures return user-friendly messages, not stack traces

**Acceptance Criteria**: `./scripts/start.sh` brings up full system; `/health` shows all green; errors are caught and logged with trace IDs.

---

### Sprint 3.3 — Testing & Documentation (Days 11-14)

**Tasks**:
- [ ] `tests/test_agent.py` — agent tool-calling integration tests
- [ ] `tests/test_rag.py` — RAG retrieval quality tests (port eval harness from v4)
- [ ] `tests/test_auth.py` — auth and user scoping tests
- [ ] `tests/test_mcp.py` — MCP tool registration and execution tests
- [ ] Golden dataset for RAG evaluation (port from v4): target Recall@10 > 0.7
- [ ] `README.md` — quick start, architecture overview, adding new MCP tools
- [ ] `docs/ADDING_MCP_TOOLS.md` — step-by-step guide for extending the system
- [ ] `docs/PERSONA_SYSTEM.md` — how to configure personas and user scopes

**Acceptance Criteria**: `pytest tests/` passes; RAG quality gates met; new developer can get running in < 30 minutes following README.

---

## Future Phases (Post v5 Stable)

These are good ideas that belong *after* the system is working and being used daily. Don't build them speculatively.

| Phase | Feature | Trigger to Start |
|-------|---------|-----------------|
| 4 | Redis caching for embeddings and sessions | Noticeable latency issues in daily use |
| 4 | HNSW vector indexing | Library exceeds ~10k documents |
| 5 | Voice interface | Actively wanted by family members |
| 5 | Mobile app / PWA | Frontend usage on phones becomes primary |
| 6 | Multimodal (image/audio ingestion) | Specific use case identified |
| 6 | Email integration | Actively requested |
| 7 | Multi-household / friends network | Personal use case proven |

---

## LLM Backend Configuration

v5 supports multiple backends via LiteLLM — switch without code changes.

```env
# Cloud (best quality, requires API key)
LLM_BACKEND=anthropic
LLM_MODEL=claude-sonnet-4-5

# OpenAI
LLM_BACKEND=openai
LLM_MODEL=gpt-4o

# Local via Ollama (privacy-first, no API key)
LLM_BACKEND=ollama
LLM_MODEL=ollama/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434

# Local via llama.cpp (existing v4 setup)
LLM_BACKEND=openai_compat
LLM_MODEL=qwen2.5-14b
LOCAL_API_BASE=http://localhost:8080/v1
LOCAL_API_KEY=sk-local
```

---

## Key Design Decisions & Rationale

**Why LiteLLM instead of direct SDK calls?**  
Single interface for all LLM backends. Swap from local Qwen to Claude to GPT-4 via one env var. No code changes, no vendor lock-in.

**Why MCP instead of the v4 service registry?**  
MCP is the emerging standard. New tools added via config, not code. Works with Cursor, Claude Desktop, and any other MCP-compatible client — your tooling investment compounds.

**Why is multi-user implemented as context injection rather than middleware?**  
Simpler to reason about, simpler to test, simpler to extend. The LLM enforces scope through its prompt — and you can inspect exactly what it's being told. The v4 approach wove permissions through every layer, making it hard to trace why something was or wasn't accessible.

**Why no ServiceOrchestrator?**  
The LLM is the orchestrator. This is what GPT-4 function calling, Claude tool use, and every major agent framework has converged on. Hand-written orchestration rules are brittle and require constant maintenance as capabilities grow. The LLM routes to the right tool based on semantic understanding of the query.

**Why not Kubernetes / microservices?**  
This is a family assistant running on home hardware. Optimize for developer experience and reliability, not horizontal scale. Revisit if the use case genuinely demands it.

---

## Migration Notes from v4

When migrating specific components, reference these v4 files:

| v5 Component | Port From (v4) |
|-------------|----------------|
| `app/rag/retriever.py` | `app/services/library_manager/service.py` + `config/library_manager.yaml` |
| `app/mcp/client.py` | `app/services/mcp_client/python_refactoring_service.py` |
| `app/mcp/servers/calendar.py` | `app/services/calendar/service.py` + `providers.py` |
| `app/mcp/servers/web_research.py` | `app/services/web_research/service.py` |
| `app/mcp/servers/rss.py` | `app/services/rss/service.py` |
| `app/auth/` | `app/auth/` (mostly unchanged) |
| `app/db/models.py` | `app/db/models.py` (keep existing schema, add persona/scope fields) |
| Frontend | `frontend/` (update API URLs and auth handling) |

---

## Cursor Usage Notes

When working through this roadmap in Cursor:

- **Start each sprint** by reading the relevant sprint section and identifying files to create vs. port
- **Use `@codebase`** to reference v4 source files when porting logic
- **RAG service**: reference `config/library_manager.yaml` from v4 when building `config/rag_config.yaml`
- **MCP client**: the Docker stdio transport code in v4 is production-proven — port it directly, don't rewrite
- **Agent loop**: the tool-calling pattern in `app/agent/core.py` is the most important new file — get this right before building tools
- **One sprint at a time**: resist the urge to build Phase 2 features during Phase 1. Get end-to-end working first.

---

*FruitcakeAI v5 — Simpler. Smarter. Still private.* 🍰  
*Last Updated: February 2026*
