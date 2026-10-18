# Coding Workspace Lifecycle

This follow-on supersedes the one-conversation/immutable-HEAD restrictions in the
original checkout slice. It adds migration **056_coding_workspace_lifecycle**.
Stop the backend and use the normal `scripts/start.sh` bootstrap flow, then rebuild
the native client. Implementation tests do not migrate the live database.

## Conversations And Checkouts

- A checkout owns an authorized source workspace, immutable baseline, expected
  current HEAD, and optional managed branch. Its original `session_id` remains
  creation provenance, not exclusive runtime ownership.
- `coding_checkout_attachments` maps conversations to execution roots. Migration
  056 backfills existing attachments and HEADs without touching checkout files.
- New Coding Session offers **Working copy: Continue ...** for ready checkouts of
  the selected authorized repository. Conversation context starts fresh; files
  are not reset, copied, or committed. Edits and commands default off.
- Only one run may execute in a shared checkout at a time, including read-only
  runs. Other conversations receive a conflict until that run finishes/Stops.
  Coordination assumes the existing single backend supervisor, not distributed
  workers or protection from an external filesystem writer.
- Switching execution to another attached conversation retires peer Pi processes.
  Returning conversations reconstruct bounded history and reread shared files
  rather than retaining stale process observations across another agent's work.
- Closing, archiving, deleting, or changing a conversation's model does not remove
  checkout files. Clean-only removal requires **all attached conversations** to be
  closed. Committed branch refs are retained, not silently deleted.
- There is still no source fallback for failed, busy, or removed checkouts.

## Conversation Controls

Right-click a conversation to **Archive Conversation** or **Delete Conversation**.
**Show Archived** lists archived conversations and offers Restore. Archiving
closes the runtime; restoring starts idle with edit/command opt-ins off. Active
runs must finish or Stop first.

Delete is a tombstone: it removes the conversation from lists and ordinary API
access, while retaining backend history, approvals, and change provenance. The
confirmation explicitly distinguishes this from secure erasure. Permanent audit
retention/purge policy is deferred. Reusable checkouts remain selectable even if
their originating conversation was deleted.

The session ellipsis menu offers **Switch Model** while idle/interrupted. It
validates local-model access, retires old Pi process memory, and resets edit and
command opt-ins. The next run reconstructs bounded excerpts rather than restoring
the previous model's process memory. Every new run snapshots its model ID and
numeric profile ID; the UI shows that run's model. Pre-migration model identities
are backfilled from the session's best-known profile, not claimed as newly
observed historical evidence.

## Local Commits

The ellipsis menu's **Create Commit** opens the existing height-resizable top
review area. Select changed text files and enter a message, generate a review,
inspect its diff (optionally in the resizable pop-out), then explicitly confirm.
The composer is disabled while the delivery panel is open.

The host builds a temporary Git index from the expected HEAD and selected files.
The operator's real index and working files are not staged or modified by preview;
Git may write unreferenced objects. Existing staged changes are refused rather
than consumed. Text selection is bounded to 200 files / 1 MiB input / 2 MiB diff;
links, metadata, special files, submodules, and binary patches are refused.

Confirmation regenerates and hashes the base HEAD, selected tree, diff, message,
and branch. A changed preview is refused. The first commit creates the local
`fruitcake/<checkout UUID>` branch; later commits advance it with expected-ref
checks. Commits use `Fruitcake <fruitcake@localhost>` with signing/hooks disabled.
Only selected working-file content is committed; unselected work is retained.
The baseline is not rewritten. There is no model tool for commits or source
delivery and no automatic commit, reset, force, stash, remote fetch, or push.

## Apply To Source

The native client shows a dismissible **Last Git action** confirmation after a
successful commit or apply, with the returned commit hash. Apply feedback names
the destination branch/folder and explains that the conversation remains attached
to its isolated checkout. Feedback survives refresh and conversation switching
within the current workspace store, not app restarts. Preview generation alone
and uncertain/failed confirmations never produce success feedback.

**Apply Commits to Source** is a separate **operator-only**, owner-scoped action.
A read-only workspace grant never implicitly gives a non-operator source-write
authority. Review shows the original folder/branch, destination commit, and exact
committed diff. Uncommitted checkout changes are not copied.

The source must be on a local branch, completely clean (including untracked and
ignored files), and an ancestor of the reviewed checkout HEAD. First delivery is
**fast-forward-only**. Dirty or divergent sources are refused without automatic
merging, copying over files, stashing, cleaning, or force operations. Resolve such
states in an external Git client. For this conservative first version, even
unrelated ignored files block delivery; no file is silently discarded.

The host rechecks the preview hash, ownership/grant, expected checkout state, and
operator role before applying. Git hooks, filters, credentials, network, and
external helpers remain disabled. The existing trusted-local race caveat applies
to external processes modifying either repository during the operation.

Git operations durably reserve `committing`/`applying` checkout states so new runs
cannot start. Failure after a mutation becomes `failed / delivery_uncertain`;
restart during a reservation becomes `failed / backend_restart`. Files/commits
are kept and automatic replay is blocked pending operator inspection. Normal
transport loss likewise never triggers an automatic client retry. This is not a
transaction spanning Git and PostgreSQL, nor a complete Git recovery UI.

## API Additions

All routes retain coding authentication and owner scoping:

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/coding/checkouts` | Retained checkout metadata for the owner. |
| POST | `/coding/sessions` | Optional `checkout_id` joins a ready checkout from the same authorized repository. |
| GET | `/coding/sessions?archived=true` | Archived rather than ordinary conversations. Deleted rows are excluded. |
| POST | `/coding/sessions/{id}/model` | `{"model_profile_id":N}`; idle only, resets tool opt-ins. |
| POST | `/coding/sessions/{id}/archive` | `{"archived":true|false}`; close/archive or restore idle. |
| DELETE | `/coding/sessions/{id}` | Close and tombstone the conversation; no filesystem deletion. |
| POST | `/coding/sessions/{id}/delivery` | Preview or confirm a fixed-form local Git action. |

Delivery body: `{"action":"commit","files":["file.py"],"message":"Fix"}` or
`{"action":"apply"}`. Absence of `review_hash` means preview only. Confirmation
repeats the same inputs with the returned SHA-256 `review_hash`. A stale hash
returns conflict. The source action additionally requires an operator role.

Session-scoped checkout responses use `session_id` for the requested attachment;
the checkout list retains the originating session identity. Clients must not
reject shared checkouts merely because another conversation created them.

Migration downgrade refuses extra attachments, advanced commits, or archive/
delete markers that the old schema cannot represent safely. Do not downgrade a
live adopted workspace by dropping these records manually.

## Verification And Remaining Work

Verification: 288 isolated backend coding tests pass with installed Pi and Docker
fixtures; 56 focused Swift tests pass. macOS app/test bundles and the iOS Simulator
app build with signing disabled. Offscreen commit-panel renders were checked at
600- and 1,180-point widths. These checks do not certify live signed-in behavior.

Tests cover migration/backfill, shared-root serialization, retained files after
delete, model/runtime transitions and per-run identity, source operator checks,
owner-scoped API attachment metadata, stale reviews, selected/unselected commits,
source cleanliness/divergence, preserved staged index, and uncertain Git outcomes.
Swift tests cover model changes, archive/restore/delete, exact checkout selection,
cross-session review rejection, and non-retried lost confirmation responses.

Live signed-in acceptance remains necessary after upgrading: join the existing
AV inventory checkout, switch models, create a reviewed local commit, apply it to
a clean original folder, and confirm archive/delete retains the files. No live
operator checkout is mutated by implementation verification.

Independent Review Changes and model-specific reasoning controls remain the next
quality slice. Arbitrary merges/cherry-picks, remote Git, permanent audit erasure,
and automated uncertain-state reconciliation are not implemented here.
