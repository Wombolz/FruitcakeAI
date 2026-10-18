# Coding Backend Foundation

## Status

The read-only backend foundation and authenticated HTTP/event layer are implemented on `codex/coding-runtime-phase-0`, following the Phase 0 gate authorized on October 16, 2026. The [coding API contract](coding_api.md) documents workspace grants, sessions, runs, approvals, stop/steering, and reconnectable SSE. The [first native read-only workspace](coding_client.md) passed the operator's reported manual checks. The follow-on [repository review](coding_repository_review.md) adds independent live Git status/diff inspection, not write-enabled execution.

The supported deployment is one Fruitcake backend process with operator-controlled local repositories. The existing workspace guard is application-level confinement; the [Phase 0 report](coding_runtime_phase_0.md) documents its limits. Write mode and arbitrary commands require a separate gate.

## Components

- `app/coding_runtime/contracts.py`: provider-neutral runtime, capabilities, model, start request, prompt, and event contracts.
- `app/coding_runtime/pi_runtime.py`: translates those contracts to the private Pi RPC client; Pi response/disposition types remain in the adapter.
- `app/coding_runtime/service.py`: workspace grants, session/run state, authorization, persistence, event replay, approval decisions, and process supervision.
- `app/coding_runtime/bootstrap.py`: optional application lifecycle wiring; constructing the service starts no runtime process.
- `app/api/coding.py`: authenticated, owner-scoped HTTP endpoints and durable SSE replay; explicit output schemas exclude private runtime fields.
- `app/coding_runtime/repository.py`: bounded read-only Git status and per-file staged/unstaged/untracked previews, with no model/runtime dependency or repository mutation.
- Migration `052_coding_sessions`: creates coding workspaces, sessions, runs, events, and approval records.
- `app/coding_runtime/checkouts.py` and migration `053_coding_checkouts`: private managed-worktree storage, immutable session baseline/identity/state, detached provisioning, validation, clean-only removal, and restart/cancellation recovery. See [managed checkouts](coding_checkouts.md).

Coding sessions have their own identifiers and tables. An optional `project_id` provides organization. Project membership grants no filesystem access, and ordinary chat rows are not repurposed for coding sessions.

## Authorization

An active operator in `ADMIN_ROLES` explicitly authorizes an existing canonical repository directory for a user. The resulting coding-workspace grant is separate from general host-root read grants. Broad roots and missing directories are rejected. Replacing an approved root with an escaping symlink invalidates it.

Session creation and every submission check the active user, grant ownership, grant activity, model availability, and per-user model policy. Session creation also checks ownership of an optional active project. Authorization is rechecked immediately before execution and before steering or approval delivery. Revoking a workspace through the service stops active work and closes its retained runtime.

The first Pi adapter supports configured local, tool-capable text model profiles. It selects the existing Fruitcake local endpoint and passes only its selected credential to Pi. Profile tool allowlists and denylists restrict the enabled inspection tools. Missing `rg` or `fd`/`fdfind` helpers remove the corresponding tools rather than causing automatic downloads.

## Runtime Supervision

- At most two runtime slots are retained by default, counting startup, active work, and idle processes.
- An idle slot can be reclaimed immediately when another session needs capacity.
- Idle processes expire after five minutes by default.
- Each coding session permits one active run; the database also has a partial unique index enforcing this rule.
- Client request identifiers make repeated submissions idempotent; reusing an identifier for different content is rejected.
- Stop targets one run identifier and cannot cancel a later run in the same session.
- Cancellation during startup still finalizes the durable run.
- Startup and execution deadlines, event counts, payload bytes, and response sizes are bounded.
- Tool display previews are trimmed to the serialized JSON byte budget, including escaping and metadata, with an explicit truncation notice. The runtime's actual tool result is unchanged. Long visible snapshots are fragmented rather than silently clipped.
- Small visible text deltas are batched, and redundant reasoning-token status ticks do not consume the durable event budget. Status transitions remain available without storing reasoning content.
- Failed runtimes are closed before reuse. A runtime whose cleanup fails retains a quarantined slot until cleanup succeeds.
- Application shutdown closes coding runtimes before MCP shutdown, local-model release, and database disposal.
- Ollama models used by the coding adapter join Fruitcake's existing shutdown-release tracking.

These limits are process-local. Multiple application workers need a distributed runtime owner/lease design before being supported.

## Durable State And Events

`coding_sessions` stores owner, project, workspace, selected model profile, persona snapshot, runtime kind, and context generation. `coding_runs` stores submitted content, final visible response, lifecycle status, error classification, idempotency key, and event cursor. Runtime credentials and Pi protocol payloads are excluded.

Run states are `starting`, `running`, `waiting_approval`, `stopping`, and terminal `completed`, `failed`, `cancelled`, or `interrupted`. On backend restart, abandoned active runs become `interrupted` with a `backend_restart` classification. Shutdown interruption is distinguished from explicit user cancellation.

Events have a schema version and a unique monotonically increasing sequence per run. A consumer can replay records after a cursor and optionally follow an active run without retaining an unbounded subscriber queue. The final terminal event is committed with the run's final state. Provider-specific event names and reasoning snapshots are excluded from durable events.

Pi context is ephemeral. When a process is replaced, its context generation increases. The service reconstructs excerpts from up to three successful runs, including recent steering/follow-up inputs and visible final answers, with a 15,000-character overall limit. A `session.ready` event identifies this reconstruction. Tool transcripts and interrupted runs are not silently presented as fully restored context.

## Approvals

Optional read confirmations become durable coding approval records bound to a run and a runtime request. Consumers see the durable approval identifier in the event stream. Only the owning user can resolve a pending, unexpired request for the active run; repeated or late answers are rejected.

The decision is committed before delivery to the runtime. A recorded approval means the user approved the operation; it does not assert that delivery or execution succeeded. Remaining pending requests are expired or cancelled during run finalization or restart recovery. Pi's gate remains responsible for pausing the actual read before execution.

## Configuration

The feature is disabled by default. Apply migrations through 054 through the normal bootstrap/migration workflow before enabling this revision. Pi 1.3.0 must be deliberately installed by the operator; application startup never installs it.

```dotenv
CODING_RUNTIME_ENABLED=false
CODING_RUNTIME_EXECUTABLE=/absolute/path/to/pi
CODING_MAX_ACTIVE_SESSIONS=2
CODING_RUNTIME_IDLE_SECONDS=300
CODING_RUN_TIMEOUT_SECONDS=1800
CODING_REQUIRE_READ_APPROVAL=false
```

The executable setting must be an absolute path. Model access still uses existing Fruitcake model profiles and per-user policy. When enabled, the application exposes the internal service as `app.state.coding_sessions`; when disabled, that value is `None`.

The execution budget defaults to 30 minutes (`CODING_RUN_TIMEOUT_SECONDS=1800`), excluding time awaiting human approvals. It is independently configurable; for example, `3600` allows one hour of execution. Restart the backend after changing it. Idle-runtime eviction applies only between runs, not to an active approval wait. Startup, operations, and Stop/cleanup retain their deadlines. New approvals do not expire; see [approval waits](coding_approval_wait.md) for migration 057 and native-client compatibility.

Internal callers use `approve_workspace`, `create_session`, `submit`, `get_run`, `events`, `stop`, `steer`, `list_approvals`, `resolve_approval`, `close_session`, and `revoke_workspace`. Runtime and database factories are injected, so session behavior can be tested without installing Pi.

## Verification

Final verification: 144 coding-runtime/API tests passed, including checks against the installed Pi 1.3.0 executable, authorized numeric model selection, serialized output budgets, lossless answer fragmentation, token-event coalescing, live Git review, managed worktrees, and migration 053. The 23 targeted application lifecycle, router/auth integration, project, host-root, and model-release regressions also passed their assertions in this slice. The broader application test process still exhibits its existing background-thread teardown hang after the passing summary; it was interrupted after all assertions completed. The isolated coding suite exits normally.

The coding suite tests protocol behavior, workspace confinement, actual installed-Pi tool-policy enforcement, model/profile selection, user isolation, grant/model revocation, idempotency, process limits, stop races, crashes, timeouts, event bounds/replay, idle context reconstruction, approvals, restart recovery, and migration/ORM consistency. Its 21 HTTP/streaming tests additionally verify real bearer authentication, operator-only grants, strict input schemas, safe approval serialization, authorized numeric model options, cursor validation/replay, heartbeats, authentication connection release, control error redaction, account reauthorization, actual ASGI client disconnection without aborting a run, repository ownership/revocation, and Stop responsiveness during inspection. Thirteen repository tests cover real Git staged/unstaged/untracked content, literal paths/renames, binaries, unborn HEAD, parent-root refusal, link restrictions, disabled execution helpers, output/diagnostic bounds, and process reaping. Separate application tests verify router registration, the production error envelope, and cleanup order for normal exit and an exception inside the application lifespan.

```bash
.venv/bin/python -m pytest -q --confcutdir=tests/coding_runtime tests/coding_runtime
FRUITCAKE_TEST_PI=/absolute/path/to/pi .venv/bin/python -m pytest -q --confcutdir=tests/coding_runtime tests/coding_runtime
.venv/bin/python -m pytest -q tests/test_coding_lifecycle.py tests/test_projects.py tests/test_host_root_access.py tests/test_local_model_lifecycle.py
```

Installed-Pi tests use a temporary scripted loopback model. They make no hosted model calls. Database tests use disposable SQLite databases; the live development database is not migrated by the test suite.

## Next Slice

The operator reported the live approvals/Stop, interrupted-session recovery, full-window layout, inspector/top-diff presentation, and managed checkout path working on October 16, 2026. [Host-approved edits](coding_approved_edits.md) are now implemented as a bounded follow-on: migration 054 adds explicit checkout-only opt-in and host-derived durable change outcomes. The runtime proposes; the host applies only after exact review acknowledgement and final validation. Soak this path before widening mutation scope. Shell, arbitrary network access, publishing, and stronger containment remain deferred.
