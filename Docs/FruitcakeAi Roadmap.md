# 🍰 FruitcakeAI Roadmap

**Version**: 5.6  
**Status**: Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅ · Phase 4 ✅ · Phase 5.1 ✅ · Phase 5.2 ✅ · Phase 5.3 ✅ · Phase 5.4 ✅ · Phase 5.5 ✅ · Phase 5.6 ✅  
**Philosophy**: Trust · Privacy · Continuity. Local-first, cloud-optional, resilient by construction.  
**Build Location**: `fruitcake_v5/`  
**Last Updated**: March 18, 2026  
**Checkpoint Note**: North Star direction is now the decision filter; Phase 6 remains gated behind reliability soak and measured need for cloud judgment.

---

## Executive Summary

FruitcakeAI is built around three outcomes: **Trust**, **Privacy**, and **Continuity**.

- **Trust**: multi-user, role-aware, safe-by-default operation with approval gates for irreversible actions.
- **Privacy**: local-first baseline where cloud use is opt-in, not assumed.
- **Continuity**: resilient operation under degraded conditions with graceful fallback and freshness-aware behavior.

The current rebuild preserves the best ideas from v3/v4 — hybrid RAG retrieval, multi-user/persona support, MCP tool integration — while discarding orchestration complexity that made earlier versions cumbersome.

The core mental model evolution:

> **v3/v4**: A platform that contains an AI  
> **v5**: An AI agent that has tools  
> **v5 Phase 4+**: An AI agent that knows its people and acts without being prompted

### What Makes FruitcakeAI Different From OpenClaw

OpenClaw is optimized for a single power user who wants maximum connectivity and tool surface. FruitcakeAI optimizes for something different: a trusted, private, multi-user system that genuinely knows the people it serves — and gets better at knowing them over time.

| Dimension | OpenClaw | FruitcakeAI |
|-----------|----------|-------------|
| Users | Single power user | Family / small team, multi-user |
| Memory | Flat MEMORY.md + HEARTBEAT.md | Persistent per-user memory in pgvector |
| Heartbeat context | Reads a markdown file | Semantically retrieves what's been relevant for this person lately |
| RAG | SQLite-vec + FTS5 | pgvector + BM25 + RRF fusion + reranking |
| Document library | Flat workspace files | Full ingest pipeline, per-user scoping |
| Safety | Single-user, no controls | Persona-scoped tools, kids safety, role-based access |
| Security | Cloud-first | Air-gapped by default, cloud opt-in per signal type |
| Mobile | Telegram dependency | Native Swift, APNs, on-device FoundationModels fallback |

The memory system is the core differentiator. OpenClaw's heartbeat knows what's in your checklist. FruitcakeAI's heartbeat knows *you*.

---

## Guiding Direction

- **Privacy-first, cloud-optional**: local inference is the default operating mode.
- **Resilience by construction**: degraded connectivity should reduce freshness before it breaks functionality.
- **Memory-first intelligence**: per-user memory context is core behavior, not an add-on.
- **Role-aware safety**: persona-scoped tools and approval gates are design constraints.
- **Modular MCP expansion**: integrations are additive and replaceable.

Boundary rule:
- FruitcakeAI core must function fully without optional providers.
- Optional integrations may extend capability, but are never load-bearing.

What FruitcakeAI is / is not:
- **Is**: a local-first assistant for individuals, households, and small teams.
- **Is not**: a cloud-first assistant with a local mode.
- **Is not**: a prescribed single configuration; deployments can remain minimal or extend via MCP.

### Design References
- `Docs/FruitcakeAI_NorthStar.md`
- `Docs/DesignPhilosophy.md`

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              Swift Client                   │
│  Chat · Library · Inbox (Ph4) · Settings    │
│           Memories (in Settings)            │
└─────────────────────┬───────────────────────┘
                      │ WebSocket / REST / APNs
┌─────────────────────▼───────────────────────┐
│           FastAPI — Thin Layer              │
│   Auth (JWT) · File Upload · Chat API       │
│   User/Session · Task API (Ph4)             │
│   Memory API (Ph4) · Webhook API (Ph5)      │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│              Agent Core                     │
│   LiteLLM (model-agnostic)                  │
│   System prompt = user context + persona    │
│     + standing memories (semantic/proc)     │
│   Tool-calling drives all orchestration     │
│   Mode-aware turn limits: chat=8 task=16    │
└──────┬──────────┬──────────┬────────────────┘
       │          │          │
┌──────▼──┐ ┌─────▼───┐ ┌───▼────────────────┐
│   RAG   │ │Calendar │ │  Web / RSS / etc    │
│pgvector │ │  MCP    │ │   MCP Servers       │
└──────┬──┘ └─────────┘ └────────────────────┘
       │
┌──────▼────────────────────────────────────────────────┐
│   PostgreSQL + pgvector                               │
│   Documents · Sessions · Memories · Tasks (Ph4)       │
│   APScheduler in-process (Ph4)                        │
└───────────────────────────────────────────────────────┘
```

---

## ⚠️ Ground Truth: Verified Working Configuration

### Hardware
- **Machine**: M1 Max, 64GB RAM (macOS)
- **Verified LLM**: `qwen2.5:14b` via Ollama ✅
- **`llama3.3:70b`** (~43GB): crashes Ollama — memory pressure with embedding model + macOS overhead
- **`qwen2.5:32b`** (~20GB): viable step-up if other apps closed first

### LiteLLM / Ollama Critical Patterns

```env
LLM_MODEL=ollama_chat/qwen2.5:14b   # ✅ /api/chat — tool calling works
# LLM_MODEL=ollama/qwen2.5:14b     # ❌ /api/generate — tool calls silently broken
```

```python
# Always pass api_base explicitly — strip trailing /v1
def _litellm_kwargs(self) -> dict:
    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return {"api_base": base, "model": settings.llm_model}

# Check message.tool_calls — not finish_reason
while message.tool_calls:   # ✅ Ollama returns stop even with tool calls present
    ...

# _normalize_tool_calls() required — Ollama format inconsistent across model versions
```

---

## Project Structure

```
fruitcake_v5/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── auth/
│   ├── agent/
│   │   ├── core.py                  # Agent loop — mode-aware turn limits
│   │   ├── context.py               # UserContext builder + memory injection
│   │   ├── tools.py                 # Tool registry + create_memory tool (Ph4)
│   │   └── prompts.py
│   ├── memory/                      # Phase 4 — new module
│   │   ├── service.py               # MemoryService — create, retrieve, prune
│   │   └── extractor.py             # Nightly session extraction job
│   ├── autonomy/                    # Phase 4 — new module
│   │   ├── heartbeat.py             # Heartbeat runner
│   │   ├── runner.py                # TaskRunner — isolated agent execution
│   │   ├── scheduler.py             # APScheduler (in-process, persists to PG)
│   │   └── push.py                  # APNs delivery via httpx HTTP/2
│   ├── rag/
│   ├── mcp/
│   ├── api/
│   │   ├── chat.py
│   │   ├── library.py
│   │   ├── tasks.py                 # Phase 4 — task CRUD + approval
│   │   ├── devices.py               # Phase 4 — APNs token registration
│   │   ├── memories.py              # Phase 4 — memory CRUD for Swift UI
│   │   ├── webhooks.py              # Phase 5
│   │   └── admin.py
│   └── db/
│       ├── models.py                # + Memory, Task, DeviceToken (Ph4)
│       ├── session.py
│       └── migrations/
├── config/
│   ├── mcp_config.yaml
│   ├── personas.yaml
│   ├── users.yaml
│   └── heartbeat.yaml               # Phase 4 — per-checklist config
├── tests/
├── scripts/
│   ├── start.sh
│   └── reset.sh
├── docker-compose.yml
└── .env / .env.example
```

---

## Completed Work

### Phase 1 ✅ — Agent Core + RAG Foundation
Agent loop, LiteLLM integration, pgvector RAG, hybrid BM25+vector+RRF retrieval, basic auth, PostgreSQL, document ingestion pipeline.

### Phase 2 ✅ — MCP Tools + Multi-User Polish
Calendar MCP, web research MCP, RSS MCP, persona system, library scoping (personal/family/shared), multi-user API, pre-sprint tech debt resolved.

### Phase 3 ✅ — Frontend + Production Stability
Swift client (chat, library, settings), WebSocket dual-auth, FoundationModels on-device fallback, health check fix (`/api/tags`), one-command startup with Ollama auto-start.

### Sprint 3.7 ✅ — Library Management GUI
Local filename filter, semantic search, scope editing, status polling, shared scope, `summarize_document` hallucination fix, `PATCH /library/documents/{id}`, FK constraint fix on session delete.

---

## Memory Architecture

Memory is FruitcakeAI's primary differentiator. OpenClaw reads a flat HEARTBEAT.md checklist every 30 minutes. FruitcakeAI retrieves semantically relevant context about *this specific person* before every heartbeat and task run. The assistant doesn't just check a list — it reasons in light of what it knows.

### Memory Types

Three distinct types with different retrieval and lifecycle behavior:

| Type | What it stores | Lifecycle | Retrieval |
|------|---------------|-----------|-----------|
| `episodic` | Events, facts with time context, things that happened | Expires (time-bound) | Semantic similarity + recency |
| `semantic` | Persistent facts about the person's life, preferences, relationships | Never expires unless changed | Always included (small set) |
| `procedural` | How to behave with this person | Never expires | Always injected into system prompt |

**Examples:**
- *"Sarah's mom had surgery this week"* → episodic, importance 0.9, expires in 30 days
- *"James prefers conservative financial options first"* → semantic, importance 0.7
- *"Always use bullet points for Sarah's task summaries"* → procedural, importance 0.8
- *"Dentist appointment confirmed for Thursday 2pm"* → episodic, importance 0.85, expires in 7 days

### DB Model

```python
class Memory(Base):
    __tablename__ = "memories"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    
    # Content
    content: Mapped[str]
    memory_type: Mapped[str]              # "episodic" | "semantic" | "procedural"
    
    # Source tracking — full audit trail
    source: Mapped[str]                   # "agent" | "task" | "explicit" | "extracted"
    source_session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"))
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    
    # Retrieval
    embedding: Mapped[Vector(384)]        # same dim as existing BAAI/bge-small-en-v1.5
    
    # Relevance management
    importance: Mapped[float] = mapped_column(default=0.5)    # 0.0–1.0, agent-set
    confidence: Mapped[float] = mapped_column(default=0.8)    # how certain is this
    access_count: Mapped[int] = mapped_column(default=0)      # feedback loop
    last_accessed_at: Mapped[datetime | None]
    
    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]    # None = never expires
    is_active: Mapped[bool] = mapped_column(default=True)
    
    # Optional
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=[])
```

**Design notes:**
- Memories are **immutable** — never edited, only deactivated. If something changes ("Sarah was promoted"), the agent creates a new memory and marks the old one `is_active=False`. Audit trail preserved.
- `access_count` + `last_accessed_at` create a natural feedback loop. Frequently retrieved memories are demonstrably useful; rarely accessed ones are candidates for pruning. ⚠️ **The loop must be closed explicitly**: high `access_count` should raise `importance`; zero accesses over 30 days should flag as a pruning candidate. The `MemoryService` pruning job (Phase 8) handles bulk cleanup, but a lightweight inline adjustment on `_record_access()` — e.g. `importance = min(1.0, importance + 0.02)` per access — keeps scores meaningful before then. Without this, importance values drift stale and the ranking signal degrades.
- `expires_at` is agent-settable. The `create_memory` tool accepts `expires_in_days`. Time-bound information (appointments, deadlines, temporary situations) should always expire.
- Embedding uses the same BAAI/bge-small-en-v1.5 model already running for document RAG. No new model required.
- ⚠️ **Memory scope is per-user only in Phase 4.** Family-level facts ("the Johnsons go camping every August") are not addressable — each user's heartbeat would rediscover them independently. A future `scope` field (`"personal" | "family" | "shared"`) on `Memory` would mirror the library scoping model and allow family-relevant memories to surface across all users. Deferred to Phase 5+.

### Memory Creation

**Primary path — agent tool call:**

The agent receives a `create_memory` tool alongside its existing tool set. One line in the system prompt does the rest: *"Use create_memory when you learn something important about the user that should inform future interactions."*

```python
async def create_memory(
    content: str,
    memory_type: Literal["episodic", "semantic", "procedural"],
    importance: float = 0.5,
    expires_in_days: int | None = None,
    tags: list[str] = []
) -> str:
    """
    Store something worth remembering about the current user.

    episodic: events, facts with time context, things that happened.
    semantic: persistent facts — preferences, relationships, standing info.
    procedural: how to behave with this user in future interactions.

    Set expires_in_days for time-bound info (appointments, deadlines, current situations).
    Set importance 0.8+ for things that should surface in future heartbeats.
    """
```

⚠️ **Write-time deduplication is required.** The nightly extraction job deduplicates before inserting, but the live `create_memory` agent path does not. A user who mentions their bullet-point preference in three sessions will accumulate three near-identical procedural memories — all three will be retrieved, wasting token budget and degrading ranking. `MemoryService.create()` must run a similarity check before inserting:

```python
# In MemoryService.create() — before db.add(memory)
embedding = await embed(content)
duplicate = await db.execute(
    select(Memory)
    .where(Memory.user_id == user_id, Memory.is_active == True,
           Memory.embedding.cosine_distance(embedding) < 0.12)
    .limit(1)
)
if duplicate.scalar_one_or_none():
    return "Memory already exists (duplicate suppressed)"
```

The 0.12 cosine distance threshold keeps near-verbatim duplicates out while allowing genuinely distinct updates. Tune after real usage data.

**Secondary path — nightly extraction job:**

After Phase 4 ships and real data accumulates, a nightly background task reviews the previous 24 hours of chat sessions and extracts memories the agent didn't explicitly create. This catches things the agent noted but didn't persist — patterns that only become obvious in retrospect.

The extraction prompt is simple: *"Review this conversation. Extract any facts about the user that would be useful to remember in future sessions. Format as JSON list of {content, type, importance, expires_in_days}."*

**Explicit path — user-created:**

Via the Memories UI in Swift Settings. Users can write memories directly, edit importance, delete memories. Full control over what the assistant knows.

### Memory Retrieval

`app/memory/service.py` — called before every heartbeat and task run:

```python
class MemoryService:

    async def retrieve_for_context(
        self,
        user_id: int,
        query: str,
        max_tokens: int = 400
    ) -> MemoryContext:
        
        # Tier 1: Always include — semantic + procedural (standing facts)
        # These are small, high-value, always relevant to this person
        standing = await self._get_standing_memories(user_id)
        
        # Tier 2: Recent high-importance episodic (last 7 days, importance ≥ 0.6)
        recent = await self._get_recent_episodic(
            user_id, days=7, min_importance=0.6
        )
        
        # Tier 3: Semantically similar episodic memories
        # "check calendar for conflicts" → retrieves past calendar-related memories
        similar = await self._get_similar(
            user_id=user_id,
            query=query,
            memory_types=["episodic"],
            limit=5
        )
        
        # Deduplicate, rank by (similarity × importance × recency), truncate to budget
        merged = self._rank_and_truncate(
            standing + recent + similar,
            max_tokens=max_tokens
        )
        
        await self._record_access(merged)
        return MemoryContext(memories=merged)

    async def _get_similar(self, user_id, query, memory_types, limit):
        embedding = await embed(query)
        return await db.execute(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.memory_type.in_(memory_types),
                Memory.is_active == True,
                or_(Memory.expires_at > datetime.utcnow(), Memory.expires_at.is_(None))
            )
            .order_by(Memory.embedding.cosine_distance(embedding))
            .limit(limit)
        )
```

### What the Heartbeat Prompt Looks Like

```
[Heartbeat for Sarah — Tuesday 8:15am ET]

What I know about Sarah:
• Always use bullet points for her summaries [procedural]
• Prefers to be contacted in the morning [semantic]
• Mom had surgery this week — check in if appropriate [episodic, imp: 0.9]
• Thompson project deadline is Friday [episodic, imp: 0.85]
• Asked about rescheduling Tuesday 2pm meeting yesterday [episodic, imp: 0.7]

Checklist:
- Check calendar for conflicts in next 24 hours
- Review any pending task approvals

Current time: Tuesday 8:15am (within active hours 7am–10pm ✓)

If nothing needs attention, reply HEARTBEAT_OK.
```

The agent has genuine personal context before making any tool calls. It knows about the surgery, knows about the deadline, knows about the meeting. It doesn't need to rediscover these things — it reasons from them immediately.

### Memory Management API + Swift UI

```
GET    /memories              list user's active memories (paginated, filterable by type)
POST   /memories              create memory explicitly
PATCH  /memories/{id}         update importance, tags, or deactivate
DELETE /memories/{id}         deactivate (soft delete — audit trail preserved)
```

**Swift — Memories section in Settings:**
- List grouped by type (Procedural · Semantic · Episodic)
- Each row: content + type badge + importance dot + age
- Swipe to delete
- Tap to view source ("From conversation on March 3" / "From task: Morning briefing")
- Search/filter
- This is the answer to "why did it mention my dentist appointment?" — transparent and auditable

---

## Phase 4 — Memory + Heartbeat + Autonomous Tasks (~3 weeks)

**Goal**: FruitcakeAI acts without being prompted — and does so with genuine knowledge of each person it serves.

### What This Phase Borrows From OpenClaw (Proven)

- **LLM-as-judgment-router**: no pre-built context aggregator. The agent calls its normal tools to gather what it needs. The instruction *is* the context directive.
- **HEARTBEAT_OK suppression**: if the agent decides nothing needs attention, return the token and suppress delivery silently. Zero noise to the user.
- **Isolated sessions for tasks**: task runs create their own `ChatSession` rows, hidden from chat UI, so background work never pollutes conversation history.
- **Active hours**: heartbeat skips outside configured hours. A 3am notification is a product-killing failure mode.
- **Exponential retry backoff**: a task that fails once does not fail forever. Transient errors (network, rate limit) retry with backoff; permanent errors (auth failure, config) disable immediately.
- **Session cleanup**: task sessions are pruned after 24 hours. Background work doesn't accumulate dead session rows indefinitely.

### What This Phase Adds Beyond OpenClaw

- **Per-user persistent memory** in pgvector — retrieved semantically, injected into every heartbeat and task prompt
- **`create_memory` agent tool** — agent persists what it learns during chat and task execution
- **Multi-user scope enforcement** in the task runner — tasks inherit the owning user's persona scopes; no privilege escalation
- **Approval workflow** for irreversible actions — task pauses with `waiting_approval` status, APNs push asks the user, resume on confirmation
- **Native APNs push** — not Telegram, not webhooks. Real iOS notifications via the Swift client

### What This Phase Intentionally Defers

The `JudgmentRouter` and `ContextSanitizer` from Roadmap 4 are removed entirely. They solved a problem that doesn't exist until cloud LLM routing is actually opted into. Adding them now would be dead code and added complexity. The cloud routing path (`config/autonomy.yaml`) will be added as a focused sprint when the first user asks for it. Until then, local model only — air-gapped by default, no config required.

---

### Sprint 4.1 — DB Models + Memory Foundation (Days 1–4)

> ⚠️ **This sprint is dense.** It covers: Memory + Task + DeviceToken DB models, `MemoryService` (including write-time dedup and retrieval tiers), the `create_memory` agent tool, three new API modules (`tasks.py`, `devices.py`, `memories.py`), schedule parsing, and Alembic migrations. Budget 5–6 days if the memory retrieval ranking or deduplication logic runs longer than expected. Sprint 4.2 (runner + scheduler) has a hard dependency on all of 4.1 shipping — do not start 4.2 until migrations are applied and `MemoryService.retrieve_for_context()` is tested.

**New DB models** (`app/db/models.py`):

```python
class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str]
    memory_type: Mapped[str]              # "episodic" | "semantic" | "procedural"
    source: Mapped[str]                   # "agent" | "task" | "explicit" | "extracted"
    source_session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"))
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    embedding: Mapped[Vector(384)]
    importance: Mapped[float] = mapped_column(default=0.5)
    confidence: Mapped[float] = mapped_column(default=0.8)
    access_count: Mapped[int] = mapped_column(default=0)
    last_accessed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(default=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=[])


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str]
    instruction: Mapped[str]              # natural language prompt for the agent
    task_type: Mapped[str]               # "one_shot" | "recurring"
    status: Mapped[str] = mapped_column(default="pending")
                                          # pending | running | completed | failed
                                          # cancelled | waiting_approval
    schedule: Mapped[str | None]          # cron expr | "every:30m" | ISO timestamp
    deliver: Mapped[bool] = mapped_column(default=True)
    requires_approval: Mapped[bool] = mapped_column(default=False)
    result: Mapped[str | None]
    error: Mapped[str | None]
    retry_count: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None]
    active_hours_start: Mapped[str | None]    # "08:00"
    active_hours_end: Mapped[str | None]      # "22:00"
    active_hours_tz: Mapped[str | None]       # "America/New_York"
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_run_at: Mapped[datetime | None]
    next_run_at: Mapped[datetime | None]


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token: Mapped[str] = mapped_column(unique=True)
    environment: Mapped[str] = mapped_column(default="sandbox")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

Add `is_task_session: Mapped[bool] = mapped_column(default=False)` to `ChatSession`.

**New module: `app/memory/service.py`** — `MemoryService` with `retrieve_for_context()`, `create()`, `deactivate()`, `_record_access()`.

**New agent tool** (`app/agent/tools.py`): `create_memory` — registered alongside existing tools.

**Alembic migration**: `memories`, `tasks`, `device_tokens` tables + `chat_sessions.is_task_session`.

**New API** (`app/api/tasks.py`, `app/api/devices.py`, `app/api/memories.py`):

```
# Tasks
POST   /tasks              create task
GET    /tasks              list user's tasks
GET    /tasks/{id}         detail + last result
PATCH  /tasks/{id}         update / approve / reject
DELETE /tasks/{id}         cancel
POST   /tasks/{id}/run     manual trigger (dev/testing)

# Devices
POST   /devices/register   upsert APNs device token
DELETE /devices/{token}    deregister on logout

# Memories
GET    /memories            list active memories (filterable by type)
POST   /memories            create explicitly
PATCH  /memories/{id}       update importance / tags / deactivate
DELETE /memories/{id}       soft delete (is_active = False)
```

**Schedule parsing helper**:

```python
def compute_next_run_at(schedule: str, after: datetime | None = None) -> datetime:
    """
    Three schedule formats:
    - "every:30m" / "every:1h" / "every:6h" / "every:12h" / "every:1d"
    - Standard 5-field cron expression ("0 8 * * 1-5")
    - ISO 8601 timestamp for one-shot tasks
    """
```

---

### Sprint 4.2 — Heartbeat + Task Runner (Days 5–9)

**`app/autonomy/heartbeat.py`**

```python
class HeartbeatRunner:

    async def run(self, user: User) -> HeartbeatResult:
        config = load_heartbeat_config(user)

        # Skip if checklist is empty — no wasted API calls
        if not config.has_active_items():
            return HeartbeatResult(notified=False, skipped=True)

        # Skip if outside active hours
        if not self._within_active_hours(user):
            return HeartbeatResult(notified=False, skipped=True)

        # Retrieve relevant memories — this is what makes it personal
        memory_ctx = await memory_service.retrieve_for_context(
            user_id=user.id,
            query=config.checklist_text,
            max_tokens=400
        )

        # Build prompt: memories + checklist + timestamp
        prompt = self._compose_prompt(user, config, memory_ctx)

        # Run isolated agent session (LLM gathers its own context via tools)
        result = await self._run_isolated_agent(user, prompt)

        # HEARTBEAT_OK — suppress silently
        if result.strip().startswith("HEARTBEAT_OK"):
            remaining = result[len("HEARTBEAT_OK"):].strip()
            if len(remaining) <= 300:
                return HeartbeatResult(notified=False)

        # Real output — push to user
        await self.push.send(
            user_id=user.id,
            title="FruitcakeAI",
            body=result[:200],
        )
        return HeartbeatResult(notified=True)

    def _within_active_hours(self, user: User) -> bool:
        # ⚠️ Active hours has THREE potential config sources that must resolve to one:
        #   1. heartbeat.yaml defaults.active_hours  (global fallback)
        #   2. User model fields (user.active_hours_start / end / tz)  — set via Settings UI
        #   3. Task model fields (task.active_hours_start / end / tz)  — per-task override
        #
        # Resolution order: task fields → user fields → heartbeat.yaml defaults
        # HeartbeatRunner only uses user fields (source 2), falling back to yaml (source 1).
        # TaskRunner uses task fields (source 3), falling back to user fields, then yaml.
        # The User model must expose active_hours_start/end/tz columns (Alembic migration required).
        ...

    def _compose_prompt(self, user, config, memory_ctx) -> str:
        lines = [f"[Heartbeat for {user.display_name} — {now_local(user)}]", ""]
        if memory_ctx.memories:
            lines.append("What I know about this person:")
            for m in memory_ctx.memories:
                lines.append(f"• {m.content} [{m.memory_type}]")
            lines.append("")
        lines.append("Checklist:")
        for item in config.items:
            lines.append(f"- {item.description}")
        lines.append("")
        lines.append("If nothing needs attention, reply HEARTBEAT_OK.")
        return "\n".join(lines)
```

**`config/heartbeat.yaml`** — default checklist (per-user overrides via DB in the future):

```yaml
defaults:
  active_hours:
    start: "07:00"
    end: "22:00"
    timezone: "America/New_York"

checklist:
  - id: calendar_conflicts
    description: "Check for scheduling conflicts or urgent events in the next 24 hours"
    enabled: true

  - id: pending_approvals
    description: "Check for any tasks waiting user approval"
    enabled: true

  - id: overdue_tasks
    description: "Check for any overdue recurring tasks"
    enabled: true
```

**`app/autonomy/runner.py`** — TaskRunner:

```python
APPROVAL_REQUIRED_TOOLS = {"create_calendar_event", "send_email"}

RETRY_BACKOFFS = [30, 60, 300, 900, 3600]   # seconds

class TaskRunner:

    async def execute(self, task_id: int, pre_approved: bool = False) -> None:
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if not task or task.status not in ("pending", "waiting_approval"):
                return

            # Respect active hours per task
            if not self._within_active_hours(task):
                return

            task.status = "running"
            task.last_run_at = datetime.utcnow()
            await db.commit()

        try:
            result_text = await self._run_isolated_agent(task, pre_approved)
            await self._finalize(task, status="completed", result=result_text)

            if task.deliver and result_text.strip():
                await self.push.send(
                    user_id=task.user_id,
                    title=task.title,
                    body=result_text[:200],
                    data={"task_id": task.id},
                )

        except ApprovalRequired as e:
            await self._finalize(task, status="waiting_approval", error=str(e))
            await self.push.send(
                user_id=task.user_id,
                title=f"Approval needed: {task.title}",
                body=f"Task wants to {e.tool_name}. Approve in Inbox.",
                data={"task_id": task.id, "requires_approval": True},
            )

        except TransientError as e:
            # Retry with exponential backoff — task stays alive
            backoff = RETRY_BACKOFFS[min(task.retry_count, len(RETRY_BACKOFFS) - 1)]
            task.retry_count += 1
            task.next_retry_at = datetime.utcnow() + timedelta(seconds=backoff)
            task.status = "pending"
            await db.commit()

        except Exception as e:
            # Permanent error — disable task
            await self._finalize(task, status="failed", error=str(e))

    async def _run_isolated_agent(self, task: Task, pre_approved: bool) -> str:
        user = await db.get(User, task.user_id)

        # Retrieve relevant memories for this task
        memory_ctx = await memory_service.retrieve_for_context(
            user_id=task.user_id,
            query=task.instruction,
            max_tokens=300
        )

        # Create isolated session (hidden from chat UI)
        session = await create_session_internal(
            db, user_id=task.user_id,
            title=f"[Task] {task.title}",
            is_task_session=True,
        )

        # Compose prompt with memory context + timestamp
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        memory_block = ""
        if memory_ctx.memories:
            lines = [f"• {m.content}" for m in memory_ctx.memories]
            memory_block = "Context about this person:\n" + "\n".join(lines) + "\n\n"

        prompt = f"[Task: {task.title}]\n{memory_block}{task.instruction}\n\nCurrent time: {now}"

        user_ctx = UserContext.from_user(user)
        user_ctx.session_id = session.id
        user_ctx._task_pre_approved = pre_approved
        user_ctx._task_requires_approval = task.requires_approval
        user_ctx._approval_required_tools = APPROVAL_REQUIRED_TOOLS

        response = await run_agent(
            session_id=session.id,
            user_message=prompt,
            user_context=user_ctx,
            mode="task",
        )
        return response.get("content", "")
```

**`app/agent/core.py`** — mode-aware turn limits (surgical change only):

```python
TURN_LIMITS = {
    "chat":  8,
    "task": 16,
}

async def run_agent(session_id, user_message, user_context, mode: str = "chat"):
    max_turns = TURN_LIMITS.get(mode, 8)
    ...
```

**`app/autonomy/scheduler.py`** — APScheduler wired into FastAPI lifespan:

```python
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=settings.database_url_sync)}
)

async def start_scheduler(runner: TaskRunner, push: APNsPusher) -> None:
    # Heartbeats — every 30 minutes
    scheduler.add_job(
        lambda: asyncio.create_task(_run_all_heartbeats(push)),
        trigger="interval", minutes=30, id="heartbeat",
    )
    # Task dispatcher — every minute
    scheduler.add_job(
        lambda: asyncio.create_task(_dispatch_due_tasks(runner)),
        trigger="interval", minutes=1, id="task_dispatcher",
    )
    # Session cleanup — every 6 hours (prune task sessions > 24h old)
    scheduler.add_job(
        _cleanup_task_sessions,
        trigger="interval", hours=6, id="session_cleanup",
    )
    scheduler.start()

_run_semaphore = asyncio.Semaphore(2)   # max 2 concurrent agent loops

async def _dispatch_due_tasks(runner: TaskRunner) -> None:
    async with AsyncSessionLocal() as db:
        due = await db.execute(
            select(Task).where(
                Task.status == "pending",
                Task.next_run_at <= datetime.utcnow(),
            )
        )
    for task in due.scalars():
        asyncio.create_task(_run_with_limit(runner, task.id))

async def _run_with_limit(runner, task_id):
    async with _run_semaphore:
        await runner.execute(task_id)

async def _cleanup_task_sessions():
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(ChatSession).where(
                ChatSession.is_task_session == True,
                ChatSession.created_at < cutoff,
            )
        )
        await db.commit()
```

---

### Sprint 4.3 — APNs Push Notifications (Days 10–13)

**`app/autonomy/push.py`**

```python
class APNsPusher:
    _jwt_token: str | None = None
    _jwt_expires_at: float = 0

    def _make_jwt(self) -> str:
        # Cache JWT — valid 1 hour, regenerate 60s before expiry
        now = time.time()
        if self._jwt_token and now < self._jwt_expires_at - 60:
            return self._jwt_token
        key = Path(settings.apns_auth_key_path).read_text()
        self._jwt_token = jwt.encode(
            {"iss": settings.apns_team_id, "iat": int(now)},
            key, algorithm="ES256",
            headers={"kid": settings.apns_key_id},
        )
        self._jwt_expires_at = now + 3600
        return self._jwt_token

    async def send(self, user_id: int, title: str, body: str, data: dict = {}) -> None:
        tokens = await self._get_tokens(user_id)
        for token in tokens:
            await self._deliver(token, title, body, data)

    async def _deliver(self, token: str, title: str, body: str, data: dict) -> None:
        payload = {
            "aps": {
                "alert": {"title": title, "body": body[:200]},
                "sound": "default",
                "badge": 1,
            },
            **data,
        }
        async with httpx.AsyncClient(http2=True) as client:
            resp = await client.post(
                f"{self._base_url}/3/device/{token}",
                json=payload,
                headers={
                    "authorization": f"bearer {self._make_jwt()}",
                    "apns-topic": settings.apns_bundle_id,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning("APNs delivery failed",
                            token=token[:8], status=resp.status_code)
```

**Required `.env` additions:**

```env
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=XXXXXXXXXX
APNS_AUTH_KEY_PATH=./certs/AuthKey_XXXXXXXXXX.p8
APNS_BUNDLE_ID=none.FruitcakeAi
APNS_ENVIRONMENT=sandbox          # sandbox | production
```

**Swift — APNs registration** (`FruitcakeAiApp.swift`):

```swift
UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
UIApplication.shared.registerForRemoteNotifications()

func application(_ app: UIApplication,
                 didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
    let hex = token.map { String(format: "%02x", $0) }.joined()
    Task {
        try? await api.requestVoid("/devices/register", method: "POST",
                                    body: ["token": hex, "environment": "sandbox"])
    }
}
```

---

### Sprint 4.4 — Inbox Tab + Memory UI (Days 14–21)

**Swift — Inbox tab** (`Views/Inbox/`):

```
Views/
├── Inbox/
│   ├── InboxView.swift           # Main list: pending approvals + recent task results
│   ├── TaskRow.swift             # Status badge (green/blue/red/orange/gray)
│   └── TaskCreateSheet.swift     # Create/edit task form
```

Status badge colors: `completed` → green · `running` → blue + spinner · `failed` → red · `waiting_approval` → orange + Approve/Reject buttons · `cancelled` → gray.

`TaskCreateSheet` fields: Title · Instruction (multiline) · Schedule picker (one-time / every 30m / 1h / 6h / 12h / daily / custom cron) · Active hours toggle · Push when done toggle · Require approval toggle.

**`ContentView.swift`** — add Inbox tab with approval badge:

```swift
TabView {
    Tab("Chat",    systemImage: "bubble.left.and.bubble.right") { ChatView() }
    Tab("Inbox",   systemImage: "envelope.badge") { InboxView() }
        .badge(pendingApprovalCount)
    Tab("Library", systemImage: "books.vertical") { LibraryView() }
    Tab("Settings",systemImage: "gear") { SettingsView() }
}
```

**Swift — Memories section in Settings** (`Views/Settings/MemoriesView.swift`):

- List grouped by type: Procedural → Semantic → Episodic
- Each row: content + type badge + importance dot (●●○ etc.) + age
- Swipe to delete (calls `DELETE /memories/{id}`)
- Tap → detail: full content, source attribution ("From conversation March 3" / "From task: Morning briefing"), importance slider
- Search bar filters across all types

---

## Phase 5 — Webhooks + External Triggers (1 week)

**Sprint 5.1** — Inbound webhook surface:

```python
class WebhookConfig(Base):
    __tablename__ = "webhook_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    webhook_key: Mapped[str]          # random secret — appears in POST /webhooks/{key}
    instruction: Mapped[str]           # what agent does when triggered
    active: Mapped[bool] = mapped_column(default=True)
```

```
POST   /webhooks/{key}   inbound trigger (GitHub, Zapier, IFTTT, etc.)
GET    /webhooks         list
POST   /webhooks         create
DELETE /webhooks/{id}    remove
```

**Sprint 5.2** — Gmail Pub/Sub (`app/mcp/servers/gmail.py`):

Tools: `read_email`, `send_email`, `search_emails`, `label_email`. Gmail push → Pub/Sub → `/webhooks/{key}` → agent wakes with memory context injected.

### Phase 5.3 — Completed (Task Persona Routing + Runner Stability)

Shipped outcomes:
- Removed brittle, domain-specific task-runner orchestration instructions (news mode rollback).
- Added task-level persona support:
  - `tasks.persona` column + migration
  - `/tasks` `POST`/`PATCH`/`GET`/list include persona behavior
- Added deterministic persona routing from `config/personas.yaml` via `persona_router`.
- Introduced execution profile seam:
  - `resolve_execution_profile(task, user)`
  - returns `persona`, `allowed_tools`, `blocked_tools`
- Added lazy backfill for legacy tasks with null persona at execution time.
- Hardened MCP stdio stream handling for large Playwright payloads.

Verification highlights:
- Task persona inference and explicit override behavior validated in API tests.
- Resolver integration validated in runner tests (explicit persona + lazy backfill).
- Large-page Playwright `browser_navigate` no longer fails with separator/chunk limit error.

### Phase 5.4 — Pre-Phase-6 Hardening Gate

**Goal**: Stabilize execution/tool reliability and observability before any cloud judgment routing.

#### Phase 5.4.x — Completed in This Branch (Checkpoint)

Shipped outcomes:
- Added stage-based task model routing for autonomous paths (tasks + webhooks):
  - planning model
  - execution model
  - final synthesis model
- Added configurable large-model fallback retries for qualifying non-final step failures.
- Extended agent core interface for model routing:
  - `run_agent(..., model_override=..., stage=...)`
  - `stream_agent(..., model_override=..., stage=...)`
- Added model-stage observability counters and surfaced them through metrics/admin diagnostics.
- Hardened RSS task behavior:
  - expanded fake/synthetic feed URL guardrails
  - `search_my_feeds` empty-query behavior returns recent headlines instead of hard-failing
  - `search_feeds` invalid-URL path can recover to curated feed search when user context exists

Validation checkpoint:
- Routing + regression suites passed in `fruitcake_v5`.
- RSS/MCP suites passed after hardening and fallback updates.
- Changes are additive; no API-breaking removals.

#### Sprint 5.4.1 — MCP runtime reliability
- Add per-client request serialization lock in MCP client.
- Add reconnect + one retry for EOF/broken pipe/timeout scenarios.
- Add stderr ring buffer capture for Docker MCP clients.

#### Sprint 5.4.2 — Registry and tool contract hardening
- Add deterministic duplicate-tool-name policy (no silent overrides).
- Add optional alias/prefix strategy in MCP config.
- Keep internal `web_search`/`fetch_page` as the stable primary web contract.

#### Sprint 5.4.3 — Admin observability
- Expand `/admin/tools` diagnostics with last error, connection state, and server health.
- Add `/admin/mcp/diagnostics` endpoint for targeted checks.

#### Sprint 5.4.4 — Execution profile v1 formalization
- Keep resolver persona-derived for now.
- Document extension path for future merge of:
  - persona
  - capability profile
  - user policy
  - task overrides

#### Sprint 5.4.5 — Profile-driven execution extraction
- Added task profile contract (`tasks.profile`) with runtime default resolution to `default`.
- Added profile modules under `autonomy/profiles/`:
  - `default`
  - `news_magazine`
  - resolver + shared profile interface hooks
- Refactored planner to use profile-owned planning (`plan_steps`) instead of inline magazine special-casing.
- Refactored runner to use profile hooks for:
  - run-context preparation (`prepare_run_context`)
  - prompt augmentation (`augment_prompt`)
  - effective blocked tool policy (`effective_blocked_tools`)
  - finalize validation (`validate_finalize`)
  - standardized artifact emission (`artifact_payloads`)
- Standardized profile artifact types:
  - `prepared_dataset`, `draft_output`, `final_output`, `validation_report`, `run_diagnostics`
- Added API-level profile validation for create/patch/get/list tasks:
  - allowed in this sprint: `default`, `news_magazine`
  - unknown profile returns `400`

Verification highlights:
- Profile create/patch validation covered by task API tests.
- `news_magazine` deterministic 2-step planning validated via planner/task-step tests.
- Runner/profile integration validated with artifact persistence and grounding checks.

#### Sprint 5.4.6 — Python 3.11 Upgrade + Security Cleanup
- Upgraded backend runtime baseline to Python 3.11 and pinned local default via `.python-version`.
- Updated startup/runtime docs and startup script to create/use a 3.11 virtual environment path consistently.
- Remediated blocked dependency advisories in active requirements set, including:
  - `python-multipart` >= 0.0.22
  - `nltk` >= 3.9.3
  - `filelock` >= 3.20.3
  - `pillow` >= 12.1.1
- Kept Task 48 (`news_magazine`) reliability behavior stable after dependency/runtime upgrades (fuzzy link repair + partial publish path).

Verification highlights:
- Full suite passes on Python 3.11 in branch validation.
- `pip check` reports no broken requirements.
- Security audit reduced to residual advisories only; remaining items are explicitly tracked in release notes.

#### Deferred from Future Architecture (explicitly out of 5.4)
- Memory budgets (deferred post-5.4 unless prompt bloat metrics demand early pull-in).
- Layered memory semantics expansion (deferred).
- Event-driven heartbeat triggers (deferred).
- Dream-cycle consolidation (deferred).

#### Reference Inputs
- `Docs/phase_5_3_persona_routing_rollback_plan.md`
- `Docs/MCP_Modernization_Plan.md`
- `Docs/FruitcakeAI – Future Architecture Update.md`

---

## Phase 5.5 — Adaptive Chat Orchestration (Quality Parity)

**Goal**: close the quality gap between single-turn chat and task-mode execution on local models by adding optional task-like scaffolding to chat only when complexity warrants it.
**Status**: 5.5.1-5.5.7 merged to `main`.

**Why now**:
- Current task runs outperform chat on reliability because tasks use explicit planning, tool-grounding, and final synthesis.
- Chat remains intentionally lightweight, but this causes inconsistent quality for multi-part prompts on local 14B/16B models.

**Sprint 5.5.1 — Chat complexity detector**
- Add lightweight complexity scoring for chat turns (multi-part asks, high-stakes asks, tool-heavy asks).
- Route low-complexity requests through existing fast single-pass chat path.
- Route high-complexity requests to orchestrated chat path.
- Completed:
  - `classify_chat_complexity(...)` added with deterministic scoring and route decision.
  - Complex chat turns routed to chat orchestration mode; simple turns remain single-pass.

**Sprint 5.5.2 — Orchestrated chat path (non-task UX)**
- Add internal micro-plan for complex chat turns (2-3 steps max).
- Reuse existing tool + grounding patterns from task runner where safe.
- Keep response as a single chat answer (no task creation required).
- Completed:
  - Internal orchestration overlay added for complex chat turns (micro-plan + grounded synthesis rules).
  - Dedicated `chat_orchestrated` mode added (higher turn budget) while preserving normal chat UX.

**Sprint 5.5.3 — Grounding and output checks for chat**
- Add optional validation for news/research style answers:
  - link presence checks
  - invalid link rejection
  - empty-result retry policy
- Add “deep mode” switch in API/UI later (optional), defaulting to auto-routing.
- Completed:
  - Chat validation module added for research/news responses.
  - One-shot retry for missing links / invalid links / empty responses.
  - Invalid/placeholder links are stripped from final output when retry is not taken.

**Sprint 5.5.4 — Observability and controls**
- Add counters for:
  - chat turns routed to orchestrated path
  - fallback/retry rates
  - latency delta vs single-pass path
- Add kill switch env flag to disable orchestrated chat instantly.
- Completed:
  - Kill switch `chat_orchestration_kill_switch` added; complex prompts immediately degrade to simple chat mode when enabled.
  - Chat metrics expanded with retry counters, invalid-link counters, and simple vs orchestrated latency delta.
  - REST and WebSocket chat paths aligned to shared orchestration/validation behavior.

**Sprint 5.5.5 — Library grounding and RAG runtime reliability**
- Goal:
  - stop library-chat drift/hallucinated filenames,
  - prevent runtime `Invalid fusion mode: rrf` errors from breaking chat/task flows.
- Implementation:
  - add explicit `list_library_documents` tool for deterministic doc listings,
  - add `GET /library/documents/{id}` and `GET /library/documents/{id}/excerpts`,
  - enforce tool-required grounding for library list/detail/excerpt intents,
  - finalize runtime fusion fallback (auto-switch to vector-only + same-request retry),
  - expose `fusion_runtime_disabled` in health for soak diagnostics.
- Acceptance:
  - weather -> library-list sequence does not drift,
  - library list/detail answers are tool-grounded with no invented docs,
  - runtime fusion mode failures degrade gracefully (no user-facing hard failure),
  - chat/rag/library regression suites pass.
- Completed:
  - Added deterministic `list_library_documents` grounding path plus document detail/excerpt APIs.
  - Finalized fusion fallback so invalid runtime fusion modes degrade to vector-only instead of breaking chat/task requests.
  - Added scheduler/task guardrail follow-up:
    - duplicate task dispatch now uses a DB claim step so the same task is only executed once,
    - LLM-unavailable/task timeout paths now pause and requeue instead of churning long failures,
    - `/admin/health` exposes `llm_dispatch_gate` diagnostics for soak visibility.

**Sprint 5.5.6 — Memory grounding parity and relevance control**
- Goal:
  - bring normal chat up to the same baseline user-memory context already used by tasks,
  - stop passive retrieval/search from inflating memory access/relevance signals over time.
- Implementation:
  - inject memory context into normal chat before `run_agent` / `stream_agent`,
  - split retrieval from scoring in `MemoryService` so passive retrieval does not mutate access metadata,
  - add explicit recall/material-use access paths only,
  - add `POST /memories/{id}/recall`,
  - directionally unify prompt/context assembly so persona + memory + library/client context merge consistently.
- Acceptance:
  - chat and task prompts share the same baseline memory context model,
  - generic retrieval does not increment `access_count`,
  - explicit recall/material use updates access metadata once,
  - chat/task/memory regression suites pass.
- Completed:
  - Normal chat now injects baseline memory context before agent execution, using the same `MemoryService` retrieval + prompt rendering pattern as task mode.
  - Passive retrieval no longer mutates memory access metadata during chat/task context assembly.
  - Added explicit recall endpoint `POST /memories/{id}/recall`.
  - Added `last_accessed_at` tracking for deliberate recall/material-use paths.
  - Successful task runs now record deliberate memory access once per run for recalled memories used in task execution.

**Sprint 5.5.7 — Restricted-access terminology cleanup**
- Goal:
  - remove child/kids product language from user roles, personas, prompts, and user-facing docs,
  - preserve strict filtering and blocked-tool behavior under neutral capability-based naming.
- Implementation:
  - rename role `child` to `restricted`,
  - rename persona `kids_assistant` to `restricted_assistant`,
  - update seed users, prompt language, API/docs references, and persona picker labels,
  - add compatibility migration to rewrite legacy `child` / `kids_assistant` values in `users`, `tasks`, and `chat_sessions`.
- Acceptance:
  - strict content filter and blocked tools behave exactly as before,
  - clients/API surfaces return `restricted` / `restricted_assistant`,
  - legacy DB rows are rewritten safely by migration,
  - auth/agent/persona regression tests pass.
- Completed:
  - Renamed role `child` -> `restricted` and persona `kids_assistant` -> `restricted_assistant`.
  - Updated seed users, prompt language, docs, and persona picker labeling.
  - Added compatibility migration for legacy DB rows and merged the terminology cleanup into `main`.

**Acceptance criteria**
1. Complex chat prompts show measurable quality improvement without forcing heavy orchestration on simple chat.
2. Median chat latency for simple prompts stays near current baseline.
3. No API-breaking changes; feature is additive and flag-gated.
4. Implemented branch validation:
   - Chat-focused suites passed (`tests/test_chat_routing.py`, `tests/test_chat_orchestration.py`, `tests/test_chat_validation.py`, `tests/test_auth.py`).
   - Core regression subsets passed (`tests/test_agent.py`, `tests/test_task_steps.py`, `tests/test_webhooks.py`, `tests/test_scheduler_guardrail.py`).

---

## Phase 5.6 — Release Prep: Repository Realignment

**Status**: 5.6.1-5.6.10 merged to `main` through release `v0.6.8`. `5.6.13` was trialed on a branch, partially kept, and moved to deferred follow-up after heuristic chat gating regressed response quality and tool reliability.

**Goal**: align repository boundaries before Phase 6 so ownership, release flow, and open-source onboarding are clean.

Target repository layout:
- `FruitcakeAI` = backend/runtime app (current `fruitcake_v5` codebase)
- `FruitcakeAI_Client` = shared Apple client app for iOS and macOS (current `FruitcakeAi` codebase)

**Scope**
- Repo rename/move with full git history preservation.
- Remote/branch/README/docs link updates.
- CI/workflow path updates.
- Cross-repo references and setup docs cleanup.

**Out of scope (for this sprint)**
- New product features.
- Architecture changes unrelated to repo boundaries.
- Phase 6 cloud-routing implementation.

**Sprint 5.6.1 — Pre-move safety and freeze**
- Create pre-move checkpoint tags on both repos.
- Freeze feature work during the move window.
- Record rollback commands and branch protection expectations.
- Completed:
  - Created pre-realignment checkpoint tags for backend and client repos.
  - Added rollback/runbook documentation before repo realignment.

**Sprint 5.6.2 — Backend repo transition**
- Rename/move backend repo identity to `FruitcakeAI`.
- Update remotes, badges, clone URLs, and contributor docs.
- Validate backend startup, MCP health, and full test suite.
- Completed:
  - Backend repo realigned to `FruitcakeAI`.
  - Local remotes, docs, clone references, and release flow were updated and verified.

**Sprint 5.6.3 — Swift client repo transition**
- Rename/move Swift repo identity to `FruitcakeAI_Client`.
- Update project docs, build references, and CI workflows.
- Validate a supported Apple build target and API connectivity.
- Completed:
  - Swift client repo realigned to `FruitcakeAI_Client`.
  - Shared Apple-client positioning documented and build validation completed.

**Sprint 5.6.4 — Cross-repo release validation**
- Run end-to-end smoke flow (chat, task, RSS, push path).
- Confirm release tags and rollback path on both repos.
- Publish updated onboarding docs for open-source readiness.
- Completed:
  - Cross-repo remotes, tags, and rollback checkpoints were validated after rename.
  - Open-source-facing onboarding/docs cleanup was completed.

**Sprint 5.6.5 — Knowledge Skills System (Admin-managed, additive)**
- Add DB-backed `skills` records (frozen content at install time) with scope support (`shared` and `personal`).
- Add admin two-step install flow: `POST /admin/skills/preview` then `POST /admin/skills/install` (preview may fetch once from an allowlisted URL; runtime never re-fetches).
- Inject relevant skills in `UserContext.build()` with semantic gating and explicit prompt-budget caps.
- Keep skill tool grants additive but bounded: grants must intersect both persona `blocked_tools` and `resolve_execution_profile(...)` output.
- Add `/admin/skills/{id}/preview-injection` diagnostics for threshold tuning and explainability.
- Backend/admin first; defer Swift admin UI until the backend path proves useful in soak.

Guardrails locked for this sprint:
1. Query-empty behavior: do not inject all skills by default; only inject explicitly pinned/global-safe skills.
2. Install safety: URL preview fetch (if used) must enforce allowlist, timeout, and response-size limits.
3. Versioning/scope safety: avoid brittle global-name collisions by supporting update-friendly identity (versioned slug or scoped uniqueness).
4. Prompt budget: enforce per-skill and total skill token limits to prevent prompt bloat/drift.

Acceptance additions for Sprint 5.6.5:
1. Skills are additive only and cannot bypass persona or execution-profile tool restrictions.
2. Skill injection remains context-relevant and bounded by token budget.
3. Existing chat/task APIs and runner behavior remain backward compatible.
4. Admin diagnostics can explain why a skill did or did not inject for a sample query.
- Completed:
  - Added DB-backed skills with shared/personal scope, frozen install content, and admin preview/install flow.
  - Added semantic skill injection with bounded prompt budgets across chat, task, and webhook execution.
  - Added admin preview-injection diagnostics and backend-only rollout.

**Sprint 5.6.6 — Skills Lifecycle Hardening and Explainability**
- Add superseding install semantics for existing scoped slugs: installing a replacement skill creates a new active record and deactivates the prior active version instead of failing on collision.
- Add admin hard delete for specific skill records with audit-friendly version retention handled by the supersede flow, not in-place mutation.
- Harden degraded runtime selection so embedding-unavailable mode falls back to pinned-only injection rather than broad lexical matching.
- Surface active-skill attribution in chat, task, and webhook metadata plus richer task-run diagnostics for included/excluded skill decisions.
- Expand admin skill listings and diagnostics with lineage and selection-mode visibility so operators can inspect active, superseded, and degraded-mode behavior cleanly.

Acceptance additions for Sprint 5.6.6:
1. Reinstalling the same scoped slug supersedes the old active record without collision churn.
2. Admins can hard-delete individual skill records without corrupting other versions of the same skill.
3. Degraded selection mode uses pinned-only behavior and does not inject non-pinned skills by lexical accident.
4. Chat/task/webhook responses and diagnostics expose which skill slugs were active for that execution.
- Completed:
  - Added superseding reinstall semantics, admin hard delete, and pinned-only degraded selection mode.
  - Added skill attribution metadata to chat/task/webhook responses and richer run diagnostics.
  - Post-review hardening merged in `v0.6.6`:
    - direct install payloads now enforce the same slug/scope invariants as markdown preview,
    - malformed payloads return `400` instead of creating inconsistent rows,
    - DB uniqueness conflicts now resolve to clean `409` responses instead of leaking raw `500`s.

**Sprint 5.6.7 — Run Inspector**
- Add a backend-only admin inspection endpoint for single task runs so operators can view run metadata, tool traces, artifacts, and normalized diagnostics in one response.
- Keep the existing `/admin/task-runs` list endpoint lightweight; use a dedicated inspect route for joined debug payloads.
- Return inline artifacts in deterministic order, including prepared datasets, outputs, validation reports, and run diagnostics.
- Normalize common diagnostic fields such as active skills, skill injection events, refresh stats, dataset stats, and suppression events while preserving raw artifacts as source-of-truth.
- Keep v1 scoped to task runs only; defer chat and webhook inspection unless the endpoint proves useful in soak.

Acceptance additions for Sprint 5.6.7:
1. A completed task run can be inspected from one admin endpoint without cross-referencing audit logs and artifacts separately.
2. Artifact ordering and tool timelines are deterministic and stable across calls.
3. Sparse or failed runs still return useful inspection payloads with empty arrays instead of hard errors when optional data is missing.
4. Existing `/admin/task-runs` and `/admin/audit` behavior remain backward compatible.
- Completed:
  - Added `GET /admin/task-runs/{run_id}/inspect` for a one-response execution trace.
  - Inline artifacts, normalized diagnostics, and ordered tool timelines now make single-run debugging possible without cross-referencing multiple endpoints.

**Sprint 5.6.8 — Simple Chat True Streaming**
- Add true token streaming for the simple WebSocket chat path end to end.
- Keep complex/orchestrated chat buffered in this sprint.
- Preserve the current token/done/error wire contract while making simple chat feel live in the Swift client.
- Follow-up soak hardening tightens calendar mutation trust boundaries so chat does not claim success without a confirmed tool result.
- Completed:
  - Added true simple-chat token streaming over WebSocket.
  - Hardened client streaming state handling and offline fallback messaging.
  - Stopped false calendar success claims when tool execution was missing or provider status was not successful.

**Sprint 5.6.9 — Auth and Dependency Hardening**
- Targeted dependency security cleanup without changing auth APIs or JWT behavior.
- Upgrade `python-jose[cryptography]` to `3.5.0` so `pyasn1` can resolve to a safe version.
- Re-run dependency audit plus auth/retrieval regression after the update.
- Explicitly track current upstream blockers:
  - `nltk` advisories remain upstream because the latest published `nltk` release is still `3.9.3`
  - `ecdsa` advisory remains upstream because the latest published `ecdsa` release is still `0.19.1`
- Success for this sprint is reducing the actionable auth-side vulnerability set, verifying no auth regressions, and documenting any unresolved upstream advisories clearly.

**Sprint 5.6.10 — RSS Newspaper Export and Demo Polish**
- Focus the polish work on the `news_magazine` task for demo readiness.
- Normalize final magazine markdown so story blocks and `Read More` links render with reliable spacing.
- Export each successful full publish to a dated edition bundle under `storage/exports/newspapers`.
- Save `edition.pdf`, `edition.md`, and `edition.json` for each run, with run/task metadata that shows continuity over multiple days.
- Add an `edition_export` task-run artifact plus an admin PDF download endpoint (`GET /admin/task-runs/{run_id}/edition.pdf`) so exported editions are easy to inspect and demo.

**Sprint 5.6.11 — `Fruitcake News` Layout and Edition Density**
- Keep the internal profile key as `news_magazine`, but brand the rendered edition as `Fruitcake News`.
- Refactor the final pass so it builds a canonical edition first, then publishes/export renders from that finalized structure.
- Raise the effective edition density to roughly 10 to 12 linked stories when enough dataset items are available.
- Use a two-tier paper structure: featured top stories first, followed by shorter section briefs across multiple categories.
- Preserve grounded-link validation while allowing the finalized edition builder to backfill additional publishable briefs from the prepared dataset when the model draft is too sparse.

**Acceptance criteria**
1. Both repos are renamed/repositioned with history intact.
2. All documentation and remotes point to new canonical names.
3. Backend tests and iOS build checks pass after transition.
4. Rollback tags exist and are verified before Phase 6 work begins.

---

## Phase 6 Entry Criteria

North Star governance note: this direction filters decisions and prioritization; it does not by itself expand implementation scope before phase gates are met.

Phase 6 starts only when all are true:
1. MCP error rate is below agreed threshold in daily use.
2. No unresolved duplicate-tool ambiguity exists in registry.
3. Admin diagnostics can identify failing MCP server causes without reading raw logs.
4. Execution profile seam is stable in task runs.
5. At least one week of stable Phase 5.4 soak is complete.

## Phase 6 — Cloud Judgment Routing (as needed)

**Dependency**: Depends on completion of the Phase 5.4 reliability gate.

**Trigger**: A user requests it, or local judgment quality on heartbeats is demonstrably causing missed-important / false-alarm patterns in daily use.

Cloud routing remains opt-in and justified by measured local judgment gaps.

This is the `config/autonomy.yaml` per-signal-type routing system from Roadmap 4. It's deferred until real-world data shows where local judgment fails and cloud routing is worth the data exposure tradeoff.

```yaml
# config/autonomy.yaml — added when Phase 6 is built
judgment:
  default: local
  routing:
    calendar_conflicts: local
    email_urgency: cloud        # opt-in per signal type
    financial_signals: local    # never
    document_content: local     # never
  cloud:
    provider: anthropic
    model: claude-haiku-4-5
    max_context_tokens: 500     # structural sanitization — forces abstraction
    audit_log: true
```

The `ContextSanitizer` and `JudgmentRouter` classes are built in this phase, not Phase 4. They solve a problem that requires real-world data to scope correctly.

### Sprint 6.x — Client Context Integration (Apple additive, platform-neutral)

This sprint adds a platform-neutral client context layer that Apple clients can use first via App Intents, Shortcuts, and similar system entry points without making Fruitcake Apple-dependent.

Positioning rules:

- Fruitcake core remains platform-neutral and fully functional for Android, web, and non-Apple users.
- Apple is the first producer of optional client context, not the definition of the product.
- The backend contract stays generic so Android-equivalent integrations can adopt it later.

Planned scope:

- optional `client_context` support in chat-facing backend APIs
- shared prompt/context assembly that merges persona context, memory retrieval, optional client context, and existing library grounding
- memory retrieval added to normal chat so chat and tasks share the same baseline context model
- backend resolution of selected entities when possible, with graceful fallback when not

Not in this sprint:

- Apple-only backend paths
- Spotlight/Core Spotlight indexing
- Foundation Models offline fallback
- syncing Apple semantic indexes into Postgres

Reference:

- `Docs/sprint_6_x_client_context_integration.md`

---

## Phase 7 — Filesystem + Sub-Agent Spawning (2 weeks)

**Sprint 7.1** — Sandboxed filesystem MCP: `--allowed-paths /workspace`, per-user `workspace/{user_id}/`.

**Sprint 7.2** — Shell MCP: `docker run --network none`, 30s timeout, 8k output cap, explicit blocked commands list.

**Sprint 7.3** — Sub-agent spawning:

```python
async def spawn_agent(instruction: str, persona: str, timeout_seconds: int = 120):
    """Delegate to a specialist sub-agent. Child cannot escalate parent scopes."""
    child_session = create_child_session(parent=current_session, persona=persona)
    result = await run_agent(child_session, mode="task", max_turns=16)
    audit_log_child(parent=current_session, child=child_session)
    return result
```

**Sprint 7.4** — Graph Memory Foundation (MCP-informed, Fruitcake-native)

Goal: add durable relationship memory for long-horizon reasoning without adopting the MCP demo memory server as a production dependency.

Scope:
- Keep Fruitcake's existing memory stack as primary (semantic/procedural/episodic retrieval).
- Add a graph-memory layer in the same Postgres DB, user-scoped and auditable.
- Use MCP memory-server concepts (entity/relation/observation) as interface inspiration only.

Data model additions (Phase 7 candidate):
- `memory_entities` (id, user_id, name, entity_type, aliases, confidence, active, created_at, updated_at)
- `memory_relations` (id, user_id, from_entity_id, to_entity_id, relation_type, confidence, source_ref, created_at)
- `memory_observations` (id, user_id, entity_id, content, observed_at, confidence, source_ref, created_at)

Tool/API contract direction:
- `create_entities`
- `create_relations`
- `add_observations`
- `search_memory_graph`
- `open_memory_graph_nodes`

Guardrails:
- Persona-aware tool filtering via execution profile.
- Full provenance (`source_session_id`, `source_task_id`, webhook/run linkage).
- Confidence decay + conflict handling instead of silent overwrite.
- No cross-user graph joins by default.

Rollout:
1. Ship graph tables + service layer behind feature flag.
2. Add additive tool/API interfaces and admin diagnostics.
3. Run recall/grounding evals in soak before default enablement.
4. Keep cloud routing and graph memory decoupled; either can ship independently once Phase 6 gate is open.

---

## Phase 8 — Nightly Memory Extraction

**Trigger**: Phase 4 has been running for several weeks and real-world data shows what the agent misses.

A nightly background task reviews the previous 24 hours of chat sessions and extracts memories the agent didn't explicitly create via `create_memory`. This catches patterns that only become obvious in retrospect.

The extraction prompt reviews each session: *"Extract any facts about the user worth remembering. Return JSON: [{content, type, importance, expires_in_days}]."* New memories are deduplicated against existing ones before insertion.

---

## Phase 9 — Enterprise Fork

**Trigger**: Home version in stable daily use + confirmed business interest. Not speculative.

**Delta from home version**: SSO/LDAP/SAML · Teams (10–500 users) · ACL role matrix · Compliance export + retention · Docker Compose / K8s manifests · All judgment routing locked to `local` = air-gapped compliance guarantee · HIPAA/SOC 2 path · Mandatory audit logging.

**Why v5 already supports this**: persona=role mapping · library scopes→workspaces · LiteLLM model swap via env var · Memory scoped per-user already · `autonomy.yaml` all-local = one config change.

---

## Deferred Decisions and Design Notes

These items are intentionally tracked outside the sprint list because they are real design pressures, but they are not yet decision-complete enough to justify a full sprint or implementation commitment.

### Near-Term Decisions

- **Calendar mutation model**
  - Why it matters: chat can create events, but true move/update semantics are still unsupported and smaller models can mis-handle calendar identifiers.
  - Current status: calendar trust-boundary fixes stop false success claims; no first-class `update_event` or `move_event` tool exists yet.
  - Decision needed later: whether calendar writes remain create-only or expand to explicit event lookup + update semantics.
  - Earliest likely sprint: next chat/tooling reliability follow-up.

- **Run Inspector client surface**
  - Why it matters: the backend inspector is now good enough to debug task runs, but operators still need to hit the API directly.
  - Current status: `GET /admin/task-runs/{run_id}/inspect` exists and is useful in practice.
  - Decision needed later: whether to add a Swift admin/debug view or keep inspection API-only.
  - Earliest likely sprint: after more soak confirms the payload shape is stable.

- **Chat responsiveness optimization**
  - Why it matters: local chat still feels slower than direct model interaction, especially once memory, tools, and grounding are layered in.
  - Current status: a `5.6.13` branch tried heuristic tool/memory/history gating to improve perceived speed, but it regressed response quality and caused brittle tool-access failures; those changes were rolled back. The safe pieces kept from that work are stage latency instrumentation plus grounded library summary and PDF ingest fixes.
  - Decision needed later: whether to improve responsiveness through true streaming, prompt/context budgeting, or architecture changes rather than heuristic suppression of capabilities.
  - Earliest likely sprint: after the RAG restoration pass, using measured latency data instead of aggressive chat-path trimming.

- **Product positioning / tagline refresh**
  - Why it matters: the current product direction is more memory- and continuity-driven than the older preparedness-focused tagline implies.
  - Current status: README positioning has improved; public-facing tagline strategy is still unsettled.
  - Decision needed later: whether to refresh public launch language around memory, continuity, and trusted local operation.
  - Earliest likely sprint: before broader public launch/open-source promotion.

### Phase-Gated Design Notes

- **Ollama crash-state hardening**
  - Why it matters: dispatch gating handles some unavailability cases, but hard process death/model reload failure still needs deeper handling.
  - Current status: partially addressed through pause/requeue and local gating work.
  - Decision needed later: what explicit crash detection and recovery contract should exist for local LLM outages.
  - Earliest likely sprint: Phase 5 reliability follow-up.

- **Canonical `active_hours` resolution**
  - Why it matters: three-source policy resolution remains a subtle failure seam for scheduler behavior.
  - Current status: the risk is documented; behavior needs one canonical resolver and sharper tests.
  - Decision needed later: whether to centralize resolution in one policy layer and remove distributed fallback logic.
  - Earliest likely sprint: scheduler/reliability cleanup before additional autonomy complexity.

- **Sub-agent approval inheritance**
  - Why it matters: Phase 7 sub-agents become unsafe or confusing if approval and scope ceilings are not inherited cleanly.
  - Current status: roadmap principle exists (`child cannot escalate parent scopes`), but execution semantics are still open.
  - Decision needed later: exact inheritance model for approvals, tool ceilings, and audit lineage across spawned agents.
  - Earliest likely sprint: before Phase 7 implementation starts.

- **Tenant isolation model**
  - Why it matters: enterprise support gets expensive fast if tenant boundaries are bolted on late.
  - Current status: single-household/single-deployment model remains the active assumption.
  - Decision needed later: shared schema with `tenant_id`, schema-per-tenant, or deployment-per-tenant.
  - Earliest likely sprint: before Phase 9 implementation.

### Upstream / External Blockers

- **`nltk` security advisories**
  - Why it matters: the current dependency audit still flags `nltk 3.9.3`.
  - Current status: latest published `nltk` release is still `3.9.3`; no newer package release is available to clear the advisories from this stack today.
  - Decision needed later: whether to wait for upstream, patch around the dependency, or replace the dependency path in a broader retrieval-stack refresh.
  - Earliest likely sprint: next dependency/security review if upstream remains stalled.

- **`ecdsa` security advisory**
  - Why it matters: the current audit still flags `ecdsa 0.19.1` through the `python-jose` dependency chain.
  - Current status: latest published `ecdsa` release is still `0.19.1`; `python-jose 3.5.0` cleared `pyasn1` but not this residual advisory.
  - Decision needed later: whether to accept the residual risk temporarily, remove the native backend dependency path, or replace the JWT library in a later auth refresh.
  - Earliest likely sprint: next auth/security review.

---

## LLM Backend Configuration

```env
# Default — local, privacy-first, verified M1 Max 64GB
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1

# Step up (close other apps first)
# LLM_MODEL=ollama_chat/qwen2.5:32b

# Cloud — best quality, opt-in only
# LLM_MODEL=claude-sonnet-4-6
# ANTHROPIC_API_KEY=sk-ant-...

# Embeddings — shared across LLM backends and memory retrieval
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# Phase 4 APNs
APNS_KEY_ID=
APNS_TEAM_ID=
APNS_AUTH_KEY_PATH=./certs/AuthKey_XXXXXXXXXX.p8
APNS_BUNDLE_ID=none.FruitcakeAi
APNS_ENVIRONMENT=sandbox
```

---

## Key Design Decisions

**Memory is the core differentiator, not task execution.**
OpenClaw's task execution model is simple and proven — adopt it. Where FruitcakeAI earns its advantage is in what the agent knows before it makes any tool call. Persistent per-user memory in pgvector, retrieved semantically, injected into every heartbeat and task prompt. This is not something OpenClaw can replicate without a major rearchitecture.

**LLM-as-judgment-router — adopted from OpenClaw.**
No pre-built context aggregator. The instruction is the context directive. The agent uses its normal tools to gather what it needs. Less code, more flexible, proven in production at scale.

**Drop `JudgmentRouter` and `ContextSanitizer` from Roadmap 4.**
They solved a problem that doesn't exist until cloud routing is actually opted into. Dead code in Phase 4 becomes tech debt before the product ships. Build them in Phase 6 when they're needed, with real-world data to inform exactly what needs sanitizing.

**Air-gap is automatic, not configured.**
Ollama runs locally. Tasks use the local model. The only thing that leaves the machine is the push notification body. No `autonomy.yaml` needed until Phase 6. Default is correct by construction.

**HEARTBEAT_OK suppression — adopted from OpenClaw.**
If the agent decides nothing needs attention, return the token and suppress delivery. No noise, no training users to ignore notifications. The suppression threshold (300 chars) is configurable.

**Active hours — first class, not optional.**
A heartbeat that can fire at 3am is a product-killing failure mode. `active_hours` is stored per-user and enforced at the heartbeat runner level. ⚠️ Three config sources must resolve to one: `heartbeat.yaml` defaults → user-level fields → per-task override. Resolution order is task → user → yaml. The `User` model needs `active_hours_start`, `active_hours_end`, `active_hours_tz` columns (add to Alembic migration in Sprint 4.1 alongside Task and DeviceToken).

**Approval workflow for irreversible actions.**
Primary documented failure mode in the OpenClaw community. Any tool in `APPROVAL_REQUIRED_TOOLS` pauses the task in `waiting_approval`, sends a push, and waits for the user to confirm from the Inbox tab. Safe-by-default is non-negotiable for a system that acts without the user present.

**Exponential retry for transient errors.**
A task that fails once because of a network timeout should not fail forever. Transient errors retry with backoff (30s → 1m → 5m → 15m → 60m). Permanent errors (auth failures, config errors) disable immediately.

**APNs JWT caching.**
The JWT must be cached and reused for up to 1 hour, regenerated 60 seconds before expiry. Generating a new JWT per delivery will hit Apple's rate limits under load.

**Memory is immutable — no edits, only deactivation.**
If a fact changes, the agent creates a new memory and marks the old one `is_active=False`. The full history of what the assistant knew and when is preserved. This is essential for debugging ("why did it mention that?") and for trust.

---

## Phase 4 Verification Checklist

1. `POST /tasks` with `schedule: "every:1m"` + `deliver: false`
   → `next_run_at` computed + stored ✓ task hidden from `GET /chat/sessions` ✓

2. Wait 1 minute → `GET /tasks/{id}`
   → `status: "completed"` · `result` populated · `last_run_at` updated ✓

3. Create task outside active hours
   → task skipped silently ✓

4. Simulate transient error in agent loop
   → `retry_count` incremented · `next_retry_at` set · status stays `pending` ✓

5. `POST /devices/register` from Swift
   → token stored in `device_tokens` ✓

6. Create task with `deliver: true` + instruction that produces output
   → APNs push arrives on sandbox device ✓

7. Create task with `requires_approval: true` + `create_calendar_event` tool use
   → `status: "waiting_approval"` · approval push received · task in Inbox ✓

8. `PATCH /tasks/{id}` `{"approved": true}`
   → task re-runs with `pre_approved=True` · completes · status → `completed` ✓

9. `POST /memories` + verify embedding stored
   → `GET /memories` returns it · visible in Swift Settings → Memories ✓

10. Run heartbeat manually for a user with existing memories
    → memory context injected into prompt · `access_count` incremented ✓

11. Heartbeat agent returns `HEARTBEAT_OK`
    → no push sent · logged silently ✓

12. Task session cleanup job runs
    → sessions older than 24h with `is_task_session=True` removed ✓

13. `pytest tests/ -q` — existing 48 tests still pass ✓
    New tests: task CRUD · schedule parser · runner isolation · approval intercept ·
    memory CRUD · memory retrieval · heartbeat suppression · active hours

---

## Cursor Usage Notes

- **`app/memory/`** and **`app/autonomy/`** are new top-level modules — create from scratch
- **Agent core** (`core.py`) changes are surgical — preserve all existing `_normalize_tool_calls()` and `message.tool_calls` patterns
- **`create_memory` tool** goes into `app/agent/tools.py` alongside existing tools — same registry pattern
- **Memory embedding** reuses the same `BAAI/bge-small-en-v1.5` model already used for document RAG — no new model setup
- **APScheduler wires into FastAPI lifespan** — not a separate process
- **Test heartbeat manually** via `POST /tasks/{id}/run` before enabling the scheduler
- **APNs sandbox** during all development — switch to production only when submitting to App Store
- **JWT caching** in `APNsPusher` — this is not optional, Apple will rate-limit uncached JWT generation

---

*FruitcakeAI — Simpler. Smarter. Knows its people.* 🍰  
*Phases 1–3 + Sprint 3.7 complete March 2026 · 
