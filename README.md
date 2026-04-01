# FruitcakeAI

**A self-hosted AI agent platform for households and small teams.**
**Local-first. Private by default.**

FruitcakeAI is a complete autonomous agent runtime — persistent memory, task scheduling, multi-user access control, hybrid RAG document pipeline, MCP extensibility, and a native Swift client, all wired together and working. There's no SaaS dependency, no ongoing API cost, and no vendor lock-in. You bring your own models via Ollama, or swap in Claude or OpenAI with one environment variable.

It's built for self-hosters, homelab tinkerers, developers, and small teams who want real autonomous AI capabilities without giving up ownership of their data or their stack. The foundation is fully working. What you build on top of it is up to you.

<img width="1358" height="852" alt="Screenshot 2026-03-30 at 6 58 01 PM" src="https://github.com/user-attachments/assets/f1b5ddd8-00b7-4926-8083-2c33c43950b0" />

<img width="1358" height="852" alt="Screenshot 2026-03-30 at 4 44 07 PM" src="https://github.com/user-attachments/assets/8043159e-575e-46cd-953a-d3d06d0b6f89" />

<img width="1358" height="852" alt="Screenshot 2026-03-30 at 4 46 33 PM" src="https://github.com/user-attachments/assets/8f85eed4-7bc5-4a82-beae-460536f56e28" />



## Project Status

FruitcakeAI is currently a quiet open alpha.

That means:

- behavior may still change between releases
- setup is still opinionated and macOS-first
- schema migrations are part of normal upgrade flow
- some integrations are stable enough for testing, not guaranteed production-hard
- public bug reports are welcome, but the project is not yet open to broad outside co-development

The repository is available publicly, but it is not being actively pushed as a broad launch. Treat it as a serious self-hosted alpha under active development, not a polished production platform.

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

**Explicit routing and trust boundaries.** `Automatic` chat uses the routing classifier and your reasoning preference. `Fast` forces the simpler chat path. `Deep` forces orchestrated chat. Tasks without an override use the existing `TASK_SMALL_MODEL` / `TASK_LARGE_MODEL` policy; tasks with an explicit model override use that model for all LLM stages.

**Extensible via MCP — additive integrations.** New capabilities arrive as MCP servers in a config file. The agent discovers them at startup. Nothing in core depends on any optional provider. Everything added is additive; nothing is load-bearing.

**Multi-user and safe by default.** Role-based personas with scoped tool access. Per-persona content filtering and blocked tool lists. Approval gates before irreversible actions. Active-hours windows that prevent the autonomous agent from running at 3am.

**No speculative infrastructure.** The codebase contains only what the current use case requires. No Kubernetes, no distributed queues, no ELK stack. Complexity is added when real friction demands it, not in anticipation of scale that doesn't exist yet.

## Known Limits

- The primary operator path is still local/self-hosted on trusted hardware and trusted networks.
- The backend is not positioned as internet-hardened by default.
- The native client story is still split from the backend repository.
- Setup and testing are currently macOS-first.
- Apple ID / CalDAV and Google Calendar identity are currently shared application-wide rather than isolated per user.
- Per-user access to those integrations is planned, but it is not the current development priority.
- Some workflows are well-soaked for daily use; others are still alpha-grade and may shift quickly.
- Internal planning and maintainer workflow are intentionally not the same thing as the public contribution surface.

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

## Contributing

Bug reports and reproducible issues are welcome.

General outside code contributions are not broadly open yet while the project is still in an alpha-stage architecture churn phase.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the current contribution policy.
See [SUPPORT.md](SUPPORT.md) for where to file bugs, setup issues, and security concerns.

### LLM Routing Trust Boundary

- Local models keep prompts, task context, and tool results on your hardware.
- Choosing a cloud model, or routing a task/chat turn to a cloud model, sends the current turn context to that provider.
- Stored secrets stay server-side. Fruitcake resolves them in backend-owned API adapters and does not place plaintext secret values into model prompts.
- Structured external APIs are executed server-side through approved adapters; they are not run through shell improvisation.
- When research-heavy chat cannot complete reliably, Fruitcake now fails with a bounded explanatory message instead of the generic fallback.

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
- WebSocket chat no longer replays stale completed sends from reused server-side message state

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
- Linked-folder ingestion is restricted to operator-approved roots via `LINKED_SOURCE_ALLOWED_ROOTS`
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

### 4. Put it behind HTTPS before other people log in

For local development, `http://localhost:30417` is still fine.

### 3.5 Optional: enable reviewed linked-folder roots

Folder linking is disabled by default unless you explicitly allow roots in `.env`:

```env
LINKED_SOURCE_ALLOWED_ROOTS=/Users/you/Documents/FruitcakeLibrary,/Users/you/Shared/FruitcakeImports
```

Only folders inside those roots may be linked through `/library/link-folder`. Regular file upload is unaffected.

For any shared-network or tester-facing use, Fruitcake should sit behind HTTPS.
This repo now ships a Caddy-based front door:

```bash
brew install caddy
FRUITCAKE_SITE_ADDRESS=fruitcake.localhost ./scripts/start_https.sh
```

Then trust Caddy's local CA once on the machine running the client:

```bash
sudo caddy trust
```

Important:
- the backend stays on `127.0.0.1:30417`
- Caddy becomes the user-facing HTTPS entrypoint
- for a public-domain rollout, edit `Caddyfile` and remove `tls internal` so Caddy can obtain a public certificate automatically

### 5. Open the Swift app (optional)

1. Open the `FruitcakeAI_Client` Swift project in Xcode
2. Build and run (`⌘R`)
3. Settings → Server URL:
   - local dev: `http://localhost:30417`
   - shared-network/tester use: `https://<your-hostname>`
4. Log in with any seed user

Upload a document via the Library tab, then ask about it.

---

## License

FruitcakeAI is licensed under the GNU Affero General Public License v3.0.

See [LICENSE](LICENSE) for the full text.

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
