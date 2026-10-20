# Chat: My Workspace

Prepared for v0.7.44 after implementation October 19, 2026. This is the signed-in user's Fruitcake
workspace, not a coding checkout or an arbitrary authorized host directory.

## User Flow

Chat's folder button opens **My Workspace** in the shared inspector. Directories
load lazily; files open in read-only tabs. Text uses the shared code palette for
supported languages. Markdown remains literal text, not a rendered document.

A file tab offers:
- Find and Copy for complete text previews.
- Download through the native save/export dialog, up to 20 MB.
- Open Preview for supported images and PDFs, using existing native viewers.
- Attach to Chat, which stages a path reference in the originating conversation's
  draft. It does not send a message or insert file content into a model request.
  References use an explicit `workspace/<user>/<relative-path>` tool prefix so a
  real `workspace/` subfolder cannot be mistaken for the legacy root alias.
  Workspace references stay with that draft across navigation and clear on an
  account/server change. The user must still send the message normally.

There are no delete/rename/write operations in this slice. Library remains
separate. Code still requires its exact authorized ready managed checkout and
never falls back to My Workspace.

## API And Authority

All new endpoints are authenticated GETs beneath `/workspace/explorer`:
- Root: returns a server-selected `user_workspace` reference, `user_id`, null
  `session_id`, opaque root ID, display label and inspection bounds.
- `/directory`: root_id, relative path, offset and optional observed version.
- `/file`: root_id and relative path; complete UTF-8 text or binary/oversized status.
- `/download`: root_id and relative path; bounded bytes, always attachment /
  application/octet-stream with no-store, nosniff and a sandbox CSP. HTML is not
  served as an executable same-origin page.

The root is `settings.workspace_dir / authenticated_user.id`. Clients cannot pick
another user or an absolute root. An empty user directory can be initialized on
first open. The provider applies existing authenticated-user checks and rechecks
active user status after filesystem work. Client providers also recheck the
account/server and originating context before publishing delayed results.

Root identity includes the configured base, user, device and directory inode;
replacement requires reopening My Workspace. Directory pages are version-bound,
100 entries/page and at most 4,096 scanned direct names. Complete text is capped
at 256 KiB and 12,000 LF-delimited lines; paths at 1,024 UTF-8 bytes/32 components.
Binary/oversized files are not truncated into text. Downloads/previews cap reads
at 20 MB before allocating the complete response.

Shared descriptor-relative no-follow filesystem primitives reject symbolic/hard
links, special files, traversal and changing parent/file identities. User-workspace
policy excludes dot paths and sensitive secret/key/database names but deliberately
allows normal build/storage/workspace folders. Git ignores and repository grants
are not applied or invoked for My Workspace. Code's original path/ignore policy
is unchanged. IO uses the existing two-worker bound; cancellation keeps a worker
slot occupied until its OS call actually finishes. This is trusted-local IO,
not an OS sandbox against a hostile backend host.

## Verification

- New isolated workspace tests: **7 passed** (auth, cross-user roots, path/link
  policy, bounded inert downloads, paging/root replacement, revocation and exact
  attachment-path parity with filesystem reads).
- Shared Code Explorer/API/repository/checkout/capture regressions: **87 passed,
  1 skipped**. The skip is an existing opt-in installed-runtime check.
- Closeout coding regressions: **356 passed / 62 skipped** in the isolated run;
  the one sandbox-denied process-inspection check passed separately with permission
  (**357 total passes**). Installed-runtime/Docker opt-in checks were not enabled.
- Native build and **141 tests / 168 cases passed**, excluding only the known
  unrelated missing-state MCP fixture. New coverage includes provider/owner/basis
  separation, explicit attachment staging, account changes during reads and
  inert root-bound preview references. Synthetic tree/file renders at 300/600pt
  were inspected; actual signed-in save/preview/attachment UX remains manual.
- Python compilation and tracked/new-file whitespace checks passed. Workspace
  boundary tests are added to CI. No live migration or model run was performed.

Logs and native summaries are under `/private/tmp/fruitcake-workspace-*`.
Final closeout logs/summaries use `/private/tmp/fruitcake-closeout-*`. The first
native closeout pass had a checkout-polling timeout; the complete final rerun
passed. The existing excluded MCP fixture remains a separate issue.

## Rollout And Development Worktrees

Release pairing: backend v0.7.44 and native client v0.2.5. Once merged, update
the normal backend checkout and rebuild the matching native release; use the
normal start command. The paths below describe pre-merge development testing,
not a requirement to run a separate checkout after deployment.

Backend branch: `codex/chat-workspace-runtime` in
`/Users/jwomble/.codex/worktrees/chat-workspace/fruitcake_v5`.
Native changes remain with the pending code-appearance work in
`/Users/jwomble/Development/FruitcakeAi/.worktrees/code-appearance-client`.
Open its `FruitcakeAi.xcodeproj`, not the original native checkout.

The normal startup checkout was NOT switched or patched, so its existing
`./scripts/start.sh` still runs the old backend without these APIs. No `.env`,
venv, user files or credentials have been copied to the new backend worktree.
Because workspace/storage paths in the operator's configuration are relative,
do not launch it from an unconfigured worktree and accidentally browse an empty
new workspace.

For a temporary test, stop the current server normally first, then launch the
new app code from the ORIGINAL backend working directory. This retains its
existing `.env`, venv, DB credentials and relative workspace/storage paths:

```sh
cd /Users/jwomble/Development/fruitcake_v5
.venv/bin/python -m uvicorn app.main:app \
  --app-dir /Users/jwomble/.codex/worktrees/chat-workspace/fruitcake_v5 \
  --host 0.0.0.0 --port 30417
```

Use the existing deployment's host/port policy if different. This is a test
launch, not a replacement bootstrap or a migration bypass for future schema
changes. This slice has no migration, new dependency or required `.env` change.
After landing the branch in the normal checkout, return to the usual start
command. No working server/application was stopped or restarted by the agent.
