# Host-Approved Coding Changes

## Scope

This slice adds explicit, per-session approved text changes **only inside a ready managed checkout**. Source-repository sessions remain read-only. It builds on migration 053 and requires migration `054_coding_changes`; Pi stays pinned to 1.3.0. No new environment setting is needed.

Coding runs default to a 30-minute execution budget (`CODING_RUN_TIMEOUT_SECONDS=1800`), excluding human approval waits. New approvals do not expire. Upgrade through migration 057 and rebuild the native client; see [approval waits](coding_approval_wait.md). Startup, execution, and Stop deadlines remain bounded.

This is a deliberately narrow trusted-local alpha, not a general write-capable coding agent or OS sandbox. Files must be complete UTF-8 text, no NUL bytes, at most 256 KiB before and after. Parent directories must already exist. No file deletion, directory creation, fuzzy replacement, bulk patches, executable commands, installs, Git mutation, automatic merge/publish, or automatic rollback is exposed. Clean-only worktree removal still refuses changed, untracked, and ignored files.

The initial 4 KiB whole-file cap was too restrictive for ordinary source files. The proposal extension and host now both enforce 256 KiB in UTF-8 bytes, with separate 4 MiB serialized proposal and complete approval/event budgets. Pi's RPC normalization preserves full file-change confirmation messages instead of treating them as 32 KiB display previews; ordinary tool previews and metadata retain their smaller budgets. Exact edits are preferred for existing files and preserve all unrelated content, including portions outside a paginated read. Tool instructions and size errors explicitly forbid shortening a file or writing a truncated read preview to get around a limit. Snapshot reads handle short reads and never silently accept a prefix. Files larger than the new cap remain unsupported, not partially editable. This does not prove model-authored replacement content is correct: complete-file writes still require review.

Restart the backend with the normal start command to reload the proposal extension and release retained Pi runtimes. Rebuild the native client for its matching approval-event parser allowance (4 MiB plus framing headroom; ordinary frames remain limited to 128 KiB). SSE uses UTF-8 JSON to keep its serialized size consistent with the approval budget. No migration is needed. Retry a blocked edit with an exact replacement rather than reusing a shortened whole-file replacement drafted under the old limit. Existing files already modified incorrectly are not automatically repaired.

## Operator Test

1. Stop the backend and use the normal `./start.sh` from `scripts/` (or `./scripts/bootstrap.sh` from the backend root) to apply migration 054. Rebuild the native `codex/coding-runtime-client` branch. Implementation tests have not migrated the live database or restarted the live app/backend.
2. Open a session with a ready **isolated checkout**, then choose **Enable Approved Edits** and confirm. The banner must show `approved edits`. This explicit idle-session change releases any retained Pi process; the next prompt reconstructs bounded conversation context under the new tool policy.
3. Test with: `Create fruitcake-note.txt containing "Approval test" and a newline. Use the available Fruitcake proposal tool and wait for my approval. Do not execute commands.`
4. Before approval, confirm the file does not exist. Choose **Review Proposed Diff**. The complete bounded proposal opens in the existing adjustable top diff drawer, with split/unified and optional resizable window. Approve becomes available after opening review; Deny remains available without review. Every change has its own approval, not an allow-all permission; new requests wait without expiry.
5. Approve, then use **Load Saved Changes** on the run. Its `applied` record opens an immutable host-derived diff, separately labelled from live working-tree observations. The file should exist only in the managed checkout, not the source repository.
6. Repeat and Deny, wait beyond the execution budget, or Stop before approving. Waiting alone must neither expire a new approval nor write proposed content. Edit the target externally while approval is pending, then approve: the stale change must fail without overwriting the newer file. Disable edits while idle to restore read-only tool exposure.

A lost edit-mode response blocks submission until Refresh reconciles session metadata. Saved change records remain owner-scoped history even if a later local edit changes the checkout; they are not a claim that the current worktree still matches their content.

## Authority And Evidence

Pi exposes `fruitcake_edit` (one literal unique replacement) and `fruitcake_write` (a complete new/replacement file). Their extension implementation **does not write files**. It reads a bounded snapshot, requests confirmation with the full before/after content, and checks the host's result. Built-in edit/write/bash and direct user-bash remain unavailable. Model profile allow/block policies apply to the canonical `edit`/`write` names and their `fruitcake_` equivalents. An explicit allowlist must include these tools to expose them.

Both proposal tools accept a checkout-relative path or an absolute path under the authorized checkout. The extension normalizes the latter to a relative path before snapshot/approval and reuses that frozen path after confirmation. Absolute paths to the source repository, sibling directories, or elsewhere remain blocked, as do traversal, Git metadata, and linked targets. The host API still accepts only relative proposal paths. Restart the backend to reload retained Pi processes after extension-code updates; no native rebuild or migration is needed for this path-handling fix.

Fruitcake independently reads and compares the proposal's target, checks its managed checkout and active authorization, derives the complete diff/hashes, and persists the proposal with its durable approval. The approval binds one exact path, existence state, and before/after content; it cannot authorize later content or another path. Oversized/incomplete previews are refused, never truncated into approvable changes. At most 32 proposals are retained per run.

On approval, the host revalidates ownership, active user/grant/model/tool policy, run/Stop state, checkout identity/baseline, deadline, and the file snapshot. Relative paths only; `.git` components, traversal, links, special files, and nonexistent parent directories are rejected. Descriptor-relative no-follow traversal and a same-directory temporary file avoid following target links. Existing permission bits are preserved (special set-ID bits are not propagated); new files are 0644. The host rechecks before atomic replacement, syncs, and rereads the exact result. No shell or runtime-supplied patch is executed.

Migration 054 adds `CodingSession.edit_mode` (false by default) and `CodingChange`: run/approval/checkout identity, path/tool, complete before/after text, SHA-256 hashes, host-generated diff, outcome/error, and timestamps. Outcomes distinguish `proposed`, `denied`, `cancelled`, `applying`, `applied`, `failed`, and `uncertain`. `applied` requires host verification, not an assistant success message. This is per-approved-operation evidence, not an automatically attributed net diff of all external edits in a run.

`applying` is committed before touching disk. A crash in the filesystem/database boundary becomes `uncertain` on recovery; the host never automatically retries or reverses it. Failure after replacement is also uncertain. A lost Pi confirmation does not erase an already verified applied record. Inspect uncertain changes manually. Stop cannot undo an edit already approved and applied. Filesystem operations and database commits are not a distributed transaction.

These are application-level checks for operator-controlled local repositories. Concurrent hostile filesystem writers, compromised runtimes/operators, network-mounted filesystems, and arbitrary untrusted code are not contained by this design. Worktrees are writable directories, not a sandbox. No claim is made that an approval can atomically compare-and-swap against an adversarial external editor between system calls.

## Additive API

- `GET /coding/status` adds `approved_edits: true`. The default `mode` remains `read_only`; this is a capability, not blanket write permission. Older backends omit it and new clients hide the opt-in control.
- Session responses add `edit_mode: false|true`.
- `POST /coding/sessions/{id}/edit-mode` accepts only `{"enabled":true|false}` and returns updated session metadata. It requires idle/interrupted state, an active source grant, and a ready owned checkout when enabling. No path/ref/tools can be supplied.
- Existing approval responses add nullable `change` with `id`, `path`, complete `diff`, nullable `before_hash`, and `after_hash`. The existing durable approval resolve endpoint is the only apply action; the model cannot resolve it. Approving a change must include `change_hash` equal to that proposal's `after_hash`. Missing/mismatched hashes return `409/change_review_required` without resolving or writing; read approvals and denials still need only `approved`.
- `GET /coding/runs/{id}/changes` returns owner-scoped persisted records including status, safe error, and timestamps. A `denied`/`failed`/`uncertain` record is evidence of a proposal/outcome, not proof the after-content was applied.

No event schema bump is required. `change.recorded` is additive; older clients can advance over unknown event kinds but should not enable edit mode without complete review UI. A session explicitly enabled by a newer client remains enabled on the backend. Legacy boolean-only approvals cannot apply a file change: the server requires the change-specific hash acknowledgement as well as the durable approval ID.

## Verification And Next Gate

The 256 KiB follow-up passes 219 isolated backend coding tests (including installed Pi 1.3.0 against a scripted local model) and 43 focused Swift tests. macOS application/test bundles and the iOS Simulator application build with signing disabled. Regression coverage includes exact edits at the whole-file boundary, UTF-8 byte limits, escaped approval payloads, complete API/SSE diffs, client byte framing, unchanged ordinary-event limits, short filesystem reads, and refusal without mutation above the limit. No live backend restart or real-model run was performed.

The full isolated coding suite passes 196 tests, including installed Pi 1.3.0 with a scripted loopback model, the complete host-approval/write/evidence flow, uncertain outcomes after post-write cleanup failure, relative/absolute proposal paths, new-file creation, and blocked outside/link/metadata targets. The focused native harness passes 36 tests, including an offscreen render of the actual proposed-change card and top drawer. Mac application/test bundles and the iOS Simulator application compile with signing disabled; app-hosted UI execution is not claimed as passing. The path-handling follow-up changes no native source.

Tests cover real Git checkout/source separation, complete approval previews, owner/policy enforcement, stale-file rejection, expiry/denial/Stop, recovery uncertainty, evidence after lost runtime confirmation, file/link/metadata safety, and installed Pi against a scripted loopback model. The native tests cover explicit mode selection, lost-response reconciliation, review-before-approve, and stale-session isolation. No real model was called by these tests.

The live operator test above remains required after migration/rebuild. Next, soak this bounded edit path before further widening file limits or adding multi-file/directory operations. Preventively approved commands/tests, stronger isolation, hosted adapters, and publish/merge remain separate gates.
