# Read-Only Coding API

Approval-wait follow-on: migration 057 makes approval `expires_at` nullable in REST
and SSE. Null means no expiry; finite legacy deadlines are still enforced. Human
wait time is excluded from the run's execution budget. Rebuild the native client
before using the upgraded backend; see [approval waits](coding_approval_wait.md).

Follow-on: [workspace lifecycle API additions](coding_workspace_lifecycle.md#api-additions)
require migration 056 and add reusable checkouts, conversation controls, per-run
model identity, and explicit reviewed Git delivery. Source sessions remain read-only;
committing and source application are host-owned user actions, not agent tools.

Follow-on: [approved offline container commands](coding_container_commands.md)
adds migration 055, separate `command_mode` opt-in, exact-hash command approval,
owner-scoped command history, and additive command lifecycle events. Host Bash
and general networking remain unavailable; existing sessions default to disabled.

Implemented October 16, 2026, on `codex/coding-runtime-phase-0`. This is the HTTP contract for the Mac coding workspace, not a new ordinary-chat mode. See [backend configuration and security limits](coding_backend_foundation.md) before enabling it.

## Availability And Authentication

All routes use the existing `Authorization: Bearer <access-token>` header. Refresh tokens, query-string tokens, and unauthenticated streams are not accepted. Roles and account activity come from the database, not the role claim in the token. Guest and restricted accounts cannot use coding routes. Session, run, approval, and workspace reads are owner-scoped; being an operator does not grant access to another user's coding conversation.

`GET /coding/status` returns:

```json
{"enabled":true,"mode":"read_only","event_schema_version":1,"managed_checkouts":true,"approved_edits":true}
```

`enabled` means the backend service is enabled and started. It does not assert that Pi is installed, a model is reachable, or a workspace has been granted. Runtime startup occurs only on submission; startup failures become durable failed runs with an `error_code`.

When disabled, status still returns HTTP 200 for an authorized user; other coding routes return HTTP 503 with `coding_unavailable`. Apply migrations through 054 before enabling this backend revision. `managed_checkouts` and `approved_edits` are additive capabilities, not blanket permissions. Older backends omit them and clients should hide unsupported controls. The live development database is not automatically migrated by these tests.

Explicit host-applied changes, per-session `edit_mode`, complete approval previews, and persisted run change records are specified in [Host-Approved Coding Changes](coding_approved_edits.md). Source sessions/default mode remain read-only. `POST /coding/sessions/{id}/edit-mode` accepts only `{"enabled":true|false}`; `GET /coding/runs/{id}/changes` reads durable owner-scoped outcomes. Shell remains disabled.

## Workspaces

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/coding/workspaces` | List the current user's grants, including revoked entries with `is_active:false`. |
| POST | `/coding/workspaces` | Operator-only authorization of an existing repository directory. |
| POST | `/coding/repositories` | Operator-only creation and authorization of a new local Git repository; returns 201. Never initializes or overwrites an existing directory. |
| DELETE | `/coding/workspaces/{workspace_id}` | Operator-only revocation, stopping active work and closing retained runtimes. Returns 204; preserves history. |

Grant body:

```json
{"user_id":2,"path":"/absolute/path/to/repository","require_repository":true}
```

The native repository chooser sends `require_repository:true`: validate the exact Git root and an existing commit before granting access. Empty folders, subfolders, bare repositories, and repositories without an initial commit are rejected immediately. The optional flag defaults to false for compatibility with existing directory-only read grants.

Repository creation takes `{"user_id":2,"path":"/absolute/existing/parent/NewRepository"}`. The parent must exist; the final path must not exist, including as a symlink. The backend creates a private directory, initializes branch `main` without inherited templates/hooks or global Git configuration, and makes an empty `Initial commit` using `Fruitcake <fruitcake@localhost>` without signing. No remote is configured and no source files are staged. Authorization follows successful Git validation; no session, runtime, checkout, edits, or commands are enabled automatically. Existing paths return 409. A partial Git setup failure leaves the new folder for inspection, without a grant; it never recursively deletes files or automatically retries. Restart the backend and rebuild the client for this additive API; no migration is required.

The Mac client uses `NSOpenPanel` for existing repositories and `NSSavePanel` to select a new repository name/location, with a separate authorization/creation confirmation. Cancelling selection makes no backend call. These panels browse the client Mac, not a remote backend filesystem; backend and client must share the same machine/path namespace for this flow.

Workspace responses include `id`, `user_id`, `canonical_path`, `is_active`, and `created_at`. The client selects an active grant by ID, not by submitting an arbitrary path with a prompt. A project association never grants filesystem access. Repeated authorization of the same canonical path for the same owner reuses its grant.

`GET /coding/models` returns authorized local, tool-capable numeric profile options, for example `[{"id":1,"label":"Local Qwen","model_id":"ollama_chat/example"}]`. It applies per-user profile access and inspection-tool policy, excludes unsupported provider families, and starts no runtime. Use this endpoint for coding selection: generic `/llm/models` exposes public string identities, not coding's numeric foreign keys.

## Sessions And Runs

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/coding/sessions?limit=50&offset=0` | Current user's sessions, most recently updated first. Includes closed sessions. |
| POST | `/coding/sessions` | Create an idle session; returns 201. Does not start a runtime. |
| GET | `/coding/sessions/{session_id}` | Session metadata. |
| POST | `/coding/sessions/{session_id}/close` | Close an inactive session and release its runtime; returns 204. Rejects an active run. |
| GET | `/coding/sessions/{session_id}/runs?limit=50&offset=0` | Runs, newest first, including prompts and final visible responses. |
| POST | `/coding/sessions/{session_id}/runs` | Accept a prompt and return its durable run, HTTP 202. |
| GET | `/coding/runs/{run_id}` | Current durable run state and final response. |
| POST | `/coding/runs/{run_id}/stop` | Stop only this run. Returns `{"stop_requested":true}` or false if already terminal. |
| POST | `/coding/runs/{run_id}/control` | Send steering or follow-up input; returns 204. |

History `limit` is 1-100; `offset` is nonnegative. Offset pages are a presentation convenience, not a frozen snapshot while sessions change.

Session creation body:

```json
{"workspace_id":"<grant-id>","model_profile_id":1,"project_id":null,"title":"Inspect Fruitcake"}
```

Session responses contain `id`, `workspace_id`, `model_profile_id`, `project_id`, `title`, `status`, `context_generation`, `created_at`, and `updated_at`. Use `/coding/models` to choose an authorized local, tool-capable text profile. The initial Pi adapter supports local Ollama/local/OpenAI-compatible profiles only. Neither the client nor prompt can supply runtime credentials, an executable, or extra tools.

Submission body:

```json
{"message":"Inspect the session lifecycle and explain its failure paths.","client_request_id":"<client-generated-uuid>"}
```

Generate and retain `client_request_id` before sending. Retrying the same session, identifier, and exact message returns the same run, including after completion. Reusing the identifier with different content returns `request_conflict`; do not generate a fresh identifier automatically after a transport failure. Identifiers are required by the HTTP contract even though internal service callers may omit them.

Run responses contain `id`, `session_id`, `client_request_id`, `status`, `prompt`, `response_text`, `context_generation`, `last_event_sequence`, `error_code`, `created_at`, and `finished_at`. The visible response is finalized durably at completion; use events for incremental display. Active states are `starting`, `running`, `waiting_approval`, and `stopping`. Terminal states are `completed`, `failed`, `cancelled`, and `interrupted`. A 202 response is acceptance, not completion.

Control body:

```json
{"action":"steer","message":"Focus on startup cancellation first."}
```

`action` is `steer` or `follow_up`. Controls are not idempotent and must not be blindly retried after transport failure. Delivery failures return a safe error and may be ambiguous; inspect current events/state first. Stop is run-specific, not session-wide, so retrying an old stop cannot cancel a later prompt.

## Managed Checkouts

`GET /coding/sessions/{session_id}/checkout` returns nullable owner-scoped metadata. Explicit `POST` to the same path creates one detached worktree before a session's first run; `POST /coding/sessions/{session_id}/checkout/remove` removes it only after closing the session and only if clean, including ignored files. Both mutations return 202 immediately, are session-idempotent, accept no arbitrary destination/ref, and require an active grant. Poll the GET endpoint for terminal state; a 202 is not success.

Responses contain `id`, `session_id`, `canonical_path`, nullable `baseline_commit`, `status`, nullable `error_code`. Once attached, runtime and review use that worktree without source fallback; unavailable/failed/removed records block execution. Existing source-only sessions are unchanged. See [managed checkout lifecycle and migration 053](coding_checkouts.md) for limits, restart/cancellation recovery, private storage, non-force removal, and the still-separate write gate.

## Repository Review

The Mac Code header's **Changes** action uses `GET /coding/sessions/{session_id}/repository` for a bounded live file list, and `GET /coding/sessions/{session_id}/repository/diff?path=<encoded-relative-path>` for a selected file preview. Both are read-only, owner/grant scoped, start no runtime, and recheck authorization before delivering data. They release authentication transactions before waiting for Git.

Responses include `basis:"current_worktree"` and `observed_at`. Status includes nullable `head` and `files` (`path`, `index_status`, `worktree_status`, nullable `original_path`). Diffs include `path` and `sections` (`kind`, `text`, `truncated`), separating staged, unstaged, and untracked additions. A live observation is not a saved checkpoint or run-attributed change artifact; concurrent edits may change it. Status cannot silently fall back to a parent repository or return an incomplete clean list.

See [repository review](coding_repository_review.md) for limits, unsupported files, safe Git execution, error codes, manual acceptance, and the remaining write/worktree gate. No migration or new configuration is required.

## Event Stream

`GET /coding/runs/{run_id}/events?after=0&follow=true` is authenticated SSE (`text/event-stream`). Use `URLSession.bytes(for:)` or an equivalent authenticated streaming request in the native client. Browser `EventSource` cannot attach the required bearer header by itself.

```text
id: 4
event: assistant.text_delta
data: {"schema_version":1,"run_id":"<run-id>","sequence":4,"kind":"assistant.text_delta","payload":{"delta":"Inspecting"}}

```

- Sequence numbers are positive, monotonically increasing, and scoped to one run. Persist the last successfully applied event ID separately for each run.
- Reconnect with `Last-Event-ID: <sequence>` or `after=<sequence>`. If both are provided, replay starts after their maximum. Malformed/negative cursors return 422; a cursor ahead of the run's committed sequence returns 409 `cursor_ahead`.
- Events strictly after the cursor are replayed before following live work. `follow=false` gives a finite SSE replay of currently committed events, not a JSON array.
- The stream ends after terminal state and all committed events are delivered. Reconnecting at the terminal cursor yields an empty stream. Do not treat ordinary EOF as run success; reconcile with the durable run status.
- Idle streams emit `: keep-alive` comments every 15 seconds, without cancelling the pending event read. Comments do not advance the cursor.
- Authentication releases its database connection before streaming; event reads use short service transactions. Account coding eligibility is rechecked during following.
- Disconnection cancels only the observer. It never aborts the underlying coding run. Stop requires the explicit stop endpoint.
- Both streaming and normal coding responses use `Cache-Control: no-store`; streaming also disables proxy buffering with `X-Accel-Buffering: no`.
- Known events include `run.accepted`, `session.ready`, `run.started`, `assistant.text_delta`, `assistant.message`, operation events, approval events, user-control events, and a terminal `run.completed`, `run.failed`, `run.cancelled`, or `run.interrupted`. Ignore unknown event kinds without losing the cursor; render payloads as data, not instructions or executable markup.
- `assistant.message` replaces the visible-message snapshot, rather than appending another delta. A large snapshot may be delivered as an empty snapshot reset followed by bounded `assistant.text_delta` fragments. Always reconcile terminal state with the durable response. `session.ready` indicates whether bounded prior context was reconstructed; a new context generation is not full process-memory restoration.
- Tool output may include `preview_truncated:true` and `original_text_bytes`. Its `text` includes a visible truncation notice and fits the serialized event-byte budget, including JSON escaping and metadata. This is a display preview only; Fruitcake does not shorten the tool result consumed by the runtime. Clients that ignore the optional metadata still show the notice in the text.
- Token-level reasoning ticks are suppressed and small visible deltas are batched. Event sequences describe committed display events, not individual model tokens; do not assume one event per token.

If an error occurs after HTTP headers have been sent, the stream emits `event: stream.error` with a safe `{"code":"...","message":"..."}` object and no `id`, then closes. It is a transport/authorization signal, not a durable run event. Do not advance the cursor for it. Reconnect for transient errors; refresh authentication or stop retrying when access is denied. Provider protocol records, credentials, raw reasoning, and private runtime request IDs are not public events.

## Approvals

`GET /coding/runs/{run_id}/approvals` returns the durable approval records: `id`, `run_id`, `status`, `title`, `message`, `created_at`, `expires_at`, `resolved_at`, nullable `change` (complete path/diff/hashes), and nullable `request_sequence`. Records are ordered by their persisted request event sequence. `approval.requested` includes `expires_at` for immediate client rendering; expiry/cancellation resolves through `approval.resolved` with `status`. Operation completion settles only its own pending request, not other parallel requests. See [ordered transcript](coding_transcript.md). Internal runtime request IDs and payload storage fields are excluded.

`POST /coding/approvals/{approval_id}/resolve` takes `{"approved":true}` or `{"approved":false}` and returns 204. File-change approval additionally requires `"change_hash":"<after_hash>"`; missing/mismatched acknowledgements return 409 without resolving or writing. The boolean is strict; strings such as `"false"` are rejected. Decisions must use the durable ID from `approval.requested` or the approval list. Foreign, expired, or already answered approvals cannot be resolved.

The decision is committed before runtime delivery. `approval_delivery_failed` means the decision was recorded but delivery failed; do not resend an approval. Reconcile run/approval state and saved change outcomes: an approved file change may already have been applied by the host before runtime delivery failed. Optional read confirmation does not enable write tools or shell commands.

## Errors And Client Handoff

Coding service failures have stable `code` and `message` fields. In the full Fruitcake application they appear as:

```json
{"error":{"code":"session_busy","message":"This coding session already has an active run"},"trace_id":"..."}
```

Ordinary authentication failures retain the application's existing string-valued `error` format, and request-validation errors retain FastAPI's 422 `detail` list. The client decoder must handle these deliberately rather than assuming every error body is a string. Request bodies reject unknown fields and coercion of strings to integers or booleans.

Typical service statuses: 403 for policy/grant/model denial, 404 for missing or foreign session/run/approval, 409 for busy/closed sessions, request conflicts, expired approvals or unavailable controls, 422 for invalid inputs, 502 for failed approval/control delivery, and 503 for unavailable service/runtime or shutdown. A failed asynchronous run uses its durable `error_code` instead of retroactively changing the submission's HTTP response.

Output guard failures now distinguish `response_limit` (visible response characters), `event_payload_limit` (metadata that cannot fit the event-byte budget), and `event_limit` (committed event count). Historical runs may retain the older generic `output_limit` classification. Defaults remain bounded at 65,536 response characters, 32,768 serialized payload bytes for ordinary events, and 2,000 nonterminal run events; large tool previews no longer fail merely because their text plus JSON overhead exceeds one event. Complete file-change approvals use a separate 4 MiB serialized budget (including `approval.requested` events) and support complete UTF-8 files up to 256 KiB before and after; neither their snapshots nor their diffs are display-truncated. Other interactions retain the ordinary event budget.

The [Mac read-only workspace](coding_client.md) now implements this contract on the native client branch. It reuses the established rail and titlebar host without changing the existing chat transport or MCP dashboard contract. Live signed-in and full-window geometry testing remain manual soak requirements.
