# Coding Workspace V1 Closeout

## Delivered Scope

The first coding sprint delivers a usable trusted-local coding loop, not an IDE
or an autonomous developer account. Fruitcake owns authorization, approvals,
durable history, execution evidence, and Git delivery; Pi 1.3.0 is the private,
replaceable coding-runtime adapter.

- Authenticated coding sessions and HTTP/SSE event replay, operation history,
  steering, Stop, bounded context reconstruction, and honest restart recovery.
- Explicit repository grants, native authorize/create dialogs, isolated Git
  checkouts, and no automatic fallback to the original source folder.
- Complete, bounded host-approved text edits with stale-file checks and saved
  change/diff evidence. The model does not apply its own proposed writes.
- Separately enabled, per-command approved Linux Docker execution. Only the
  checkout is writable; networking, host credentials, and Docker socket are absent.
- Reusable checkouts, model switching, and conversation archive/delete controls.
  Conversation removal retains checkout files and backend provenance.
- Reviewed local commits and operator-only fast-forward delivery to a clean
  source folder. Commit/apply confirmations identify the resulting commit;
  conversations remain attached to their isolated working copy after delivery.
- Right-column file inspection, resizable top diff review, and detached snapshots.
- Non-expiring new approvals, with human waits excluded from the execution budget.
  Command/startup/Stop deadlines and late-approval authorization checks remain.
- Bottom-following coding chat, reading-position protection, Jump to Latest, and
  right-aligned prompts styled consistently with the main chat.

The native client is a separate repository: `Wombolz/FruitcakeAI_Client`.
Backend and client changes should be adopted together.

## Verification And Acceptance

Latest verification covers 304 isolated backend coding tests, including installed
Pi against a scripted localhost model and explicit Docker fixtures, and 68 focused
Swift tests. macOS app/test bundles build with signing disabled. The shared
approval model also builds for iOS Simulator; Code itself is a Mac surface.
The last Mac pass includes actual hidden-window scroll-view growth, resize,
reading-position, resume-following, and new-submission checks. Rendered prompts,
approval cards, Git confirmation banners, and diff panels were visually inspected.

Closeout also fixes the shared test fixture's aiosqlite teardown: dispose its
StaticPool engine after each test so worker threads cannot keep a passing pytest
process alive. A subprocess regression exercises the workflow's auth smoke command
and requires a clean exit. This is test infrastructure, not a production behavior
change.

The operator reported working approvals/Stop, interrupted-session recovery,
navigation/inspector geometry, approved editing/testing, model switching and review,
and end-to-end AV Inventory and RoomCheck exercises. Source delivery was verified
for the AV Inventory commit. The operator accepted the final scrolling change.
These are hands-on acceptance reports, not claims of universal model correctness
or unattended production certification. Generated-program reviews still found
real bugs; passing generated tests does not replace independent review.

## Rollout

1. Stop the old backend and use the normal `scripts/start.sh` bootstrap to apply
   migrations **052 through 057** before serving the updated API.
2. Rebuild the native client. Migration 057 permits `expires_at: null`; old native
   approval decoders cannot consume non-expiring approvals.
3. Coding remains disabled by default. Explicitly configure
   `CODING_RUNTIME_ENABLED=true` and the absolute `CODING_RUNTIME_EXECUTABLE` path
   to operator-installed Pi 1.3.0. No automatic runtime installation occurs.
4. Docker commands additionally need an operator-installed command image and
   per-session opt-in. Commands cannot build native macOS applications.

No live database migration, backend restart, or operator repository mutation is
part of the automated verification. Actual process loss still interrupts a run;
indefinite approval waiting is not transparent process-memory recovery.

## Known Boundaries And Follow-Ups

- Host-side Pi/read operations use application-level trusted-local controls, not
  an enforceable hostile-code filesystem sandbox. Docker commands are separate.
- Source delivery requires a completely clean source, including ignored files.
  Automatic merges, force operations, remote Git, and pushes are not agent tools.
- Uncertain Git mutations require operator inspection; automatic reconciliation
  and permanent audit-history erasure remain deferred.
- Independent **Review Changes** with a fresh context and model-specific reasoning
  controls are the next quality slice, not missing requirements for this closeout.
- GitHub authentication, reviewed branch publishing, pull requests, and delivery
  status belong in a separate next sprint. General network/MCP expansion, native
  builds, additional runtimes, and shared messaging remain separately gated.

The current sprint is ready for paired backend/client merge and ordinary-use
soak. Follow-up ideas should not keep these branches open indefinitely.

## Detailed References

- [Backend and API](coding_backend_foundation.md), [API contract](coding_api.md)
- [Native client](coding_client.md), [repository review](coding_repository_review.md)
- [Checkouts](coding_checkouts.md), [approved edits](coding_approved_edits.md)
- [Container commands](coding_container_commands.md), [transcript](coding_transcript.md)
- [Workspace lifecycle](coding_workspace_lifecycle.md), [approval waits](coding_approval_wait.md)
