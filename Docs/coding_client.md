# Mac Coding Workspace

V1 sprint closeout: [delivered scope, acceptance, rollout, and deferred work](coding_v1_closeout.md).
Use the paired backend/client changes with migrations through **057**. New
approvals do not expire, and coding chat now follows the latest content without
pulling readers away from older messages. Historical slice notes follow.

Current follow-on: [Coding Workspace Lifecycle](coding_workspace_lifecycle.md)
adds native repository dialogs, reusable checkout selection, conversation/model
controls, and reviewed Git delivery. Upgrade through migration 056 and rebuild;
older setup instructions below describe the original read-only client slice.

Implemented beginning October 16, 2026. Delivery branches: backend `codex/coding-runtime-phase-0` and native `codex/coding-runtime-client` in the existing `FruitcakeAi/FruitcakeAi` checkout. See the V1 closeout above for the current paired rollout.

## Delivered

- A desktop **Code** rail destination for the existing parent/admin client roles, separate from ordinary chat and task sessions.
- Explicit authorized-repository, local-model, title, and optional chat-project selection. Projects remain organizational only.
- An operator-only repository authorization sheet for the signed-in account. Paths refer to the backend machine, not automatically to the Mac running the client.
- Paginated session/run history, incremental visible responses, operation snapshots, read confirmations, steering/follow-up, run-specific Stop, and closing inactive sessions.
- Authenticated SSE with bounded byte-level LF/CRLF framing, UTF-8 chunk handling, event identity/version validation, deduplication, and sequence-gap detection.
- Bounded reconnect attempts that reuse the last applied event cursor and reconcile with authoritative run state. EOF is not treated as success.
- In-memory ambiguous-submission recovery that reuses the original request ID and exact message. Non-idempotent guidance and approval decisions are never automatically resent.
- Observer cancellation when leaving Code, without cancelling the backend run. Returning reloads durable run history and replays an active run from zero to reconstruct its display.
- Separate coding-sidebar width/visibility state with global-coordinate drag deltas. The existing titlebar host now accepts a destination title; its fixed-control anchors, moving title/divider calculation, and inspector reservation are unchanged.
- Explicit local-folder, VS Code, and Xcode handoff. A missing local path or editor produces an error rather than executing a shell command or assuming remote/local paths are identical.

The existing pending chat-sidebar edits and earlier API edits were preserved. `ChatView.swift` was not edited in this slice. The native iOS tab set is unchanged; coding is a Mac surface for now.

## Setup And First Test

1. Use both branches above in the existing backend and native checkouts. Building only the native client does not enable or migrate the server.
2. Deliberately install the tested Pi version, 1.3.0, and obtain its absolute executable path. No runtime package is downloaded by Fruitcake startup.
3. Stop the old backend and apply migrations through `057_coding_approval_wait` through the normal migration/bootstrap workflow. `./scripts/bootstrap.sh` is the backend's canonical bootstrap launcher and runs migrations before startup. The normal `./start.sh` from `scripts/` delegates to it.
4. Set the backend environment, then start that checkout:

```dotenv
CODING_RUNTIME_ENABLED=true
CODING_RUNTIME_EXECUTABLE=/absolute/path/to/installed/pi
CODING_REQUIRE_READ_APPROVAL=false
```

The feature remains **disabled by default** in source configuration. The live development database and `.env` were not changed by this implementation.

5. Build the native `codex/coding-runtime-client` branch and select **Code**. An old backend without `/coding/status` produces an explicit restart/branch diagnostic; a disabled backend shows setup guidance.
6. Sign in as an operator to **Authorize Repository**, entering an existing specific repository path on the backend machine. Broad roots are rejected. Then use the fixed new-session control or sidebar plus to select that grant and a local tool-capable model. The typed `/coding/models` endpoint provides numeric profile IDs and filters user/model/tool policy; generic `/llm/models` public string IDs must not be guessed or parsed as database IDs.
7. Submit a read-only request, for example: `Read README.md and explain the application entry points. Do not modify files or execute commands.` Verify live text and operation history. Stop a longer run, leave and return to Code, and reopen the session to check durable state.

For confirmation testing, enable `CODING_REQUIRE_READ_APPROVAL=true`, restart, and inspect a file. Allow/Deny use the durable approval ID, with client expiry display and server-side expiry enforcement.

## Client State Rules

Drafts are scoped to the selected session. While a submission's outcome is ambiguous, the composer and switching to another coding session are blocked until retry or history reconciliation establishes the result. Leaving the Code destination remains possible and does not stop the run. An explicit retry sends the same identifier, not another prompt.

The pending request and incremental projection are held together in memory, not partially persisted as a naked cursor in preferences. After app restart, the client loads server runs instead of automatically resubmitting an old editor buffer. Active runs replay from sequence zero; completed responses come from durable run records, with operation history loaded on demand. A backend restart is displayed as interruption, not successful completion or full process-memory restoration.

Older async responses are guarded by selection generation so they cannot replace another session's transcript or approvals. Expired approval buttons disable on a timer; the backend remains authoritative if device clocks differ. A read whose final operation event was missed is labelled `not finalized` until history replay establishes its result.

Responses and tool output are selectable text, not executable HTML or shell commands. The fixed read-only label is shown only for the supported read-only backend mode and event schema.

## Verification And Remaining QA

The macOS app and test bundles build with temporary DerivedData and signing disabled. The app-hosted Xcode test run compiled but stalled during the host launch/run; it was interrupted and is not counted as passing.

Thirty-two focused coding tests passed in an isolated temporary Swift harness using the same models, store, parser, inspector state, and test sources. They cover retry identity, durable approval IDs, non-retried controls, stale selection results, draft isolation, explicit stop, observer cancellation, cursor replay, snapshot semantics, wire decoding, parser limits/invalid UTF-8, real Foundation chunked SSE delivery, repository-review isolation and stale-response guards, shared-inspector lifecycle, split-diff parsing/bounds, drawer sizing, and checkout creation/polling/reconciliation/path selection. Offscreen renders cover the actual coding pane, right-hand file inspector at 340/480/900 points, and top diff drawer at 600/1,000 points. Rendered panes were visually inspected. The harness substitutes app-shell/auth/network setup; it does not certify a live signed-in end-to-end run, titlebar geometry in a real window, or native pop-out interaction.

On October 16, 2026, the operator reported the manual approvals/Stop, interrupted-session recovery, and full-window navigation/titlebar/inspector checks working and authorized the next slice. This is user-reported acceptance, not automated end-to-end certification.

The follow-on [repository review slice](coding_repository_review.md) adds **Changes** in the Code workspace header. It opens the shared resizable right-hand inspector for file selection/status/details; **View Diff** opens an adjustable-height comparison above the conversation, with split before/after and Unified modes. Its window control opens a resizable, explicitly labelled detached snapshot. There is no fixed review popup. The shared titlebar positioning and inspector resize handle are retained. Staged/unstaged/untracked content is separated, truncated previews are labelled, failed refreshes clear stale in-app data, and selection/dismissal guards reject late responses. This is not a persisted artifact or run-attributed diff. Rebuild the native branch and use the existing updated backend; this presentation follow-up requires no backend change or migration.

The implemented [managed checkout slice](coding_checkouts.md) adds explicit creation for an idle or interrupted source session, including one with run history, baseline/path/status display, background status polling, isolated runtime/review/editor routing, and confirmed clean-only removal after closing a session. A source-to-checkout transition preserves conversation history but retires the old runtime. The original checkout foundation requires migration 053; this history-preserving transition adds no migration. Update both backend and native build; inspector/diff/titlebar geometry is unchanged. A lost response blocks source execution until reconciliation. A checkout never silently falls back to the source path.

The [host-approved edit slice](coding_approved_edits.md) now adds migration 054 and an explicit **Enable Approved Edits** control on ready managed checkouts. Each complete, bounded file proposal must be opened in the top diff drawer before its approval button enables. Fruitcake owns the write and persists the actual outcome/diff; **Load Saved Changes** reviews that immutable record. Source sessions remain read-only, and lost mode responses block submission until Refresh. Shell, multi-file operations, hosted-model adapters, publishing, and stronger sandbox claims remain separate gates. The focused native harness now contains 36 checks; live signed-in edit testing remains required.

See [HTTP contract](coding_api.md) and [backend/security foundation](coding_backend_foundation.md).

The [ordered transcript follow-up](coding_transcript.md) replaces grouped text/actions/approvals with a replayable event-sequence timeline, immediate streamed approval cards, merge-safe approval refreshes, and compact resolved history. It fixes accidental cancellation of unrelated approvals during parallel tool completion. Restart the backend and rebuild the native app; no migration is needed. Current focused verification is 203 backend coding tests and 41 Swift tests, with Mac/iOS builds and a seven-approval offscreen render.
