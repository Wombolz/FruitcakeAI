# FruitcakeAI

**A self-hosted AI agent platform you run on your own hardware and extend however you want.**

FruitcakeAI is a complete autonomous agent runtime — persistent memory, task scheduling, multi-user access control, hybrid RAG document pipeline, MCP extensibility, and a native Swift client, all wired together and working. There's no SaaS dependency, no ongoing API cost, and no vendor lock-in. You bring your own models via Ollama, or swap in Claude or OpenAI with one environment variable.

It's built for self-hosters, homelab tinkerers, developers, and small teams who want real autonomous AI capabilities without giving up ownership of their data or their stack. The foundation is fully working. What you build on top of it is up to you.

---

## What It Is

FruitcakeAI is not a chat UI wrapper. It's an agent platform with a full working runtime underneath:

- The **agent** reasons over your memory, documents, and tools — not just the current message
- The **task engine** plans and executes multi-step work autonomously, on a schedule, or triggered by webhooks
- The **chat layer** can now create, inspect, and update real persistent tasks instead of only acting as a one-shot Q&A surface
- The **memory system** persists what matters across sessions, with 3-tier retrieval and semantic search
- The **RAG pipeline** ingests your documents and makes them queryable with hybrid BM25 + vector retrieval
- The **MCP layer** lets you drop in any tool server via a config file — no code changes required
- The **persona system** scopes what each user can do, see, and ask — down to individual tools

Every subsystem is independently testable, swap-able, and extensible. If you want to rip out a layer and replace it with your own, the seams are there.

---

## Design Principles

**Local-first, cloud opt-in.** The system runs fully offline against Ollama. Cloud LLMs are one env var away. Your data stays on your hardware unless you explicitly route it elsewhere.

**Zero ongoing API cost by default.** Ollama with a local model costs nothing to run. Cloud routing is opt-in and per-signal — you decide what, if anything, leaves the machine.

**Extensible via MCP — additive integrations.** New capabilities arrive as MCP servers in a config file. The agent discovers them at startup. Nothing in core depends on any optional provider. Everything added is additive; nothing is load-bearing.

**Multi-user and safe by default.** Role-based personas with scoped tool access. Per-persona content filtering and blocked tool lists. Approval gates before irreversible actions. Active-hours windows that prevent the autonomous agent from running at 3am.

**No speculative infrastructure.** The codebase contains only what the current use case requires. No Kubernetes, no distributed queues, no ELK stack. Complexity is added when real friction demands it, not in anticipation of scale that doesn't exist yet.

---

## Security

FruitcakeAI is designed for local-first, operator-controlled deployments. The current baseline is strong for trusted local hardware and trusted networks, but it is not positioned as internet-hardened by default.

Before any shared-network or remote deployment:

- change all seeded passwords
- set a strong `SECRET_KEY` / JWT secret
- keep PostgreSQL off public interfaces
- put the API behind TLS and an authenticated reverse proxy or VPN
- treat webhook keys, APNs keys, and MCP credentials as secrets

See [Security Baseline](Docs/SECURITY_BASELINE.md) for the full operator checklist, trust assumptions, and current limits.

---

## What's Shipped

```
iPhone / Mac app  →  FastAPI backend  →  Ollama (local LLM)
                              ↓
                    LlamaIndex RAG (pgvector)
                    MCP tool servers (calendar, web, RSS)
                    Autonomous task engine (scheduler + runner)
                    Persistent memory (3-tier retrieval)
                    Inbound webhooks (external triggers)
```

**Agent and personas**
- LiteLLM agent loop — works with Ollama, Claude, or OpenAI via one env var change
- Multi-user JWT auth with role-based personas (`admin`, `parent`, `restricted`, `guest`)
- Per-persona tool scoping, content filtering, and tone configuration
- Streaming chat via WebSocket; REST fallback

**Memory**
- Persistent per-user memory written by the agent via `create_memory` tool
- 3-tier retrieval: procedural rules → importance-ranked facts → pgvector semantic search
- Memory injected into every task prompt and heartbeat evaluation
- Immutable history — memories are deactivated, never overwritten
- Shared memory review flow for proposed memories:
  - pending review queue
  - approve / reject from the client
  - proposals retained separately from task-run provenance
- Graph memory foundation for entities, relations, and observations

**Document library**
- Upload and ingest PDFs and documents into a personal or shared library
- Hybrid BM25 + vector + RRF retrieval with source citations
- Per-user library scoping — personal, shared, team

**Knowledge skills**
- Admin-managed skills stored in the database as frozen prompt extensions
- Shared or personal scope, injected into chat, tasks, and webhooks only when relevant
- Semantic gating with prompt-budget caps to prevent prompt bloat and drift
- Preview/install flow, injection diagnostics, superseding updates, and per-run skill attribution

**Autonomous tasks**
- One-shot and recurring tasks with `every:Xm/h/d` or cron schedules
- Active-hours windows prevent off-hours autonomous action
- Approval gate for irreversible actions — task pauses, pushes a notification, waits
- Exponential retry on transient failures
- Built-in task profiles for common recurring work:
  - `rss_newspaper`
  - `morning_briefing`
  - `topic_watcher`
  - `maintenance`
- Topic watchers can:
  - monitor prepared RSS data for a topic
  - suppress repeats across runs
  - propose memory candidates for approval

**Chat task operations**
- Chat can create, inspect, and update real persistent tasks
- Task creation/update in chat uses the same persona/profile/schedule resolution path as the REST task API
- Chat validation rejects claims that a task was created or updated unless the matching task tool actually confirmed success

**Visibility and review**
- LLM usage events are recorded per chat/task/tool stage with estimated token cost
- The client exposes memory review and token-usage views so operator-facing state is no longer API-only

**Integrations**
- Calendar — Google Calendar and Apple Calendar
- Web research — Brave, DuckDuckGo, NewsAPI
- RSS — feed management, discovery, and search
- Webhooks — inbound triggers from GitHub, Zapier, IFTTT, or any HTTP client
- Docker stdio MCP — drop in any MCP-compatible server via config

**Mobile**
- Native Swift app for iPhone and Mac
- APNs push notifications for task results and approval requests
- On-device FoundationModels fallback for calendar, reminders, and contacts when offline


---

## Quick Start

### Prerequisites

- macOS with [Homebrew](https://brew.sh)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Ollama](https://ollama.ai) — `brew install ollama`
- Python 3.11+

Optional later:
- [Xcode 16+](https://developer.apple.com/xcode/) — for the iOS/macOS Swift app after backend setup succeeds

### 1. Clone

```bash
git clone https://github.com/Wombolz/FruitcakeAI.git FruitcakeAI
cd FruitcakeAI
```

### 2. Bootstrap the backend

```bash
./scripts/bootstrap.sh
```

Bootstrap does the following:
- creates `.env` from `.env.example` if needed
- detects RAM and chooses local model defaults
- creates `.venv` and installs dependencies
- starts PostgreSQL
- ensures the required Ollama models are present
- runs migrations
- seeds default users
- starts the API on `http://localhost:30417`

RAM-aware defaults:
- systems with **less than 64 GB RAM** default to all-`14b`
- systems with **64 GB RAM or more** default to mixed `14b/32b`
- existing `.env` values are preserved on rerun

> Verified on M1 Max 64GB with mixed routing: `32b` for chat and final synthesis, `14b` for heavier task planning/execution. If memory pressure or stability becomes a problem, edit `.env` and switch to all-`14b`. Cloud backends (Claude, OpenAI) are configured in `.env` — see [LLM Backends](Docs/LLM_BACKENDS.md).

### 3. Verify backend health

```bash
curl http://localhost:30417/health
# -> {"status":"ok"}

./scripts/doctor.sh
```

Use `./scripts/doctor.sh` whenever setup looks wrong. It reports required failures separately from optional degraded components like the shell MCP image or APNs config.

Health semantics:
- `PASS`: required backend dependencies are ready
- `DEGRADED`: backend is usable, but optional components are missing
- `FAIL`: one or more required backend dependencies are not ready

Default users — **change these passwords before running on a shared network:**

| Username | Password | Role |
|----------|----------|------|
| admin | changeme123 | admin |
| parent | changeme123 | parent |
| restricted | changeme123 | restricted |
| guest | changeme123 | guest |

### 4. Open the Swift app (optional)

1. Open the `FruitcakeAI_Client` Swift project in Xcode
2. Build and run (`⌘R`)
3. Settings → Server URL → `http://localhost:30417`
4. Log in with any seed user

Upload a document via the Library tab, then ask about it.

If bootstrap or verification fails, see [Pre-Alpha Troubleshooting](Docs/PreAlpha_Troubleshooting.md).

---

## Adding Capabilities

New tools and data sources are added via `config/mcp_config.yaml` — no code changes required. The agent discovers them at startup and starts using them.

```yaml
mcp_servers:
  my_new_tool:
    type: docker_stdio
    image: my-mcp-server:latest
    enabled: true
    timeout: 30
```

See [Adding MCP Tools](Docs/ADDING_MCP_TOOLS.md) for the full guide.

FruitcakeAI core functions fully without any optional provider. Every integration is additive.

FruitcakeAI also supports admin-managed knowledge skills. Skills do not add new runtime processes or MCP servers; they add scoped, installable prompt extensions that can be injected into chat, tasks, and webhooks when relevant. They are installed through the admin API with a preview → install flow and remain bounded by existing persona and execution-profile restrictions.

---

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

---

## Project Structure

```
FruitcakeAI/
├── app/
│   ├── agent/          Agent loop, tools, personas, context builder
│   ├── api/
│   │   ├── admin.py    Admin: users, audit, metrics, task-runs, skills
│   │   ├── chat.py     Chat sessions + WebSocket streaming
│   │   ├── devices.py  APNs device token registration
│   │   ├── library.py  Document upload + RAG query
│   │   ├── memories.py Persistent memory CRUD
│   │   ├── tasks.py    Autonomous task CRUD + manual trigger
│   │   └── webhooks.py Inbound webhook configs + trigger endpoint
│   ├── auth/           JWT auth, user management
│   ├── autonomy/       TaskRunner, scheduler, approval gate, APNs push
│   ├── memory/         MemoryService — 3-tier retrieval, dedup, pgvector
│   ├── mcp/            MCP registry + internal servers (calendar, web, RSS)
│   ├── skills/         Skill install, selection, validation, and injection logic
│   └── rag/            LlamaIndex RAG + hybrid BM25/vector/RRF retriever
├── config/
│   ├── mcp_config.yaml     MCP server definitions
│   ├── personas.yaml       Persona definitions
│   └── users.yaml          Seed users
├── tests/              API, agent, task, memory, and integration coverage
├── scripts/
│   ├── start.sh        One-command startup
│   ├── stop.sh         Stop local services
│   └── reset.sh        Wipe and reseed
└── docker-compose.yml  PostgreSQL + pgvector
```

---

## Documentation

| Doc | Purpose |
|-----|---------|
| [Design Philosophy](Docs/DesignPhilosophy.md) | Why FruitcakeAI is built the way it is |
| [Security Baseline](Docs/SECURITY_BASELINE.md) | Current security posture, operator responsibilities, and deployment limits |
| [Adding MCP Tools](Docs/ADDING_MCP_TOOLS.md) | How to extend the system with new tools |
| [Persona System](Docs/PERSONA_SYSTEM.md) | Configuring users, roles, and personas |
| [LLM Backends](Docs/LLM_BACKENDS.md) | Switching between Ollama, Claude, OpenAI |
| [RSS Newspaper Example](Docs/RSS_Newspaper_Example.md) | Example of a structured built-in task profile |
| [Pre-Alpha Troubleshooting](Docs/PreAlpha_Troubleshooting.md) | Common install and recovery fixes |

---

*FruitcakeAI — Put a fruitcake in your home and be ready for anything.* 🍰
