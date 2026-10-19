# Read-Only Checkout Explorer

Implemented October 19, 2026, alongside the shared native inspector tabs. Restart
the backend on `codex/coding-review-runtime` and rebuild the native client on
`codex/coding-review-client`. No migration, environment setting, model run, edit
opt-in, command permission or network permission is added by Explorer.

## User Flow

- In Code, use **Explorer** beside Changes. A ready managed checkout is required;
  a source-only session does not silently fall back to its authorized source tree.
- Expand directories lazily and load further pages explicitly. Click an eligible
  file to open a persistent/pinned read-only tab. Reopening its identity selects
  the existing tab rather than replacing a review or another file.
- Source is literal UTF-8 text, with line numbers, selection, Copy and local Find.
  HTML/Markdown is not executed or rendered as a browser. Diff comparisons remain
  in the existing top drawer/pop-out. Opening a file does not send it to a model.
- File metadata identifies the provider, checkout, observation time and SHA-256
  version. Refresh reloads complete current bytes and discloses a changed version;
  this is not an immutable review capture. Captured evidence stays in review tabs.
- Switching tabs/hiding the panel retains file-view state and folder-expansion
  choices; reactivation checks current provider access. Reloading restarts directory pagination and
  restores expanded folders that remain available in the loaded pages. Closing a
  tab releases its cached view state, not its files or backend work.
- Chat uses the same Explorer host, but currently has **No Attached Directory**:
  there is no explicit Chat attachment-root grant API yet. Project membership or
  an artifact path does not authorize directory browsing. Existing artifact tabs
  continue to work. A future Chat provider can implement the shared read contract.

## API

All endpoints require the existing coding access token and database-backed role,
account, session-owner and active-workspace checks. An admin cannot browse someone
else's session. Responses are `Cache-Control: no-store`.

| GET | Parameters | Response |
| --- | --- | --- |
| `/coding/sessions/{id}/explorer` | None | Authorized root reference. |
| `/coding/sessions/{id}/explorer/directory` | `root_id`, optional relative `path` (default root), `offset` (0..4096), `version` for subsequent pages | One bounded directory page. |
| `/coding/sessions/{id}/explorer/file` | `root_id`, relative `path` | Complete text or an explicit binary/oversized result. |

Root fields: `provider:"coding_checkout"`, `root_id`, `session_id`, display `label`,
`read_only:true`, `byte_limit:262144`, `line_limit:12000`, `entry_limit:100`.
No client-supplied absolute path or native-Mac path fallback is accepted. The root
ID must still be attached to the same session's ready managed checkout. Provider,
root, owning context and relative path distinguish resource identities.

Directory fields: `root`, `basis:"current_worktree"`, `observed_at`, `path`,
`version`, `entries`, `next_offset`. Each entry has `name`, relative `path`,
`kind:"directory"|"file"`, and `size_bytes` (null for directories). Results are
directories-first, sorted by case-folded then exact name. Pages contain at most
100 eligible entries. Further pages require the exact observed directory version;
a changed page fails with 409 rather than silently mixing snapshots. Enumeration
stops with 413 above 4,096 direct entries, including excluded entries. There is no
  recursive scan or arbitrary search endpoint. Relative paths are at most 1,024
  UTF-8 bytes/32 components deep.

File fields: `root`, `basis`, `observed_at`, `path`, `status`, `byte_count`,
`version`, `text`, `line_count`. `status:"text"` contains the complete UTF-8 text
and SHA-256 version. Empty text is valid. Binary/non-UTF-8 content returns
`status:"binary"`, while files above 256 KiB or 12,000 LF-delimited lines return
`status:"oversized"`; neither contains a text prefix. Do not truncate real source
files to satisfy the display bound. An external editor is the current alternative.

## Boundaries And Lifecycle

- Explorer and review capture share the existing conservative `path_policy`.
  Dot paths, Git/agent metadata, environments, generated/vendor/storage paths,
  credential/secret names and sensitive extensions remain excluded. This also
  hides ordinary dot-config files; no include-hidden toggle weakens that policy.
- Live Git ignore policy is enforced even for force-tracked ignored files.
  Excluded paths cannot be reopened by bypassing the tree and calling the file API.
- Descriptor-relative traversal uses no-follow directory/file opens. Symlinks,
  hard links and special files are omitted/rejected. File and parent identities
  are checked again to reject moved/replaced/changing files, not deliver mixtures.
- Ownership is checked before touching a checkout; user/grant/root/attachment
  identity is checked again after filesystem/Git waits. No DB lock or transaction
  spans those waits, and no coding runtime is started or stopped.
- Requests have a 15-second browser wait bound and two inspection slots. Blocking
  filesystem work runs off the event loop with two separately bounded workers.
  Cancelling a request does not release its worker slot until the OS call finishes:
  OS filesystem calls themselves cannot be forcibly interrupted. This is trusted-
  local inspection, not an OS sandbox against hostile host processes/filesystems.
- Native content is bounded to 12 tabs per context/32 total; Explorer caches at
  most 64 directories/4,000 entries. Closed tabs release content. Account/server
  changes clear open/closed references. Pending results cannot publish after a
  tab/context is suspended; inaccessible content is cleared, not opened elsewhere.

Errors: 403 `file_denied`/revoked access, 404 `file_missing`, 409 `file_changed`,
`checkout_invalid`, `checkout_unavailable` or `explorer_unavailable`, 413
`repository_limit`, and 504 `repository_timeout`. Retry/refresh is explicit.

## Verification

Final backend integration focus: **87 passed, 1 skipped**, including 23 Explorer
checks plus API/repository/checkout/shared-policy regressions. The broader coding
suite initially recorded **352 passed, 6 skipped** and one sandbox-blocked process
test (`ps` permission); that test passed separately with the needed permission.
The final integration focus reran after the last Explorer security changes.
Installed-Pi live tests were explicitly excluded; no inference was requested.

Native app/test build: **123 tests, 134 parameterized cases passed**, excluding
the previously documented unrelated missing-`state` MCP response fixture. Tests
cover generic provider validation, root/context identity, explicit unavailable
Chat, lazy expansion, pagination, revocation, stale response rejection, changed
versions and the actual remote coding adapter with no native filesystem fallback.
Synthetic tree/source renders at 300/600 points were inspected. Signed-in scroll,
keyboard/find/copy and full-window geometry acceptance remains with the operator.
Logs and before-edit snapshots: `/private/tmp/fruitcake-explorer-phase3`.

The working server and native application were not restarted. This milestone is
included in backend v0.7.43 and paired native v0.2.4. Interactive HTML testing,
preview servers, networking and GitHub delivery remain separately scoped work.
Code Appearance And Syntax Highlighting is the next approved branch; see
`code_appearance_plan.md`.
