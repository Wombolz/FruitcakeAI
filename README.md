# FruitcakeAI

**Local-first AI with persistent memory, autonomous tasks, and role-based access — for individuals, households, and small teams.**

FruitcakeAI is a self-hosted AI assistant that remembers its users across conversations, runs recurring tasks autonomously, and keeps every byte of data on your hardware. Unlike cloud assistants, it gets to know the people it serves over time — and it never phones home.

```
FruitcakeAI_iOS (iPhone / Mac)  →  FruitcakeAI backend  →  Ollama (local LLM)
                                              ↓
                                Hybrid RAG (pgvector + BM25 + RRF)
                                MCP tools (calendar, web, RSS)
                                Autonomous task engine (scheduler + runner)
                                Persistent memory (3-tier retrieval)
                                Inbound webhooks (external triggers)
```

> **Who this is for**: Technical users comfortable with Docker, Python, and Xcode. If you can run `docker compose up` and build a Swift app, you can run FruitcakeAI. Self-hosted homelabs, households, and small teams are the primary audience.

**iOS/macOS client**: [github.com/fruitcakeai/fruitcake-ios](https://github.com/fruitcakeai/fruitcake-ios) — requires Xcode 16+.

---

## Why FruitcakeAI?

Most AI assistants are episodic — each conversation starts from scratch, and your data lives on someone else's servers. FruitcakeAI is different:

- **It remembers.** The agent writes memories as it learns about you — preferences, facts, recurring situations — and retrieves them semantically in every future conversation and task run.
- **It acts without being prompted.** Schedule recurring tasks ("summarize my RSS feeds every morning"), set active hours so it stays quiet at night, and get push notifications when something needs attention.
- **It stays on your hardware.** Ollama runs the LLM locally. Embeddings use a local HuggingFace model. No data leaves your machine unless you explicitly opt in to a cloud model.
- **It knows who it's talking to.** Role-based personas give different users different tool access and content filters — useful for households with kids, teams with varying access levels, or anyone who wants a guest mode.

---

## Features

- **Persistent memory** — agent writes memories via tool call; 3-tier retrieval (procedural rules → importance-ranked facts → pgvector semantic search) injects relevant context into every prompt
- **Autonomous tasks** — one-shot or recurring (`every:30m`, cron, or ISO timestamp); active-hours windows; exponential retry on failure; push notification on completion
- **Approval gates** — tasks flag irreversible actions (calendar mutations, email) for user sign-off before executing; approval via `PATCH /tasks/{id}` or the Inbox tab
- **Role-based personas** — `admin`, `parent`, `child`, `guest` roles with per-user `blocked_tools`, tone, and content filters; designed for multi-user environments
- **Hybrid RAG** — pgvector + BM25 + RRF fusion over uploaded documents; vector-only fallback if BM25 corpus unavailable; per-user library scoping (personal / shared)
- **MCP tool servers** — calendar, web search, RSS; add new tools via config, no code required
- **Inbound webhooks** — POST any JSON payload to `/webhooks/trigger/{key}` and the agent runs your instruction; compatible with GitHub, Zapier, IFTTT, or any HTTP client
- **On-device fallback** — offline mode uses Apple FoundationModels for calendar, reminders, and contacts
- **Model-agnostic** — swap between Ollama, Claude, or OpenAI via a single env var; no code changes

---

## Quick Start (under 30 minutes)

### Prerequisites

- macOS with [Homebrew](https://brew.sh)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Ollama](https://ollama.ai) — `brew install ollama`
- Python 3.9+
- Xcode 16+ — for the iOS/macOS client

### 1. Clone and configure

```bash
git clone <repo-url> FruitcakeAI
cd FruitcakeAI
cp .env.example .env
```

Edit `.env` — the only required changes for a local setup:

```env
SECRET_KEY=change-me-to-a-random-string
DATABASE_URL=postgresql+asyncpg://fruitcake:fruitcake@localhost:5432/fruitcake_v5
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

### 2. Pull the LLM

```bash
ollama pull qwen2.5:14b
ollama serve   # runs in background on port 11434
```

> **M1 Max 64GB**: `qwen2.5:14b` is the verified default. `qwen2.5:32b` (~20GB) is a capable step-up if you close other apps first. `llama3.3:70b` (~43GB) crashes at runtime on this hardware — avoid it.

### 3. Start the backend

```bash
./scripts/start.sh
```

This script:
1. Starts the Docker postgres container (`pgvector/pgvector:pg16`)
2. Waits for Ollama health check
3. Activates existing `.venv` (or creates and installs on first run)
4. Runs database migrations and seeds default users
5. Starts the FastAPI server on `http://localhost:8000`

Default users — **change these passwords before using on a shared network:**

| Username | Password | Role |
|----------|----------|------|
| admin | changeme123 | admin |
| parent | changeme123 | parent |
| kid | changeme123 | child |
| guest | changeme123 | guest |

### 4. Verify the backend

```bash
curl http://localhost:8000/health
# → {"status": "ok"}

curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme123"}'
# → {"access_token": "eyJ...", "token_type": "bearer"}
```

### 5. Connect the client

Open `FruitcakeAI_iOS` in Xcode, set **Settings → Server URL** to `http://localhost:8000`, and log in with any seed user.

**Try it**: send any message — the assistant responds from the local LLM. Upload a PDF via the Library tab, then ask about its contents. Create a task via the Inbox tab and watch it run on schedule.

---

## Running Tests

```bash
source .venv/bin/activate
pytest -q
# 133 passed in ~30s — no running PostgreSQL required
```

---

## Architecture

```
FruitcakeAI_iOS (iPhone / macOS)
  ├── AuthManager       JWT auth, Keychain storage
  ├── APIClient         REST calls, multipart upload
  ├── WebSocketManager  Streaming chat tokens
  ├── OnDeviceAgent     FoundationModels fallback (offline mode)
  └── SwiftData         Local message cache

FruitcakeAI backend
  ├── app/api/          HTTP + WebSocket endpoints
  ├── app/agent/        LiteLLM agent loop, tool dispatch, persona system
  ├── app/autonomy/     TaskRunner, Scheduler, ApprovalGate, APNs push
  ├── app/memory/       MemoryService — 3-tier retrieval, dedup, pgvector
  ├── app/rag/          pgvector + BM25 hybrid retrieval
  ├── app/mcp/          MCP tool registry (calendar, web, RSS, Docker servers)
  └── app/db/           PostgreSQL models, async sessions, Alembic migrations
```

**Agent-first**: the LLM orchestrates tools — no hand-written routing rules.  
**Local by default**: Ollama + HuggingFace embeddings. No API keys required to get started.  
**Model-agnostic**: swap LLM backends via a single env var. Ollama, Claude, and OpenAI are all supported.

---

## API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/auth/register` | — | Create account |
| POST | `/auth/login` | — | Get JWT token |
| GET | `/auth/me` | user | Current user profile |
| GET | `/chat/personas` | user | Available personas |
| POST | `/chat/sessions` | user | Start a chat session |
| GET | `/chat/sessions` | user | List active sessions |
| GET | `/chat/sessions/{id}` | user | Session detail + messages |
| POST | `/chat/sessions/{id}/messages` | user | Send a message (REST) |
| WS | `/chat/sessions/{id}/ws` | user | Streaming chat |
| DELETE | `/chat/sessions/{id}` | user | Archive session |
| GET | `/library/documents` | user | List uploaded documents |
| GET | `/library/query` | user | RAG search |
| POST | `/tasks` | user | Create a task |
| GET | `/tasks` | user | List tasks |
| GET | `/tasks/{id}` | user | Task detail |
| PATCH | `/tasks/{id}` | user | Update / approve / reject |
| DELETE | `/tasks/{id}` | user | Cancel task |
| POST | `/tasks/{id}/run` | user | Manual trigger (dev) |
| POST | `/tasks/{id}/reset` | user | Recover a task stuck in running after restart |
| GET | `/tasks/{id}/audit` | user | Tool-call log for last run |
| GET | `/memories` | user | List memories |
| POST | `/memories` | user | Create memory (admin/testing) |
| PATCH | `/memories/{id}` | user | Update importance or tags |
| DELETE | `/memories/{id}` | user | Deactivate memory |
| POST | `/devices/register` | user | Register APNs device token |
| DELETE | `/devices/{token}` | user | Remove device token |
| POST | `/webhooks/trigger/{key}` | — | Trigger webhook by secret key |
| GET | `/webhooks` | user | List webhook configs |
| POST | `/webhooks` | user | Create webhook |
| DELETE | `/webhooks/{id}` | user | Delete webhook |
| GET | `/admin/metrics` | admin | Prometheus metrics |
| GET | `/admin/health` | admin | Backend health |
| GET | `/admin/tools` | admin | Registered MCP tools |
| GET | `/admin/users` | admin | List all users |
| POST | `/admin/users` | admin | Create user |
| PATCH | `/admin/users/{id}` | admin | Update user role/persona/scopes |
| GET | `/admin/audit` | admin | Agent tool-call audit log |
| GET | `/admin/task-runs` | admin | Task run history with tool calls |

---

## Configuration

| Topic | Guide |
|-------|-------|
| Switch LLM (Ollama → Claude → OpenAI) | [Docs/LLM_BACKENDS.md](Docs/LLM_BACKENDS.md) |
| Add MCP tools (config-only, no code) | [Docs/ADDING_MCP_TOOLS.md](Docs/ADDING_MCP_TOOLS.md) |
| Customize personas per user | [Docs/PERSONA_SYSTEM.md](Docs/PERSONA_SYSTEM.md) |
| Full roadmap | [Docs/FruitcakeAi Roadmap.md](Docs/FruitcakeAi Roadmap.md) |

---

## Security Notes

FruitcakeAI is designed to run on a trusted local network. Before sharing access:

- **Change default passwords** — seed users use `changeme123`; update them before any shared use
- **Network exposure** — `start.sh` runs Uvicorn on `0.0.0.0:8000`; restrict host firewall rules or put a reverse proxy in front before exposing beyond your local network
- **Secrets stay in `.env`** — API keys and config live only in `.env`, which is git-ignored and never committed
- **Uploaded data stays local** — documents and derived chunks are stored under `storage/`, also git-ignored

---

## Contributing

FruitcakeAI is in active development. Issues and pull requests are welcome. If you find a bug or have a feature suggestion, open an issue — include your hardware, OS, Ollama version, and LLM model.

See [Docs/FruitcakeAi Roadmap.md](Docs/FruitcakeAi Roadmap.md) for what's planned and what's in progress.

---

## Utility Scripts

```bash
./scripts/reset.sh        # drop DB, recreate tables, reseed users
./scripts/stop.sh         # stop API + postgres
./scripts/stop.sh --all   # also stop Ollama
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
│   ├── auth/           JWT auth, user registration
│   ├── autonomy/
│   │   ├── approval.py ApprovalRequired gate (ContextVar-scoped)
│   │   ├── push.py     APNs push
│   │   ├── runner.py   TaskRunner — isolated agent sessions, retry logic
│   │   └── scheduler.py cron/interval/ISO parser, next-run calculator
│   ├── db/
│   │   ├── models.py   User, Task, Memory, DeviceToken, WebhookConfig, …
│   │   └── migrations/
│   ├── memory/         MemoryService — 3-tier retrieval, dedup, access tracking
│   ├── mcp/            MCP registry + internal servers
│   └── rag/            pgvector RAG service + hybrid retriever
├── config/
│   ├── mcp_config.yaml     MCP server definitions
│   ├── personas.yaml       Persona definitions
│   └── users.yaml          Seed users
├── Docs/               Guides and roadmap
├── tests/              133 tests, SQLite in-memory (no DB required)
├── scripts/
│   ├── start.sh        One-command startup
│   ├── stop.sh         Stop local services
│   └── reset.sh        Wipe and reseed
└── docker-compose.yml  PostgreSQL + pgvector
```
