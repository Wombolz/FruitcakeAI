
# FruitcakeAI — Distributed Task Processing Architecture

**Status**: Architectural direction — implement incrementally as scale demands  
**Created**: March 2026  
**Related**: Phase 7 roadmap, `Docs/OpenClaw_Skill_Converter_Spec.md`

---

## Overview

FruitcakeAI's separation of the application layer from the database layer makes
horizontal scaling a natural extension of the existing architecture — not a
redesign. Because all shared state lives in PostgreSQL, multiple FruitcakeAI
instances can point at the same database and operate as independent workers with
no coordination overhead beyond what Postgres already provides.

This document describes the target distributed architecture, the hardware model
it enables, and the implementation work required to get there.

---

## Core Insight

A standard single-server FruitcakeAI deployment looks like this:
```
Users → FruitcakeAI instance → PostgreSQL
```

Because the instance is stateless (all session, memory, task, and skill state
lives in Postgres), this extends cleanly to:
```
Users → Central server (auth + gateway) → PostgreSQL ← Worker nodes (N)
```

No architectural changes to the database. No changes to the agent loop. Workers
are just additional FruitcakeAI instances with their task polling enabled and
their user-facing API disabled.

---

## Components

### Central server

**Role**: Single entry point for all users. Handles authentication, session
management, API routing, and task submission. Does not execute tasks.

**Hardware**: Modest — a Mac mini or equivalent. CPU and memory requirements are
low because no inference runs here.

**Responsibilities**:
- User login and JWT token issuance
- Request validation and API surface (chat, library, admin)
- Task submission: writes task rows to the shared `tasks` table with
  `status = pending`
- Results retrieval: reads completed task output from the shared DB
- Skill management and admin diagnostics
- Push notification dispatch (APNs)

**What it does not do**: execute tasks, run inference, or load models.

---

### Worker nodes

**Role**: Poll the task queue, claim work atomically, run inference, write
results back to the shared database.

**Hardware**: Mix of Mac minis and larger machines depending on model assignment.
Each worker runs Ollama locally with its assigned model pre-loaded.

**Responsibilities**:
- Poll `tasks` table for `status = pending` work
- Claim tasks atomically (no two workers claim the same task)
- Load skill context and user memory from the shared DB
- Execute the task against the locally running model
- Write results to `task_run_artifacts` and update task status to `complete`
- Report heartbeat to shared DB for health monitoring

**What workers do not expose**: no user-facing API, no auth endpoints, no admin
surface. Invisible to users.

---

### Shared PostgreSQL database

**Role**: Single source of truth for all state across all instances.

**Hosted**: Dedicated machine or managed instance. Separate from both the
central server and worker nodes.

**Key tables used by distributed coordination**:

| Table | Role |
|---|---|
| `tasks` | Task queue — status transitions drive worker coordination |
| `task_runs` | Execution records per worker attempt |
| `task_run_artifacts` | Task output written by workers |
| `skills` | Skill definitions injected into worker context |
| `memories` | Per-user memory retrieved by workers before each run |
| `chat_sessions` | Session state managed by central server |
| `users` | Auth and persona config read by both central and workers |

---

## Task Queue Pattern

Postgres handles worker coordination natively. No Redis, no external queue,
no message broker required.

### Claiming a task

Each worker polls on a configurable interval and claims the next available task
using `SELECT ... FOR UPDATE SKIP LOCKED`:
```sql
SELECT id, user_id, instruction, persona, profile
FROM tasks
WHERE status = 'pending'
  AND (next_run_at IS NULL OR next_run_at <= now())
ORDER BY next_run_at ASC NULLS FIRST
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

`SKIP LOCKED` means two workers polling simultaneously will never claim the same
row. The first to acquire the lock claims the task; the second skips it and moves
on. Postgres handles this atomically with no application-level locking required.

### Status transitions
```
pending → claimed → running → complete
                           ↘ failed → pending (retry, up to max_retries)
```

Workers update status at each transition. The central server and admin endpoints
read status to surface progress to users.

### Heartbeat

Workers write a `last_heartbeat_at` timestamp to a `worker_registry` table on a
configurable interval (e.g. every 30 seconds). The central server monitors this
table and can surface stale workers in admin diagnostics. A worker that misses
N consecutive heartbeats can have its claimed tasks reset to `pending` for
reprocessing.

---

## Hardware Model

### Model allocation strategy

Not all tasks require the same model. Workers can be assigned a model tier, and
tasks can carry a `model_size` hint that the scheduler uses for routing.

| Worker type | Hardware | Model | Task types |
|---|---|---|---|
| Small fleet | Mac mini (M-series) | qwen2.5:14b or equivalent | Routine tasks, RSS, calendar, summarization, skill execution |
| Large instance | Mac Studio or equivalent | 70B model or Claude via cloud routing | Complex reasoning, ambiguous judgment, multi-step orchestration |
| Cloud routing | External API | Claude (Phase 6) | Overflow or tasks explicitly flagged for cloud judgment |

### Scaling path
```
Phase 1: One worker, one central server, one Postgres instance
         → Proves the architecture with minimal hardware

Phase 2: Add worker nodes as queue depth grows
         → Each new Mac mini picks up work automatically
         → No reconfiguration required

Phase 3: Assign model tiers
         → Add task complexity scoring
         → Route tasks to appropriate worker by model_size hint
         → Large instance handles reasoning-heavy work

Phase 4: Geographic distribution (if needed)
         → Workers co-located near the Postgres instance reduce latency
         → Central server can remain accessible remotely
```

### Cost profile

A single Mac mini M4 (roughly $700) running qwen2.5:14b handles parallel task
execution without competing with other users for CPU. Compare to a single
overprovisioned server where every user and every task competes for the same
resources. Horizontal scaling with commodity hardware is both cheaper and more
resilient than vertical scaling a single powerful machine.

---

## Implementation Work

### Phase 1 — Worker mode flag (low effort)

Add a `WORKER_MODE` environment variable. When set:
- Disable all user-facing API routes
- Enable task poller with configurable poll interval
- Register in `worker_registry` table on startup
- Begin polling `tasks` table

Central server runs with `WORKER_MODE=false` (default). Worker nodes run with
`WORKER_MODE=true`.

No changes to the agent loop, task runner, or database schema beyond adding
`worker_registry`.

### Phase 2 — Worker registry and health monitoring

New table:
```python
class WorkerNode(Base):
    __tablename__ = "worker_registry"

    id = Column(String, primary_key=True)     # hostname or UUID
    model = Column(String, nullable=False)    # model this worker runs
    model_size = Column(String)               # "small" | "large"
    last_heartbeat_at = Column(DateTime(timezone=True))
    tasks_completed = Column(Integer, default=0)
    tasks_failed = Column(Integer, default=0)
    status = Column(String, default="active") # active | stale | offline
```

Admin endpoint: `GET /admin/workers` — lists all registered workers, their model
assignment, heartbeat recency, and task counts.

### Phase 3 — Task routing by model size

Add `model_size_hint` field to `tasks` table (`small` | `large` | `any`).

Workers filter the task queue by their assigned model size:
```sql
WHERE status = 'pending'
  AND (model_size_hint = 'any' OR model_size_hint = :my_model_size)
```

The agent or task scheduler sets `model_size_hint` based on task complexity
scoring (token estimate, tool count, step count, or explicit user flag).

### Phase 4 — Stale task recovery

A background job on the central server (or any worker) runs on a configurable
interval and resets tasks claimed by stale workers back to `pending`:
```sql
UPDATE tasks
SET status = 'pending', claimed_by = NULL
WHERE status = 'claimed'
  AND claimed_at < now() - interval '10 minutes'
  AND claimed_by IN (
    SELECT id FROM worker_registry
    WHERE last_heartbeat_at < now() - interval '5 minutes'
  );
```

This ensures no task is permanently stuck if a worker crashes mid-execution.

---

## Connection Pooling

Multiple workers connecting simultaneously to the same Postgres instance requires
connection pool discipline. Recommended approach:

- Each worker uses `asyncpg` with a pool size of 5–10 connections (tuned to
  workload)
- Central server uses a separate pool sized for API concurrency
- Add `pgbouncer` in front of Postgres if total connections become a bottleneck
  (typically only needed at 10+ workers)
- Postgres `max_connections` should be set with headroom: `(workers × pool_size)
  + central_pool + admin_buffer`

---

## Security Considerations

- Workers connect to Postgres with a dedicated `fruitcake_worker` DB role that
  has read/write on `tasks`, `task_runs`, `task_run_artifacts`, `memories`,
  `skills`, and `users` — but not on auth tables or admin-only tables.
- Central server connects with `fruitcake_api` role that has full access.
- Workers should not be exposed to the public network. They connect outbound to
  Postgres and to Ollama (localhost). No inbound ports required.
- Worker-to-central communication is not needed — all coordination happens
  through the shared database.
- If workers run on separate physical machines, use a VPN or private network
  for Postgres connectivity. Never expose Postgres to the public internet.

---

## Failure Modes and Resilience

| Failure | Impact | Recovery |
|---|---|---|
| Worker crashes mid-task | Task stuck in `claimed` | Stale task recovery job resets to `pending` |
| Worker offline entirely | Reduced throughput | Remaining workers absorb queue |
| Central server down | Users cannot submit tasks | Workers keep draining existing queue |
| Postgres down | All instances pause | Reconnect with exponential backoff on restore |
| Model load failure on worker | Worker skips tasks it cannot serve | Task remains `pending` for another worker |

The architecture degrades gracefully. A central server outage does not lose
queued work. A worker outage does not affect other workers. Postgres is the only
single point of failure, and it can be addressed with a hot standby replica when
uptime requirements demand it.

---

## Relation to Existing Architecture

This architecture is a natural extension of what is already built. The task
runner, scheduler, execution profile resolver, and approval gate all run
unchanged on workers. The `resolve_execution_profile` seam from Phase 5.4 is
the right abstraction layer for worker-aware routing — workers can eventually
advertise their capabilities and the resolver can factor that in.

The skills system (Phase 5.6.5) fits cleanly: skills are read from the shared DB
by every worker, so a skill installed once is immediately available to all nodes
in the cluster.

Phase 7's filesystem and shell MCP tools are worker-local — each worker has its
own sandboxed `/workspace/{user_id}/` directory. Cross-worker workspace sharing
is not required and should not be implemented.
