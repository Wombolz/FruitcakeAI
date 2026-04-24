# 🍰 FruitcakeAI Roadmap

**Version**: 6.0  
**Status**: Phase 1 ✅ · Phase 2 ✅ · Phase 3 ✅ · Phase 4 ✅ · Phase 5 ✅ · Phase 6 🚧 · Phase 7 ⏳  
**Philosophy**: Trust · Privacy · Continuity. Local-first, cloud-optional, resilient by construction.  
**Build Location**: `fruitcake_v5/`  
**Last Updated**: April 22, 2026  
**Checkpoint Note**: North Star direction remains the decision filter. Phase 6 is no longer purely deferred: targeted routing/accounting groundwork is now in `main`, while broader cloud judgment rollout and shared memory review remain gated behind measured need and product hardening.
**Phase 6 Latest Additions**: first-class per-chat model selection and user-visible reasoning controls have landed, using a backend-configured selectable model registry, one credential per provider, and no changes to the underlying Ollama setup in v1.
**Latest UI Note**: the client-side chat model selector follow-up is now closed. The Swift chat composer keeps a per-session display override and reconciles it against backend-confirmed session state, so model changes now show up immediately instead of only after refresh (`FruitcakeAI_Client` `v0.2.1`).
**Latest Capability Note**: normal chat can now create, inspect, and update persistent tasks through first-class task tools instead of only building one-shot plan scaffolds. Chat mutation validation now prevents it from claiming a task was created or updated unless the matching task tool explicitly confirmed success.
**Latest Hardening Note**: the RSS chat path has now gone through a full post-compaction repair pass. Replayed histories are tool-chain-safe, RSS recent-item payloads are deduped and headline-oriented, headline roundup prompts stay in the RSS lane and converge quickly, and follow-up article/detail prompts now validate out leaked tool narration instead of surfacing it to users.
**Immediate Security Triage Note**: the current “Agents of Chaos” review still matters, but its two headline items are no longer in the same state. Linked-folder ingestion is now constrained by `LINKED_SOURCE_ALLOWED_ROOTS` and should be treated as landed hardening. Task creation / task persistence is now approval-safe by default. The remaining trust-focused security follow-up is narrower: broaden approval-gated mutation coverage beyond the current explicit high-risk tool set and make approval-trigger visibility clearer where needed. Other findings remain important, but phaseable.
**Cross-Cutting Time Note**: time handling is still a named architectural concern, but the status is now mixed rather than wholly open. UTC storage remains the truth, canonical timezone precedence is in place, recurring scheduler behavior follows the normalized timezone path, and task APIs now expose localized companion timestamps. Remaining work is the finish pass: normalize push payloads, exports/reports, and the remaining human-facing display surfaces so they stop inventing time semantics ad hoc.
**Roadmap Structure Note**: the team intentionally pulled some trusted-local capability work forward before broader Phase 6 architecture was fully settled. As a result, Phase 6 now needs clearer subtracks, while Phase 8 and Phase 9 should be read as future-horizon work rather than near-sequential build steps.
**Direction Review Note**: use `Docs/_internal/fruitcake_product_and_runtime_direction.md` as the current synthesis document during roadmap progress reviews. Revisit it when evaluating new subtracks, major UI/runtime abstractions, or add-on/task-model expansion so the roadmap stays aligned with the emerging product language.
**Developer Loop Note**: the recent Fruitcake MCP control-surface work should be treated as enabling infrastructure, not roadmap drift. Its primary purpose is to give Codex direct inspection and bounded mutation access so the development loop tightens during the next roadmap phases. Its secondary value is that it begins to define a future admin/control-plane surface. Keep expanding it only when it materially improves testing, diagnosis, or operator visibility.

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
- `Docs/_internal/fruitcake_product_and_runtime_direction.md`

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

*Phase 6.x planning proposals and enabler notes that were previously in this section have been relocated to the Phase 6 area under "Phase 6.x Sprint Proposals And Planning Notes."*

---

## Phase 1 — Agent Core + RAG Foundation

**Status**: ✅ Complete

Core agent loop with LiteLLM model integration, PostgreSQL with pgvector, hybrid BM25+vector+RRF retrieval, basic authentication, and document ingestion pipeline. This phase established the foundational runtime and RAG architecture that all later phases build on.

---

## Phase 2 — MCP Tools + Multi-User Polish

**Status**: ✅ Complete

Calendar MCP, web research MCP, RSS MCP, persona system, library scoping (personal/family/shared), multi-user API, and pre-sprint tech debt cleanup. This phase added the first external tool integrations and multi-user identity model.

---

## Phase 3 — Frontend + Production Stability

**Status**: ✅ Complete

Swift client (chat, library, settings), WebSocket dual-auth, FoundationModels on-device fallback, health check fix, one-command startup with Ollama auto-start. Sprint 3.7 added library management GUI with semantic search, scope editing, and document management.

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

**Sprint 5.2** — Email Integration (Deferred)

Originally scoped as Gmail Pub/Sub with `read_email`, `send_email`, `search_emails`, and `label_email` tools. This was intentionally deferred — not accidentally omitted — because Fruitcake's trust and safety posture was not yet mature enough for email access and mutation at the time Phase 5 was built. The Sprint 5.2 timeslot was consumed by other Phase 5 hardening work.

Email integration is now a plausible revisit target: Fruitcake's approval gates, secret management, and tool-contract model have matured enough that scoped email access could be designed safely. Revisit when email becomes a concrete product priority.

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
- User-defined personas layered on top of stable built-in personas (deferred). Direction:
  keep system persona keys stable, allow future user-created personas to inherit from a built-in base persona,
  and support user-level overrides such as display name, tone, and selected tool restrictions without mutating
  the built-in persona definitions directly.

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

**Status**: Phase 5.6 is complete. The repo realignment and release-prep track landed, then the follow-through work that productized recurring task patterns and installability also merged to `main` through release `v0.6.14`.

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

**Post-5.6 completion follow-through now in `main`**
- Installability hardening landed:
  - bootstrap/start/doctor improvements,
  - dependency and local-model checks,
  - cleaner pre-alpha setup path.
- LLM routing/accounting groundwork landed:
  - usage event accounting,
  - hybrid OpenAI/Ollama task routing,
  - maintenance tasks pinned to the small/local task model.
- Built-in recurring profile pattern was productized:
  - `rss_newspaper` became the canonical built-in RSS profile,
  - prompt contract moved out of inline Python and into bundled spec files,
  - `morning_briefing`, `topic_watcher`, and `maintenance` were added as built-in profiles.
- RSS/Newspaper quality work landed:
  - publication-history-based freshness,
  - stronger repeat suppression,
  - title/published-field sanitation,
  - equivalent-feed dedupe,
  - export and schema hygiene cleanup.
- `topic_watcher` gained:
  - stronger prepared-context guardrails,
  - memory-candidate generation,
  - owner-scoped approval of flat-memory candidates via API.
- Public repo/docs cleanup landed:
  - internal planning docs moved out of the distributed docs tree,
  - RSS Newspaper example doc added,
  - branch-specific planning docs now live under `Docs/_internal`.

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

Current note:
- Targeted Phase 6 plumbing is already on `main`:
  - hybrid local/cloud task routing,
  - LLM usage accounting,
  - model-policy seams that let maintenance and recurring profiles stay explicit,
  - user-visible token usage inspection,
  - first routing-quality hardening for search-heavy chat requests,
  - per-chat model selection with a configured model registry,
  - user-visible reasoning effort controls in the client.
- Remaining Phase 6 work is no longer “build the plumbing,” but “decide where cloud judgment is actually justified and roll it out narrowly.”

### Phase 6 Active Subtracks

To keep Phase 6 readable and avoid turning it into a catch-all bucket, treat it as four active architectural subtracks:

1. **Routing And Judgment**
   - narrow cloud-routing policy
   - chat/task escalation quality
   - model/accounting visibility

2. **Time Semantics**
   - canonical timezone-aware scheduling
   - UTC storage with localized human-facing timestamps
   - consistent report/export timestamp policy

3. **Declarative Runtime And Context Preservation**
   - architecture-aware compaction with deliberate reinjection
   - declarative executor/task-recipe contracts
   - context visibility for skills, datasets, and executor state
   - source/trust layering for built-in, user, managed, plugin, and MCP behavior

4. **Linked Source And Codebase Awareness**
   - trusted linked-source ingestion
   - codebase-aware retrieval
   - tighter operator control over indexing boundaries

These subtracks are related, but they should not be forced into a single serialized sprint sequence.

### Recent Phase 6.x Work Now In `main`

The last few days materially advanced the Phase 6.x architecture track. These slices are already shipped groundwork in `main`:

- `v0.7.4` — declarative runtime hardening
  - step-boundary preserved runtime state
  - configured-executor prompt/contract cleanup
  - repetitive-reporting duplicate suppression and light title-cluster diversity

- `v0.7.5` — secrets hardening
  - encrypted user-owned secrets with real `SECRETS_MASTER_KEY` enforcement
  - audited secret access events
  - owner/admin metadata visibility for secret access
  - tighter service/secret matching for approved integrations

- `v0.7.6` — bounded JSON/API integration
  - backend-owned API execution substrate with deterministic `response_fields`
  - live ISS and weather proving contracts
  - normalized chat tool-argument handling for API requests

- `v0.7.7` — time semantics closure
  - canonical timezone precedence is now explicit
  - UTC remains storage/scheduler truth
  - task APIs expose localized companion timestamps
  - recurring backlog scheduling now uses the canonical timezone path

- `v0.7.8` — chat task runtime groundwork
  - recipe-backed task normalization and persisted `task_recipe` metadata
  - stronger chat task confirmation summaries
  - broader daily briefing inference coverage
  - stale profile carryover fix when task family changes

- `v0.7.15` — RSS chat convergence and replay hardening
  - replay-time tool-chain sanitization now strips orphaned assistant `tool_calls` and normalizes replayed message content for provider-safe model calls
  - RSS recent-items payloads are deduped and reformatted into a more compact headline-oriented shape
  - headline roundup prompts stay in the RSS lane, avoid repeated refresh churn, and synthesize from the first useful recent-items batch
  - full recent-items evidence now survives RSS synthesis instead of being truncated to the first few headlines
  - follow-up article/detail prompts validate more aggressively and retry instead of leaking fetch narration or compacted tool scaffolding
  - websocket cleanup and dict-style tool dispatch were hardened along the way so validated chat paths behave consistently again

These releases moved Fruitcake from “task/runtime direction is mostly planned” to “the core seams are real enough to iterate on product UX instead of only backend plumbing.”

### Phase 6.x Sprint Proposals And Planning Notes

These planning items were originally drafted during cross-phase work. They describe the motivating scope and intent behind the Phase 6.x subtracks. Implementation status for each is tracked in the "Recent Phase 6.x Work Now In `main`" section above and in the sprint sections below.

#### Time Semantics Normalization
Scope:
- fix recurring schedule computation so `active_hours_tz` is honored correctly
- introduce shared helpers for localized human-facing timestamps vs UTC storage/IDs
- define policy for report/export/publication timestamps
- prevent new task/profile work from inventing time semantics ad hoc

Reference:
- `Docs/_internal/time-semantics-normalization.md`
- `Docs/_internal/time_semantics_closure_plan.md`

#### MCP Developer Loop / Control Plane Foundation
Scope:
- expose a bounded authenticated MCP surface for direct task, library, and runtime inspection
- support direct Codex-driven validation of real Fruitcake behavior instead of relying only on source inspection and user-relayed outcomes
- add aggregate task-run health inspection and focused diagnostics where they shorten debugging loops
- keep the surface intentionally bounded; expand only when it clearly improves development or operator visibility

Current status:
- bounded MCP task/library/runtime inspection surface is live on the working branch
- aggregate task health rollups, no-artifact failure findings, and contradiction-style memory candidate signals are in place
- live Codex MCP testing is now a real part of the development loop

Intent:
- primary: improve development velocity and diagnosis quality while the roadmap continues
- secondary: establish an early control-plane shape that can later inform admin/dashboard work

Boundary:
- do not let MCP surface growth become the main workstream
- after this enabling slice, return to roadmap priorities and use MCP mainly as a support tool

Reference:
- `Docs/_internal/fruitcake_mcp_loop_note.md`
- `Docs/_internal/mcp_as_control_plane_for_dashboards_note.md`

#### Declarative Runtime And Context Preservation
Scope:
- add architecture-aware compaction that deliberately preserves high-value runtime context
- introduce a declarative behavior contract layer for configured executors and future task recipes
- formalize source/trust layering for built-in, user, managed, plugin, and MCP-provided behavior
- improve visibility into active skills, executor configuration, prepared datasets, and other injected context
- study hook and agent-capability patterns only after the declarative/runtime seams are stable

Why this phase exists:
- Fruitcake is now feeling pressure from profile sprawl, increasingly invisible injected context, and longer-running task/executor flows
- the next scaling step should be more declarative and more legible, not more bespoke Python profiles

Reference:
- `Docs/_internal/what_to_borrow_from_claude_code.md`
- `Docs/_internal/declarative_runtime_primitives.md`
- `Docs/_internal/chat_to_task_instruction_hardening_plan.md`
- `Docs/_internal/task_creator_editor_sprint_plan.md`
- repetitive reporting tasks in this subtrack now use persistence-level duplicate suppression plus conservative dataset-level shaping (recent-repeat trimming and light title-cluster diversity); broader story-cluster diversity remains the next likely quality step, not yet committed

#### Task Creator And Editor (Active Sprint)
Status:
- first slice now working locally: chat can propose a `TaskDraftPayload`, the client can open a prefilled native task editor popup, and the user can correct task family before save
- the next step is to carry the same native-surface pattern over to reopening and correcting existing tasks after creation

Scope:
- keep the chat-created draft popup/editor path stable and improve family-aware save behavior
- reuse the same editor model for reopening and correcting saved tasks
- make task family, schedule, topic/path, and similar core fields visible and correctable both before and after save
- continue treating the popup/native-surface pattern as a broader product interaction model, not just a one-off task flow

Reference:
- `Docs/_internal/task_creator_editor_sprint_plan.md`
- `Docs/_internal/chat_triggered_native_surfaces_note.md`

#### JSON/API Live Integration Lesson
The first live weather integration exposed an important runtime seam: chat tool calls do not always arrive in the exact nested shape the backend adapter expects. Fruitcake now treats tool-argument normalization as part of the JSON/API contract surface, not as profile-specific glue. The current compatibility rule lifts non-reserved top-level tool-call fields into `query_params` before backend execution, and future backend-owned integrations should assume this normalization layer is required for stable chat behavior.

## Phase 6 — Cloud Judgment Routing (as needed)

**Dependency**: Depends on completion of the Phase 5.4 reliability gate.

**Checkpoint Note**: Phase 6 has begun in a narrow form. Routing/accounting infrastructure has landed, but cloud judgment remains intentionally constrained until real usage shows where it materially improves outcomes enough to justify the data-exposure tradeoff.

Recent evidence:
- Frontier models are materially improving normal chat quality in practice.
- Real-world chat traces exposed a Phase 6 routing failure mode: search-heavy requests could stay on the simple-chat path, burn the bounded turn budget on repeated `web_search` retries, and fall back to a generic error.
- The first Phase 6 hardening slice has already landed:
  - address/location lookup requests now classify as complex earlier,
  - repeated failed `web_search` churn now stops with a graceful narrowing prompt instead of routinely dying on the simple-chat turn cap.
- User-visible hybrid controls have also landed:
  - chat sessions can select from configured models,
  - the client now exposes inline `Reasoning` controls for `Automatic`, `Fast`, and `Deep`,
  - token usage inspection makes the selected model path auditable,
  - tasks can now opt into a per-task model override while `Automatic` preserves the existing small/large task routing defaults.
- Routing/trust-boundary cleanup has also advanced:
  - README now states the current chat/task routing policy explicitly,
  - the trust boundary around cloud models, server-side secret resolution, and backend-owned API adapters is now documented in one place.
- Structured external integration work has also landed:
  - a first-class backend-owned JSON/API execution substrate now exists,
  - user-owned encrypted secrets are now available for authenticated integrations,
  - the first authenticated API-backed proving path is live through the ISS/N2YO watcher,
  - small task-local dedupe state now prevents repeated API-backed alerts when nothing changed.
- Graceful failure behavior for research-heavy chat has also been tightened:
  - repeated failed search loops now return a bounded explanation with recent tool outcomes,
  - max-turn failures now return a tool-aware narrowing message instead of the generic fallback.
- Alpha hardening now explicitly includes MCP/skills cleanup:
  - ship with a conservative default MCP surface,
  - keep only core first-party MCPs enabled by default,
  - treat shell/browser automation as developer-only integrations,
  - keep only reviewed shared skills in the alpha-default path.

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

Phase 6 now includes not just cloud-routing policy, but routing-quality work around when chat should escalate out of simple local execution into deeper orchestration or stronger judgment.

### Sprint 6.y — Structured JSON/API Integration

This sprint is now substantially landed. Structured external API access is no longer a plan-only item; it is a first-class backend capability rather than something chat/tasks must improvise through generic web search or shell workflows.

Landed scope:

- trusted backend-owned JSON/API execution path
- user-owned encrypted secrets for authenticated integrations
- deterministic JSON normalization/validation through approved adapters
- small task-local dedupe state for API-backed polling tasks
- first proving implementations:
  - structured place lookup
  - authenticated ISS/N2YO watcher flow
  - authenticated Alpha Vantage `global_quote` finance lookup

Still out of scope for this sprint:

- queued notifications / derived-event scheduling
- generic model-visible arbitrary API calling
- broad provider-specific integration abstractions beyond the first proving cases

Follow-up cleanup:

- refactor `api_service.py` from service-specific branching into a provider adapter registry before the approved service list grows much further

### Sprint 6.z — Linked Source Indexing And Codebase Awareness

Planned scope:

- first-class linked file and linked folder sources alongside imported library files
- manual rescan/reindex of linked sources with incremental change detection
- extracted text, summaries, and embeddings cached in Fruitcake while source files remain in place
- expanded plain-text extraction for Fruitcake's own code/config/document formats, including Python and other repo-relevant source files
- codebase-aware retrieval over linked repositories and external folders/drives

Deferred from this sprint:

- filesystem watching / automatic live sync
- large structured archive ingest such as Wikipedia/Kiwix `.zim` files or OpenStreetMap bulk downloads
- dedicated dataset parsers for non-document knowledge archives

Reference:

- `Docs/_internal/linked_source_indexing_and_codebase_awareness_plan.md`

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

### Phase 6 Follow-On — Platform Trust And Deployment

These are the next platform-facing roadmap items worth carrying forward after the current chat/task stability pass. They matter directly to Fruitcake's positioning as a trusted local server for households and small teams, but they should stay tightly scoped.

Near-term additions:

- **Workspace scoping MVP**
  - Goal: move from user-only ownership to first-class workspace ownership where it actually matters.
  - MVP scope only:
    - chat
    - library
    - tasks
    - memories
  - Required backend pieces:
    - `workspaces`
    - `workspace_memberships`
    - workspace-scoped ownership / visibility rules
    - invite + role-management APIs
    - audit trail for membership and scope changes
  - Explicitly deferred:
    - workspace-scoped RSS/feed management
    - skill sharing

- **Per-user authenticated integrations Phase A**
  - Goal: stop treating authenticated providers as deployment-wide credentials when the product is being used by multiple real users.
  - First implementation only:
    - Google Calendar per-user OAuth
    - Apple CalDAV per-user credential onboarding
    - connected-accounts UI in Settings
    - per-user credential resolution in calendar execution paths
  - Required backend pieces:
    - `user_integrations` table with one active integration per user/provider/service
    - secret references by durable ID, not name convention
    - signed OAuth state validation
    - concurrency-safe token refresh path
  - Product policy decision required before build:
    - whether users without a connected account get no personal provider access
    - or fall back to an explicit shared household provider
  - Explicitly deferred:
    - Gmail
    - Contacts
    - merged/shared family calendar views
    - admin “connect on behalf of user”
  - Reference:
    - `Docs/_internal/per_user_integrations_plan.md`
    - connector-specific workspace policy

- **Encrypted backup/restore**
  - Goal: make self-hosted trust real with a first-class recovery path.
  - Planned scope:
    - encrypted archive export of database + storage + essential config
    - restore flow with integrity verification
    - operator-friendly CLI / documented procedure
    - scheduled backup support later if the base format proves stable
  - Keep this operator-owned and explicit; do not hide restore semantics behind silent automation.

- **Secrets management completion**
  - Goal: finish the transition from `.env`-only configuration to a real app-owned secret contract.
  - Planned scope:
    - per-user and admin-managed secrets
    - audited secret access
    - encrypted import/export
    - backend-owned secret resolution for approved adapters/tools/tasks
  - Already landed:
    - dedicated encrypted user-owned secrets with a real `SECRETS_MASTER_KEY` requirement
    - owner-facing secret CRUD/disable flows and settings UI
    - audited secret access events with owner and admin metadata surfaces
    - backend-owned secret resolution tightened to approved service/secret mappings for current integrations
  - Still open:
    - admin-managed secrets beyond metadata inspection
    - encrypted import/export
    - broader secret contracts for future bounded API adapters
  - This continues the secrets groundwork already landed in the API integration path, but tightens the product contract and operator surface.

- **Agents of Chaos vulnerability hardening**
  - Goal: close the two immediate exposed risks identified in the internal "Agents of Chaos" review without turning the roadmap into an unfocused security rewrite.
  - Bounded scope only:
    - restrict linked-folder ingestion to operator-approved roots via `.env`
    - make task approval safer by default
    - broaden approval-gated tool coverage for persistent or high-risk mutations
    - update public/operator docs to match the new trust boundary
  - Explicitly deferred:
    - full workspace provenance model
    - signed connector provenance for all external channels
    - comprehensive quotas/watchdogs
    - full secrets-vault redesign
    - signed skill bundle integrity chain
  - Reference:
    - `Docs/_internal/agents_of_chaos_vulnerability_hardening_plan.md`

- **Hardware baseline docs**
  - Goal: publish honest minimum/recommended hardware guidance instead of leaving users to infer it from scattered notes.
  - Planned scope:
    - minimum and recommended hardware tiers
    - model guidance by machine class
    - basic latency / throughput / memory notes
    - deployment notes for common local-server targets
  - This is docs/devrel work, but it is important enough to track alongside product roadmap items because it directly affects trust and install success.

Internal future option:

- **Browser events MVP**
  - Keep this in the internal roadmap only for now.
  - Goal when revisited:
    - basic browser-event ingest endpoint
    - stored browsing-context records
    - conservative retrieval path for context, not broad browser automation
  - Not currently active work.
  - Keep references out of public-facing docs until the scope and trust model are clearer.

---

## Phase 7 — Trusted Local Capability Expansion (2 weeks)

Skill imports are curated, not bulk. Technical convertibility is not sufficient; a skill is included only if it strengthens Fruitcake's identity as a trusted, local personal assistant.

**Status**: 7.1 ✅ · 7.2 ✅ · 7.3 ✅ · 7.4 ⏳ · 7.5 ⏳

**Sprint 7.1** — Sandboxed filesystem MCP: `--allowed-paths /workspace`, per-user `workspace/{user_id}/`.

Completed:
- Added built-in filesystem MCP server with:
  - `list_directory`
  - `find_files`
  - `stat_file`
  - `read_file`
  - `write_file`
  - `make_directory`
- Enforced per-user workspace boundaries and path-escape protection.
- Added targeted filesystem MCP and agent tool-surface coverage.

**Sprint 7.2** — Shell MCP: `docker run --network none`, 30s timeout, 8k output cap, explicit blocked commands list.

Soak note:
- shell enforcement is working and auditable, but explicit shell-security-test prompts may still need stronger user wording before the model routes them to `shell_exec` on the first attempt. Once routed, tool-level refusal behavior is correct. Track as a follow-up only if it starts affecting normal workflows rather than deliberate security tests.

Completed:
- Added sandboxed `shell_exec` support with bounded output and refusal behavior.
- Verified shell MCP registration, routing, and refusal coverage in tests.
- Kept shell execution explicitly narrower than a general unrestricted local shell.

**Sprint 7.3** — Graph Memory Foundation (MCP-informed, Fruitcake-native)

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

Completed:
- Added graph-memory tables and service layer in the primary Postgres DB.
- Added user-scoped graph memory APIs and additive tool interfaces:
  - `create_memory_entities`
  - `create_memory_relations`
  - `add_memory_observations`
  - `search_memory_graph`
  - graph entity/node inspection APIs
- Preserved provenance and user scoping instead of adopting the MCP demo memory server as a runtime dependency.

**Sprint 7.4** — Curated OpenClaw skill conversion

Goal: once Phase 7 MCP tools are live, selectively convert the subset of OpenClaw skills that strengthens Fruitcake as a trusted, local personal assistant.

Planned outcomes:
- add `read_file`, `write_file`, and `shell_exec` to the converter's approved tool list when enabled
- add `POST /admin/skills/convert` to convert raw `SKILL.md` content into previewable Fruitcake skill payloads
- add a batch conversion script for directory-based OpenClaw skill conversion with a human-review report
- preserve review-first install flow: convert -> preview -> install, no blind auto-install
- track `source: "openclaw"` provenance on converted skills
- only grant `shell_exec` when the converted skill still makes sense inside Fruitcake's sandbox constraints

Naming and provenance rules:
- Core Fruitcake skills remain first-party Fruitcake skills even when they parallel an OpenClaw concept.
- OpenClaw-derived prefixes or provenance labels should only be applied to direct imported/converted skills.
- Do not brand foundational or product-defining skills as imported just because similar skills existed upstream.

Reference:
- `Docs/OpenClaw_Skill_Converter_Spec.md`

Note:
- `Docs/Phase 7 Tool Expansion.md` overlaps this sprint heavily and should be treated as supporting planning material, not a separate roadmap item.

### Completed outside the original Phase 7 sprint list

These changes landed after the original roadmap text was written and materially expand the trusted-local capability story even though they were not originally captured as Phase 7 sprint items:

- Built-in recurring task profiles:
  - `rss_newspaper`
  - `morning_briefing`
  - `topic_watcher`
  - `maintenance`
- Profile/spec architecture was tightened:
  - bundled prompt specs,
  - profile-owned validation,
  - cleaner separation between persona, task contract, and deterministic code.
- `topic_watcher` now supports:
  - freshness-aware RSS monitoring,
  - guarded prepared-context execution,
  - memory-candidate generation,
  - owner-scoped flat-memory approval via API,
  - first-class proposal persistence for shared memory review,
  - default `episodic` watcher memories with `30 day` expiry for later consolidation.
- Shared memory review surface now exists across backend and client:
  - `memory_proposals` persistence,
  - `/memories/review` list/detail/approve/reject API,
  - Swift `Memory` view split into `Saved` and `Review`,
  - task-run artifacts retained as provenance instead of acting as the primary review queue.
- Normal chat now supports first-class task lifecycle management:
  - `create_task`
  - `update_task`
  - `list_tasks`
  - `get_task`
- Chat task persistence now reuses the same shared create/update path as the REST task API:
  - persona resolution,
  - profile normalization,
  - schedule parsing / `next_run_at`,
  - active-hours persistence.
- Chat mutation integrity is stricter:
  - task creation/update claims are validated against actual executed tools,
  - duplicate-task creation followed by a false “updated” claim now triggers retry instead of being returned to the user,
  - chat can inspect current task state before making follow-up edits instead of guessing.
- RSS/newspaper quality and trust work landed:
  - repeat suppression,
  - publication history,
  - equivalent-feed dedupe,
  - deterministic maintenance-style refresh handling.

Treat these as completed capability work already present in `main`, not as pending Phase 7 ideas.

**Sprint 7.5** — First Agent Runs

Goal: establish the minimum persistent run substrate for agent-style work inside Fruitcake core before broader memory-phase work.

Scope:
- keep v1 backend-first
- extend the existing `TaskRun` / `TaskRunArtifact` / inspection model instead of introducing a parallel `AgentRun` system
- make agent-style execution a specialized run mode with bounded metadata and lifecycle state
- keep the slice narrow enough to support later memory workflows and management visibility without committing to full multi-agent orchestration

Minimum planned behavior:
- create an agent-style run record
- persist bounded lifecycle state:
  - `queued`
  - `running`
  - `completed`
  - `failed`
  - `cancelled`
- store lightweight identity metadata such as:
  - run kind / execution mode
  - agent role
  - source trigger or source context
- continue using the existing artifact path for summaries and structured outputs
- support inspection through the existing run-inspection surfaces with minimal additions rather than a brand-new inspector tree

Guardrails:
- do not build a full child-agent orchestration system in this sprint
- no autonomous spawn tree
- no cross-agent scheduler
- no assignment engine
- no user-facing client UI required in v1

Acceptance:
1. A first-class agent-run attempt can be created, persisted, inspected, and completed without being confused with ordinary task scheduling metadata.
2. Agent-run records support bounded role/type metadata and basic lifecycle state.
3. Existing task-run inspection surfaces remain backward compatible.
4. The slice stays narrow enough to support future memory and management work without committing to full sub-agent orchestration.

Checkpoint note:
- agent-run metadata has landed on the existing `TaskRun` substrate:
  - `run_kind`
  - `agent_role`
  - `trigger_source`
  - `source_context`
- backend-only agent-run creation/update/inspection paths are in place
- approval resume now dispatches immediately and correctly resumes paused `waiting_approval` runs
- live agent-family task execution now stamps runs as `run_kind = agent` and preserves the selected `agent_role`
- agent-family tasks can export findings to workspace files through task-owned export routes
- client task surfaces now support:
  - creating `Agent` tasks with an explicit role
  - showing agent-role badges
  - exporting agent findings from both the detail sheet and the row action area
- agent tasks now support user-selected `context_paths`, and agent-definition `required_context_sources` are preloaded before search/tool wandering
- task creation now validates overlong titles cleanly in both backend and UI, which matters because agent instructions are often long
- task and run inspection now surface resolved agent-definition metadata instead of only raw persona/role state
- the category/preset registry is now the active architecture for agent work:
  - category-backed presets load from `config/agents.yaml`
  - task creation groups presets by category
  - task/run inspection surfaces expose both preset and category
- managed agent instances now exist as first-class recurring agent configuration:
  - `Settings > Agents` manages durable instance defaults
  - Tasks remains the execution surface for the linked recurring backing tasks
  - the first stable managed trio is now seeded as:
    - `Main Library Sync`
    - `Primary Repo Map`
    - `Run Health Check`
- managed agent instances now support:
  - enable / disable
  - schedule and active-hours defaults
  - model override
  - context file selection
  - repo-root selection for repo map instances
  - latest-run visibility from the Settings surface with live refresh while visible
- task detail now shows a `Latest Run` summary so background agent work is less opaque than a hidden task session
- close-out stabilization for this branch is now in place:
  - legacy hidden `document_sync_manager`, `repo_map_manager`, and `recent_run_analyzer` duplicates are cleaned up during seed reconciliation
  - disabling a managed instance now leaves its linked recurring task unqueued instead of silently continuing through stale duplicates
  - migration sanity and app build both pass again after the branch cleanup

Remaining work in this sprint:
- improve background-agent operator visibility beyond status + latest-run summaries
- decide whether agent instances remain a Settings-only management surface or need a stronger dedicated operator view later
- finish removing remaining temporary persona-compatibility dependence where presets now carry the real behavior contract
- keep large runtime hardening work, especially prompt-history compaction, out of this branch

Design decision update:
- agent mode is no longer treated as a soft extension of generic tasks/personas alone
- Fruitcake should adopt a category-based agent registry for specialist work agents and long-running service agents
- the reference model is the agent-definition discipline proven in `/Users/jwomble/Development/src`, especially:
  - explicit `when to use`
  - role-specific system prompts
  - tool/capability boundaries
  - execution-mode separation
  - explicit drift prevention
- this decision belongs inside Sprint 7.5 because first agent runs exposed the missing abstraction; it is a clarification of the sprint architecture, not a separate roadmap phase
- near-term implementation should stay narrow:
  - formalize the category + preset structure
  - keep `persona` as a temporary runtime carrier where needed
  - define the first few presets cleanly before expanding quantity
- the first registry slices have now landed:
  - file-backed agent registry in `config/agents.yaml`
  - dynamic agent picker population in task creation UI
  - resolved agent-definition details shown in task inspection surfaces
  - category-grouped preset metadata available from `/chat/agents`
  - task picker now groups presets by category instead of treating every preset as a top-level agent type

Core agent categories:
- `explore`
- `verify`
- `plan`
- `general`
- `monitor`

First active Fruitcake presets:
- `roadmap_verifier` → `verify`
- `runtime_inspector` → `verify`
- `document_sync_manager` → `monitor`
- `repo_map_manager` → `monitor`
- `recent_run_analyzer` → `verify`

Behavioral follow-up already captured inside this sprint:
- `roadmap_verifier` works best when scoped narrowly and grounded on synced or explicitly attached docs
- `runtime_inspector` now defaults to the most recent non-self run/task when asked for the "latest" item
- `runtime_inspector` should prefer run-level inspection surfaces over task-only summaries and should not end with optional action menus unless explicitly asked

Planned next phase:
- the next branch should pivot from agent-surface expansion to runtime hardening
- first priority is agent context budgeting and compaction:
  - budget oversized tool results before they hit the model
  - compact older history into explicit boundaries instead of replaying everything forever
  - recover from context-window overflows by compacting and retrying once
  - add no-progress / loop detection for high-churn agents such as `repo_map_manager`
- this should be treated as the immediate post-7.5 follow-up because first real agent runs exposed prompt-history bloat as the limiting weakness for longer-running background work

Immediate post-branch bug follow-up:
- this slice has now landed and should be treated as completed follow-through, not future scope
- RSS-backed chat no longer shows the earlier failure mode where it kept over-researching instead of converging
- the bug/hardening pass delivered:
  - provider-safe replay sanitization for compacted / filtered tool-call histories
  - a cleaner recent-headlines path with recent-item dedupe and compact formatting
  - stronger answer convergence for both chronology/timeline prompts and headline-roundup prompts
  - synthesis-first fallback behavior when RSS research is no longer making progress
  - validation coverage for article/detail follow-ups so internal fetch narration does not leak into final chat answers
- the next RSS work, if any, should be treated as normal product polish or performance tuning rather than emergency runtime repair

Reference:
- [first_agent_runs_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/first_agent_runs_plan.md)
- [fruitcake_agent_definition_v1_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/fruitcake_agent_definition_v1_plan.md)
- [agent_context_preload_and_repo_map_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/agent_context_preload_and_repo_map_plan.md)
- [fruitcake_agent_taxonomy_and_candidate_categories_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/fruitcake_agent_taxonomy_and_candidate_categories_plan.md)
- [early_agent_trio_and_lightweight_agent_creation_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/early_agent_trio_and_lightweight_agent_creation_plan.md)
- [agent_context_budgeting_and_compaction_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/agent_context_budgeting_and_compaction_plan.md)
- [rss_chat_convergence_and_headline_roundup_plan.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/rss_chat_convergence_and_headline_roundup_plan.md)

### Phase 7 → Phase 8 Bridge: Memory Review And Consolidation

This sprint effectively closes the gap between watcher output and durable memory workflow:

- watcher runs can now surface multiple distinct memory proposals,
- proposals persist independently of task history,
- approvals happen from the shared memory surface instead of requiring task-local indexing,
- approved watcher developments are intentionally stored as short-lived `episodic` memories first.

This sets up the correct next move for Phase 8:

- use Dream Cycle / nightly synthesis to consolidate repeated episodic developments into more durable semantic memory where justified,
- prune expired watcher/event memories cleanly,
- treat the approved proposal queue as higher-quality input than raw model output.

---

## Future Horizon — Phase 8: Nightly Memory Extraction

**Status**: future

**Trigger**: Phase 4 has been running for several weeks and real-world data shows what the agent misses.

A nightly background task reviews the previous 24 hours of chat sessions and extracts memories the agent didn't explicitly create via `create_memory`. This catches patterns that only become obvious in retrospect.

The extraction prompt reviews each session: *"Extract any facts about the user worth remembering. Return JSON: [{content, type, importance, expires_in_days}]."* New memories are deduplicated against existing ones before insertion.

Phase 8 is also the natural place to revisit memory-writing policy and continuity-layer design. See [anthropic_memory_comparison.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/anthropic_memory_comparison.md) for a deferred comparison note on separating instruction memory, durable memory, and session continuity.

Phase 8 is also a reasonable place to revisit a future persistent-assistant layer: a durable assistant identity with resumable continuity and bounded execution windows, distinct from both ordinary chat sessions and one-shot task runs. See [kairos_persistent_assistant_note.md](/Users/jwomble/Development/fruitcake_v5/Docs/_internal/kairos_persistent_assistant_note.md).

Phase 8 is also the natural place for retrospective prompt-quality review. A related concept note, [Prompt_Drift_Review.md](/Users/jwomble/Development/fruitcake_v5/Docs/Prompt_Drift_Review.md), outlines a nightly loop that inspects run traces, detects recurring prompt drift, and proposes tighter prompt variants for review rather than silently mutating live prompts.

---

## Future Horizon — Phase 9: Enterprise Fork

**Status**: future

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
  - UI batching note: this should be treated as part of a broader admin/debug UI phase that groups run inspection, secret-access audit visibility, and similar operator surfaces after the underlying backend payloads have proven stable.
  - Earliest likely sprint: after more soak confirms the payload shape is stable.

- **Filesystem MCP expansion beyond Phase 7.1**
  - Why it matters: the current workspace tool surface now covers discovery, inspection, reading, writing, and directory creation, but some real workflows may later want a few more file operations.
  - Current status: `Phase 7.1` intentionally stops at `list_directory`, `find_files`, `stat_file`, `read_file`, `write_file`, and `make_directory` to keep the workspace contract narrow and trustworthy.
  - Decision needed later: whether to add selective helpers like `append_file`, `read_multiple_files`, or carefully-scoped rename/move support without drifting into a general-purpose file manager.
  - Earliest likely sprint: after real workspace usage shows repeated friction that the current tool set cannot handle cleanly.

- **Chat responsiveness optimization**
  - Why it matters: local chat still feels slower than direct model interaction, especially once memory, tools, and grounding are layered in.
  - Current status: a `5.6.13` branch tried heuristic tool/memory/history gating to improve perceived speed, but it regressed response quality and caused brittle tool-access failures; those changes were rolled back. The safe pieces kept from that work are stage latency instrumentation plus grounded library summary and PDF ingest fixes.
  - Decision needed later: whether to improve responsiveness through true streaming, prompt/context budgeting, or architecture changes rather than heuristic suppression of capabilities.
  - Earliest likely sprint: after the RAG restoration pass, using measured latency data instead of aggressive chat-path trimming.

- **Nightly prompt drift review**
  - Why it matters: smaller local models often benefit from tighter prompts, but prompt bloat should be justified by observed failures rather than guesswork.
  - Current status: early shell/tooling soak already shows cases where more explicit wording improves precision. The concept is documented in [Prompt_Drift_Review.md](/Users/jwomble/Development/fruitcake_v5/Docs/Prompt_Drift_Review.md).
  - Decision needed later: whether nightly prompt review should remain recommendation-only or evolve into a more automated prompt-tuning loop for low-risk prompt surfaces.
  - Earliest likely sprint: Phase 8 follow-on, once dream-cycle review infrastructure is in place.

- **Behavior editor / developer studio**
  - Why it matters: built-in watchers, personas, task profiles, skills, and maintenance contracts are becoming structured enough that hand-editing files will slow iteration and increase configuration mistakes.
  - Current status: behavior editing is still repo/file-driven; the new memory review surface proves the app can support first-class review/editor workflows, but there is no GUI for editing watcher vocabularies, persona rules, profile specs, or skill/task contracts yet.
  - Decision needed later: whether to introduce a dedicated GUI surface for editing watchers, skills, tasks, personas, and related behavior layers, with local-model assistance limited to drafting and keyword/theme generation rather than autonomous config mutation.
  - Earliest likely sprint: after Phase 8 memory consolidation work, once watcher/profile contracts are stable enough to justify an editor surface.

- **Application secrets management**
  - Why it matters: chat-created tasks and richer automations are starting to reference external APIs, and `.env` is the wrong abstraction for per-integration or task-usable secrets. Fruitcake needs a first-class way to store credentials safely and let approved task/tool paths use them without leaking values into prompts, logs, or files.
  - Current status: the first real product slice is now in place: encrypted user-owned secrets, explicit master-key enforcement, audited secret access, owner/admin audit metadata views, narrowed backend secret resolution for approved integrations, and a user-facing settings surface for managing and inspecting secret activity. The contract is stronger, but not finished.
  - Decision needed later: how broad the vault should become beyond the currently approved adapter/tool paths, and when to add admin-managed secrets or encrypted import/export.
  - Planned sequence: finish `Application secrets management`, keep `JSON/API integration path` bounded and backend-owned, then consider separate `Derived-event scheduling / queued notifications`.
  - UI batching note: keep narrowly-scoped user settings surfaces shipping with backend capability when they unblock real use, but defer broader admin/debug UI expansion to a single later phase so operator panels, audit views, and inspection tools can be designed together instead of feature-by-feature.
  - Next step: start the bounded `JSON/API integration path` on top of the hardened secret store rather than widening the vault surface first.

- **JSON/API integration path**
  - Why it matters: recurring tasks are starting to describe real external API workflows, and the current tool surface is weak for deterministic JSON fetch/parse/validate behavior.
  - Current status: backend-owned API execution is now live for the approved N2YO, Alpha Vantage, and current-weather contracts, with per-task API dedupe already in place and the first task-safe response extraction slice landed via `response_fields` selectors on normalized payloads. This groundwork is now substantially landed rather than merely queued next, and the broader generic JSON contract layer remains intentionally narrow.
  - Decision needed later: whether to support only backend-owned API contracts at first or also allow broader model-declared integration specs once the secrets boundary is proven.
  - Earliest likely sprint: immediately after `Application secrets management`, using that secret store for auth injection and keeping queued notifications explicitly out of scope.

- **Derived-event scheduling / queued notifications**
  - Why it matters: tasks like ISS pass alerts need “notify shortly before event time” behavior that recurring polling alone cannot express cleanly.
  - Current status: the scheduler runs recurring tasks, but it does not yet support creating and managing downstream delayed notifications derived from fetched data.
  - Decision needed later: how derived notifications should be queued, updated, deduplicated, or canceled when source data changes.
  - Earliest likely sprint: after the JSON/API integration path and per-task dedupe state are stable enough to define a general delayed-notification contract.

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

- **Task creator/editor UX**
  - Why it matters: recent chat-to-task runtime work proved that backend normalization helps, but outside testers still need a visible correction surface before save and an easy way to reopen malformed tasks later.
  - Current status: backend recipe normalization, confirmation summaries, and family-switch cleanup are now in `main`, but chat can still infer the wrong family before save because the editor/review surface does not exist yet.
  - Decision needed next: whether to start with chat-created draft review only or include reopening existing task detail in the same sprint. Current plan is creator-first with reopen/edit immediately after on the same field model.
  - Earliest likely sprint: next active declarative-runtime/product sprint.

- **Sub-agent spawning**
  - Why it matters: specialist delegation could unlock more complex workflows later, but it complicates trust, approval, and audit lineage in a product that currently benefits from a simple execution model.
  - Current status: postponed out of Phase 7 and not currently assigned to an active phase.
  - Decision needed later: whether child agents can be introduced without weakening approval clarity, audit transparency, or the single-agent trust model.
  - Earliest likely sprint: after the local-capability and dream-cycle roadmap is stable enough to absorb the added complexity.

- **Distributed worker-node architecture**
  - Why it matters: the current single-instance deployment model will eventually limit throughput once autonomy, larger models, or multiple concurrent users become normal.
  - Current status: architecture direction documented in `Docs/WorkerNode.md`; concept is coherent and fits Fruitcake's shared-DB design, but it is not implementation-ready enough to commit as an active sprint.
  - Decision needed later: whether to scale task execution with dedicated worker nodes, worker registry/heartbeat, and model-tier routing rather than continuing to scale vertically on one machine.
  - Earliest likely sprint: after task durability, task claiming, and worker recovery semantics are stable enough to distribute safely.

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
# Default — mixed local routing, verified on M1 Max 64GB
LLM_MODEL=ollama_chat/qwen2.5:32b
LOCAL_API_BASE=http://localhost:11434/v1
TASK_SMALL_MODEL=ollama_chat/qwen2.5:14b
TASK_LARGE_MODEL=ollama_chat/qwen2.5:32b
TASK_FORCE_LARGE_FOR_PLANNING=false
TASK_FORCE_LARGE_FOR_FINAL_SYNTHESIS=true

# All-14b fallback if stability or memory pressure becomes a problem
# LLM_MODEL=ollama_chat/qwen2.5:14b
# TASK_SMALL_MODEL=ollama_chat/qwen2.5:14b
# TASK_LARGE_MODEL=ollama_chat/qwen2.5:14b

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
