# 🍰 FruitcakeAI

> *Put a fruitcake in your home and be ready for anything.*

A private, local-first AI assistant for individuals, households, and small teams — one that knows its people and keeps working under degraded conditions. Runs entirely on your hardware. No data leaves unless you choose to send it.

→ [Design Philosophy](Docs/DesignPhilosophy.md)

---

## What It Is

FruitcakeAI is built around three outcomes:

**Trust.** An assistant that multiple people can use, with per-user personas that scope tool access, filter content, and gate irreversible actions. Safety controls are a design constraint, not a feature layer.

**Privacy.** Local Ollama is the default and the baseline. The system is fully functional without a cloud API key. Cloud LLMs are an opt-in enhancement for users who choose to trade some privacy for a higher reasoning ceiling — not the starting point, not the assumption.

**Continuity.** The architecture degrades gracefully. When connectivity is limited, the system keeps working from cached data and local knowledge. When it's gone entirely, the local model and document library remain available. When data has an age, the system says so.

FruitcakeAI is not a cloud-first assistant with a local mode. It is a local-first system with an optional cloud enhancement. It is not a single-user power tool. It is not a prescribed configuration — what you put in it reflects your context and your priorities.

---

## Design Principles

**Privacy first, cloud optional.** Local Ollama is the default. Anyone who never configures a cloud API key never sends a token outside their network. That is a complete, fully-functional experience.

**Resilience by construction.** The system degrades gracefully through explicit tiers — from full local with live feeds, through cached-data operation, to fully offline on local library and memory alone. Every component communicates data freshness and confidence when it matters.

**Knows its people.** Persistent per-user semantic memory across sessions. Procedural preferences, episodic facts, long-term knowledge — retrieved and injected into every interaction. The system doesn't just remember what's in a checklist; it remembers what's been relevant for this person lately.

**Multi-user and safe by default.** Role-based personas with scoped tool access. Per-persona content filtering and blocked tool lists. Approval gates before irreversible actions. Active-hours windows that prevent the autonomous agent from acting at 3am.

**Modular via MCP — additive integrations.** New capabilities arrive as MCP servers added to a config file. The agent discovers them and starts using them. FruitcakeAI core must function fully without any optional provider. Everything added is additive; nothing is load-bearing.

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

**Document library**
- Upload and ingest PDFs and documents into a personal or shared library
- Hybrid BM25 + vector + RRF retrieval with source citations
- Per-user library scoping — personal, shared, team

**Autonomous tasks**
- One-shot and recurring tasks with `every:Xm/h/d` or cron schedules
- Active-hours windows prevent off-hours autonomous action
- Approval gate for irreversible actions — task pauses, pushes a notification, waits
- Exponential retry on transient failures

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
- [Xcode 16+](https://developer.apple.com/xcode/) — for the iOS/macOS Swift app
- Python 3.11+

### 1. Clone and configure

```bash
git clone https://github.com/Wombolz/FruitcakeAI.git FruitcakeAI
cd FruitcakeAI
cp .env.example .env
```

Edit `.env` — the only required change for a local setup:

```env
SECRET_KEY=change-me-to-a-random-string
DATABASE_URL=postgresql+asyncpg://fruitcake:fruitcake@localhost:5432/fruitcake_v5
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

### 2. Pull the model

```bash
ollama pull qwen2.5:14b
```

> Verified on M1 Max 64GB. `qwen2.5:32b` (~20GB) is a capable step up if you close other apps first. Cloud backends (Claude, OpenAI) are configured in `.env` — see [LLM Backends](Docs/LLM_BACKENDS.md).

### 3. Start

```bash
./scripts/start.sh
```

This starts PostgreSQL, waits for Ollama, runs migrations, seeds default users, and starts the API on `http://localhost:30417`.

Default users — **change these passwords before running on a shared network:**

| Username | Password | Role |
|----------|----------|------|
| admin | changeme123 | admin |
| parent | changeme123 | parent |
| restricted | changeme123 | restricted |
| guest | changeme123 | guest |

### 4. Verify

```bash
curl http://localhost:30417/health
# → {"status": "ok"}
```

### 5. Open the Swift app

1. Open the `FruitcakeAI_Client` Swift project in Xcode
2. Build and run (`⌘R`)
3. Settings → Server URL → `http://localhost:30417`
4. Log in with any seed user

Upload a document via the Library tab, then ask about it.

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

---

## Tests

```bash
source .venv/bin/activate
pytest tests/
# 139 passed — no running database required
```

---

## Project Structure

```
FruitcakeAI/
├── app/
│   ├── agent/          Agent loop, tools, personas, context builder
│   ├── api/
│   │   ├── admin.py    Admin: users, audit, metrics, task-runs
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
│   └── rag/            LlamaIndex RAG + hybrid BM25/vector/RRF retriever
├── config/
│   ├── mcp_config.yaml     MCP server definitions
│   ├── personas.yaml       Persona definitions
│   └── users.yaml          Seed users
├── tests/              139 tests, SQLite in-memory
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
| [Roadmap](Docs/FruitcakeAi Roadmap.md) | Where development is now and where it's going |
| [Repo Realignment Runbook](Docs/repo_realignment_runbook.md) | Release-prep rename, rollback, and validation checklist |

---

*FruitcakeAI — Put a fruitcake in your home and be ready for anything.* 🍰
