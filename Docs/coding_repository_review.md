# Coding Repository Review

## Scope

The operator reported the live read-only approval/Stop, interrupted-session recovery, and full-window navigation/titlebar/inspector checks working on October 16, 2026. This records manual acceptance, not an automated end-to-end certification.

The follow-on slice adds **Changes** to the Mac Code workspace. Fruitcake obtains the change list and file previews directly from Git, without asking Pi or a model to describe them. It does not enable edit/write tools, shell commands, worktree creation, commits, staging, or publishing. The presentation rule is **file inspection on the right; comparisons in the top drawer or a resizable window**, not a fixed modal. Existing titlebar positioning and the shared inspector's resize geometry are retained.

## User Flow

1. Open an existing coding session associated with an active, operator-authorized Git repository root.
2. Select **Changes** in the coding workspace header to open the shared resizable right-hand file inspector. No prompt or model call is submitted.
3. Select a changed file for its status/details, then **View Diff** to open a height-resizable comparison above the conversation. Staged changes (HEAD to index), unstaged changes (index to working tree), and untracked additions are labelled separately. Split before/after is the default; Unified is available, including binary/metadata-only fallback.
4. Use the diff panel's window icon for a normal resizable detached snapshot. Its path and observation time are explicit; it does not silently follow another selected file or auto-refresh. The coding workspace releases detached windows when removed, including sign-out.
5. Close the right inspector and top diff independently; neither stops the run. Refresh obtains new repository observations and closes stale in-app comparisons. Session changes clear the in-app selection. External-editor handoff remains available in the coding header.

The top drawer uses a persisted preferred height, a fixed drag-start height, and global pointer translation, without measured-size-to-state feedback. Its visible height is bounded to preserve conversation space; very short windows show compact pop-out/close controls instead. The existing right-column resize handle and titlebar reservation are shared with document inspection, not duplicated.

Ignored files are excluded. Renames show their original path; patch previews show removal/addition rather than attempting expensive rename detection. Deleted files, new repositories without HEAD, mixed staged/unstaged files, binary summaries, empty additions, and unusual UTF-8 filenames are supported. Merge conflicts, directory/submodule previews, symbolic links, hard links, and special files require an external editor.

## Truth And Bounds

This is **live repository review**, not a persisted artifact/checkpoint or a claim that a particular agent run made the changes. Pre-existing user edits are included and never modified. Status, HEAD, and each preview are separate filesystem observations; concurrent external edits can change the repository between observations. The UI includes observation timestamps and explicit current-working-tree language.

Status is limited to 1,000 files and 256 KiB of raw Git output. Exceeding either returns an error, never a partial list labelled clean. Each diff section is limited to 128 KiB of raw output and carries an explicit `truncated` flag. Non-UTF-8 source content uses replacement characters for display; previews are not downloadable/applyable patch files. Each request has a 10-second timeout (including inspection-capacity wait), two concurrent inspection slots per backend, and bounded subprocess diagnostics. Cleanup may add a short process-reaping interval.

Split view presents changed hunks, not full before/after files. Source line numbers and context rows are preserved; consecutive removals/additions align by position, not a second inferred edit algorithm. Display is bounded to 1,500 rows per selected comparison with an explicit notice; Unified shows the original bounded returned patch. Both sides share one vertical scroller and independently scroll horizontally, avoiding offset-synchronization feedback.

## Security Boundary

- Both endpoints require the session owner and a current active workspace grant; account and grant checks are repeated after Git completes, before data delivery. Operator privileges do not override session ownership.
- A subdirectory grant cannot silently inspect its parent repository. The selected grant must name the canonical Git working-tree root.
- Diff paths must be in a newly obtained change list. Literal pathspecs prevent filename-to-pattern injection. Parent traversal, Git metadata paths, symlink ancestors, hard links, and special files are refused for content previews.
- Git uses explicit argument arrays, no shell, a minimal environment, no inherited Git/provider credentials, no pager/optional index refresh locks, disabled fsmonitor/hooks/external-diff/textconv, and disabled lazy fetching/transport. It does not run tests or repository scripts.
- Authentication and service database transactions are released before waiting for Git. Inspection does not hold the run-control lock and cannot block Stop or approval delivery.
- Results go only to the authenticated client, not automatically to a model or persistent event history. Changing session, leaving Code, or dismissing the review without an active diff invalidates late responses. Closing the file inspector while the top diff is open preserves that active review. Failed refreshes clear old review data instead of displaying stale state as current. Detached windows are explicitly labelled immutable observations.

The supported boundary remains **trusted-local alpha**, not hostile-repository or hostile-concurrent-writer containment. Git still parses repository metadata/configuration; path validation is not descriptor-based and can race malicious filesystem changes. The separate write-mode gate must not infer sandbox guarantees from this feature.

## API

- `GET /coding/sessions/{session_id}/repository`: `basis:"current_worktree"`, `observed_at`, nullable `head`, and `files` containing `path`, `index_status`, `worktree_status`, nullable `original_path`.
- `GET /coding/sessions/{session_id}/repository/diff?path=<encoded-relative-path>`: `basis`, `observed_at`, `path`, and `sections` containing `kind` (`staged`, `unstaged`, `untracked`), `text`, `truncated`.

Both use the existing bearer authentication and safe coding error envelope, with `Cache-Control: no-store`. Errors include `not_repository`/`repository_changed`/`unsupported_file` (409), `repository_limit` (413), `repository_timeout` (504), and `repository_unavailable` (503). Ordinary coding session, policy, and ownership errors still apply. No migration or new environment setting is required; use both updated backend and native branches.

## Verification

The preceding backend suite passed 127 tests against installed Pi 1.3.0; this presentation revision changes no backend code or API. The revised macOS application/test bundles and iOS Simulator application build successfully. Twenty-seven focused Swift tests pass in the isolated harness, including review wire decoding, independent failure handling, stale session/file/dismissal guards, inspector switching, split parsing/line alignment/bounds, and drawer height constraints. The actual file inspector was rendered at 340/480/900 points and the diff drawer at 600/1,000 points; rendered images were visually inspected. These checks do not replace live signed-in full-window testing or native pop-out interaction. The earlier app-hosted test-run limitation remains documented in `coding_client.md`.

## Next Gate

Prepare disposable worktree identity/lifecycle and baseline capture, then prove preventive approvals for bounded edit/write tools before exposing a distinct write mode. Persisted run-attributed diffs, application/rollback of changes, command execution, and broader sandboxing are not delivered by live Changes review.
