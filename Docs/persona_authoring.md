# Persona Authoring Foundation

Status: bounded implementation complete; see [closeout](persona_authoring_closeout.md)
for remaining acceptance checks and separate follow-ups. Backend `codex/persona-authoring-runtime` is based on v0.7.44;
native `codex/persona-authoring-client` is based on v0.2.5. Not merged or released.

## Implemented In This Checkpoint

- Immutable, source-labelled stable prompt layers: persona identity, session
  identity context, persona behavior, managed runtime contract and restrictions.
- Existing built-in prompts retain their exact text/order when account grants
  cover the selected persona, verified against pre-refactor synthetic fixture
  fingerprints for all five legacy personas. Overbroad scope context is now
  intentionally narrowed to account grants.
- Local stable-prefix and non-local prompt paths retain existing layout. Time and
  skill additions remain turn context, outside the stable prefix.
- Tool dispatch checks current blocked tools and nonempty execution-profile caps
  before implementation, including the resolved target of the legacy search alias.
  Denials never call the tool or generate a content-bearing audit record. Ordinary
  dispatch logs denial metadata; denied approval replay raises a permission failure
  so the runner does not falsely advance an unexecuted step.

The authenticated `/personas/preview` endpoint exposes bounded static layers for
the current account, without history, recalled memory, dynamic skills or image
bytes. It never exposes another account's context. Prompt text is guidance, not
the authority boundary.

## Account Policy And Neutral

- `resolve_persona_policy()` separates personality from account ceilings. Stored
  account scope lists, the administrator-assigned `user.persona` legacy policy and
  the selected persona are intersected; blocked tools are unioned. Missing scope
  fields inherit; explicit empty lists deny. Malformed policy fails closed.
- Restricted-role accounts retain the protected restricted preset's policy even
  if their account default or conversation personality is general or Neutral.
- Neutral omits the household identity, tone and generic response-menu guidance.
  Tool mechanics, evidence/approval rules and privacy protections remain. It has
  no task-routing keywords and does not alter specialist preset definitions.
- PATCH selection, `/persona`, effective-tools discovery and execution share the
  context resolver. Effective-tools responses include the content-filter state.
  Unknown references are explicit errors, not a general-assistant fallback.
- Long-lived sockets refresh account columns before the next ordinary turn and
  before persona switching, including role changes and account suspension.
- Scope strings are legacy context labels/category hints, not a new per-document
  or provider-calendar ACL. Existing resource ownership/integration boundaries
  remain. Empty effective scope lists disable their current library/calendar tool
  families; nonempty lists do not claim new fine-grained adapter filtering.
- User-owned personality defaults do not rewrite `user.persona`, which remains
  the administrator-managed access baseline.

## Saved Revisions (Slice 2)

- Migration `060_persona_revisions` adds definitions, append-only application-level
  revisions, per-chat selections, personal defaults and per-run style snapshots.
  No existing chat/run columns or legacy histories are rewritten. Nonempty
  downgrade refuses data loss; user/chat/run deletion uses FK cascades.
- Custom references are `custom:<UUID>`, owned by one user; revisions are explicit
  positive integers. Display-name changes never rename the durable reference.
  Bundled YAML definitions remain read-only and retain their existing keys.
- `GET/POST /personas` lists/creates owned definitions; `POST /personas/duplicate`
  copies personality only. `PUT /personas/{id}` appends a revision using
  `expected_version`; `GET /personas/{id}/revisions` paginates history;
  `POST /personas/{id}/archive` retains revisions and existing chat selections.
  Change the personal default before archiving its definition.
- `GET/PUT /personas/default` manages the personal default separately from access
  policy. Initial default version is 0. New chats copy that selected revision;
  saving a newer head does not move the default or existing conversations.
- `GET /chat/sessions/{id}/persona` returns the pinned snapshot, selection version,
  update-available and archived flags. The existing PATCH accepts `revision` and
  `expected_version`; custom selection requires both. Built-in PATCH and `/persona`
  remain compatible. Custom revisions use the explicit API rather than ambiguous
  slash-command name lookup. Active/running/stopping/approval-waiting turns block
  selection changes with 409; revisions themselves may still be saved.
- `GET /chat/tools?persona=custom:<UUID>&revision=1` returns effective current
  tools; omission selects the personal default. It never treats personality as a
  permission grant. Owner checks, current account ceilings and restricted floors
  apply to both turn creation and approval replay.
- REST and WebSocket turn creation persist the same style snapshot passed to the
  agent. Approval replay uses that run snapshot, not the persona's latest head or
  session's current selection. Access policy is recomputed, not frozen with style.
- Legacy chats pin their current bundled style on their next turn. Pre-migration
  runs have no recoverable original style snapshot; first resume uses the current
  bundled session style and records it. This is not historical reconstruction.
- Drafts reject extra permission fields, invalid text and excessive size (16 KiB
  text total, 24 instructions, 1000 characters each). Neutral is the default mode.
  Null/omitted identity or tone inherits once at save in `default` mode, and means
  empty in `neutral` mode; explicit empty strings always stay empty. Stored
  revisions contain resolved strings, not live links to changing YAML defaults.
- Mutation audit events contain IDs/versions only, not personality text. Catalog
  and revision history are authenticated, owner-scoped and paginated. Custom chat
  personalities do not enter specialist/task inference or coding reviews.
- Owner row locks serialize mutations; session row locks coordinate selection with
  turn snapshot creation. PostgreSQL uses `FOR NO KEY UPDATE` for owner rows so a
  new chat's user-FK key-share lock does not conflict. SQLite tests do not establish
  real PostgreSQL concurrent locking correctness; that integration check remains.

## Settings Editor (Slice 3)

- Settings > Personas now creates/duplicates/edits/archives personalities, manages
  the personal default and reads paginated revision history. Bundled presets are
  protected. Explicit-empty and inherit-on-save identity/tone remain distinct.
- Saves use expected versions; conflicts retain the draft. Busy actions prevent
  duplicate clicks; a refresh failure after a successful creation does not offer
  another create. Unsaved selection/reload prompts are explicit; Settings owns
  the editor state so switching Settings sections does not discard it.
- Layout uses existing Theme tokens and SettingsCard components, with compact
  and wide variants. Main sidebar, inspector and titlebar geometry are unchanged.
- Preview shows source-labelled static prompt layers plus effective/blocked tools.
  It is not a model call and does not claim to show dynamically selected skills
  or recalled memories.
- Fresh test chats pin a saved persona revision and an explicitly chosen allowed
  model. Automatic recalled-memory injection defaults off, through REST and
  WebSocket turns; normal memory-writing/search tools and approvals remain.
  Chat shows a test/revision/recall banner, while its composer shows the model.
- Chat Profile lists custom heads and explicitly selects revisions with optimistic
  selection versions. Keeping an old pinned revision does not silently select a
  newer one when saving other profile settings. The Profile account page now
  calls the legacy administrator-managed persona the "Access baseline."
- Migration `061_persona_visual_assets` adds private immutable image blobs and
  per-test-chat recall options. Portrait and/or visual sheet references are saved
  with asset IDs and server-computed hashes in revisions. No character.md.
- Immutable image storage uses a dedicated owner-scoped DB table rather than
  mutable workspace files, preventing later workspace edits from changing a saved
  reference. Uploads validate actual PNG/JPEG/still-WebP pixels, reject animation,
  strip metadata and normalize PNG. Limits: 8 MiB input/output, 4 MP, 128 images or
  128 MiB total per account. Images load through authenticated owner-only routes.
- Replacing/removing an image changes the draft; saved history retains old assets.
  Uploaded-but-unsaved assets are also retained for now and count against quota;
  unused-upload cleanup/asset management remains a follow-up, not a delete promise.
- Image-generation handoff/test portraits remain gated: references are stored and
  previewable, but no MCP reference-image adapter has been verified or enabled.

## Remaining Acceptance

1. Migrations 060/061 were applied and runtime-account catalog access verified on
   October 20 after a private backup; retain the paired branch-specific backend.
2. Build the paired native project and perform a signed-in editor/profile/test-chat
   walkthrough. Component rendering is not a substitute for operator visual approval.
3. PostgreSQL concurrent locking integration remains unrun; SQLite verifies service
   behavior and PostgreSQL lock SQL compilation, not real concurrent transactions.

Neutral is available through the existing dynamic persona picker and the
`/persona neutral` command only when running this branch's backend. Saved-revision
APIs, native editor and prompt preview are implemented locally. Migrations 060/061
are required before running this code and were applied to the operator's database
on October 20. No new environment variable/dependency or live model call is
required. Native builds/tests use an isolated build, not the working app.

## Verification

- Isolated foundation/policy suite: **38 passed**, including all built-in prompt
  fingerprints, source provenance, local/non-local layout, direct/model/replay
  restrictions, actual-registry alias checks, Neutral, scope ceilings and malformed
  policy handling. Legacy fingerprints use fixtures with sufficient account grants.
- Persona API/long-lived socket suite: **7 passed**, covering discovery, auth,
  owner-only selection, missing baselines, restricted-role updates and suspension.
- Focused agent/incognito/persona/profile/skills/task/chat-run regressions:
  **404 passed / 5 deselected**, including auth and the initial policy API checks.
  The five failures reproduced on unchanged v0.7.44:
  two agent compaction-marker assertions, topic-watcher historical-memory count,
  a recurring-plan fixture NameError and duplicate-dispatch fixture expectation.
  The unfiltered suite is not claimed green.
- The positive append replay fixture now exposes append_file in its mock registry,
  matching the runtime cap's offered schemas rather than mocking only execution.
- Targeted independent review found an alias bypass; it was fixed and rechecked
  with no remaining targeted findings. Compilation and whitespace checks passed.

Slice 1 counts above are historical, not added to the overlapping Slice 2 suites.
Slice 2 final verification and baseline exclusions are recorded below.

Earlier counts below are checkpoint history. No live model or production database
was used. Logs use `/private/tmp/fruitcake-persona-*`. Changes remain local and
uncommitted; no new release/merge is claimed.

## Testing The Correct Backend

This branch lives at
`/Users/jwomble/.codex/worktrees/persona-authoring/fruitcake_v5`; the running backend
and human-owned checkout were not switched or restarted. After stopping the old
server normally and deliberately applying migrations 060/061 through the deployment's
normal migration workflow, existing configuration/data can be retained by launching
from the original checkout with this branch as the app code directory:

```sh
cd /Users/jwomble/Development/fruitcake_v5
.venv/bin/python -m uvicorn app.main:app \
  --app-dir /Users/jwomble/.codex/worktrees/persona-authoring/fruitcake_v5 \
  --host 0.0.0.0 --port 30417
```

Use the deployment's existing host/port policy if different. Test Neutral in a
fresh conversation: old messages and recalled memories can still carry household
context. This preset does not disable memory recall/writes or ordinary approvals.
Account grants that previously relied on persona scope replacement may need
administrator review; selecting Neutral cannot repair or expand them.

The approved internal planning reference is
`Docs/_internal/persona_authoring_and_prompt_composition_plan.md`; it is local
planning material, not a required runtime file. Explorer CRUD/basic editing
follows this persona sprint rather than being folded into this branch.

## Slice 2 Verification

- **40 isolated checks passed**, including migration/ORM parity, FK cascade,
  retained-data downgrade refusal and PostgreSQL owner-lock SQL compilation.
- **34 focused API/chat-run checks passed**, including owner isolation, unknown
  and blank references, field/size validation, immutable revisions, stale versions,
  default separation, explicit updates, archival and effective tools.
- Actual REST approval and WebSocket orchestration run with mocked inference:
  both persist and use the chosen revision; editing the definition while approval
  waits does not change the resumed agent's style. Policy remains current.
- Broader auth/agent/chat/task/skill/persona regressions: **462 passed, 6 explicitly
  deselected**. This overlaps the focused suites. The five Slice 1 baseline
  failures remain excluded; the sixth is
  `test_agent_definitions.py::test_agent_preset_exposes_expected_metadata`, whose
  hardcoded internal-roadmap source assertion also fails on untouched v0.7.44
  extracted into a temporary directory. Preset/config code was not changed.
- No real PostgreSQL concurrency test, native build, model call, live migration,
  process restart, commit or release was performed for this checkpoint.

Logs: `/private/tmp/fruitcake-persona-revisions-{isolated,api-final,regressions-final,actual-turns}.log`
and `/private/tmp/fruitcake-persona-baseline-agent-metadata.log`.

## Slice 3 Verification

- **41 isolated foundation/migration checks passed**.
- **46 focused persona/editor/chat-run API checks passed**, including actual
  REST/WebSocket memory-injection routing with mocked inference, authenticated
  image access, invalid/animated/oversized images, quota, metadata stripping,
  reference ownership and immutable hashes across revisions.
- **475 broader regressions passed; 6 confirmed baseline failures deselected**.
  These overlap the focused API suites; do not sum them as unique tests.
- Approved isolated macOS build succeeded. Final focused native run:
  **33 tests in 4 suites passed**, including revision request encoding,
  conflict-preserved drafts, post-save refresh recovery, visual-reference draft
  state, explicit test requests, compact/wide rendering and existing inspector/
  appearance checks. Some tests have parameterized cases.
- Existing test warnings remain: backend Pydantic/JWT fixture warnings; native
  AppIntents metadata extraction/autoShortcut service diagnostics. No compile or
  focused-test failures remain. iOS device testing and operator visual acceptance
  were not performed.
- No live database migration, server restart, working-app restart, commit or release.

Final logs: `/private/tmp/fruitcake-persona-editor-{foundation,api,regressions}.log`,
`/private/tmp/fruitcake-persona-editor-native-build.log` and
`/private/tmp/fruitcake-persona-editor-native-final-tests.log`.

## Explicit Persona Deletion

Added 2026-10-21. Archive retains the previous behavior; Delete is permanent and
available only for owned custom personas, including archived ones. No additional
migration is required beyond 060/061.

- `GET /personas/{id}/deletion-preview` returns affected conversation count,
  whether the personal default resets, head version and an impact token.
  `DELETE /personas/{id}` requires `expected_version` and `confirmation_token`.
  Changed revisions, selections, historical runs or default usage require a fresh
  confirmation (409), rather than silently increasing the confirmed impact.
- The owner mutation lock and ordered session locks coordinate with selection and
  turn creation. Running/stopping/approval-waiting turns and in-process active
  tasks block deletion. Inactive conversations are included, not revived.
- Affected chat selections/defaults become Personal Assistant for future turns.
  Dedicated historical run snapshots also lose the deleted persona's instructions;
  current selections that already use a different persona remain unchanged.
  Selection/default versions advance so stale clients cannot overwrite the reset.
- Assistant replacement snapshots retain an internal `policy_source: account`
  marker: use Assistant voice while preserving the custom persona's account-policy
  resolution. This is not accepted from persona drafts. Account baseline, role,
  tool overrides, memory-recall options and approvals are not changed.
- All persona revisions are removed. Candidate image assets are removed only if
  no remaining owned revision (including archived history), selection, default or
  run snapshot references them. Unrelated uploaded-but-unsaved assets are not
  swept. Unsaved drafts in other clients are not durable references.
- Existing messages, their metadata and backups are not erased. The confirmation
  discloses this; deleting a persona is not a promise of forensic data erasure.
  Audit retains IDs/counts only, not the deleted personality text.
- Native Settings shows Delete with impact confirmation; conflicts retain the
  selected persona. A successful deletion clears local persona/history/preview
  state even if the following catalog refresh fails. Returning to Chat refreshes
  the selected persona without reloading or scrolling the message history.

Verification: **101 backend checks passed** across persona foundation/migration,
policy, revision/editor/deletion APIs and chat runs/inspection. The deletion suite
contains 10 checks, including parent/restricted policy equivalence, stale impact,
owner isolation, active turns, archived/inactive chats and shared image retention.
Approved isolated macOS build and **37 tests in 4 native suites passed**. Your
Xcode signing-team change was preserved. SQLite tests and PostgreSQL SQL compilation
do not establish real PostgreSQL concurrency behavior; that check remains pending.
No live persona was deleted, migration applied, or working process restarted in
this slice. Operator UI acceptance and iOS device checks remain.

Logs: `/private/tmp/fruitcake-persona-deletion-regressions.log` and
`/private/tmp/fruitcake-persona-deletion-native.log`.

## Paired Test Checkout

The native project to open in Xcode is:

`/Users/jwomble/Development/FruitcakeAi/.worktrees/persona-authoring-client/FruitcakeAi.xcodeproj`

The existing primary Xcode checkout was not switched or modified. The small Neutral
preview in the primary backend remains separate and does not provide editor APIs.
A normal start from that old checkout does not pick up this isolated persona branch.

For an operator-approved rollout, stop the previous backend normally, retain the
original checkout as the working directory for its private config/data, and use
this branch's migration sources explicitly. Back up the database first. The
following command uses the existing credential-isolated migration wrapper; do not
invoke Alembic directly with runtime credentials on a role-separated installation:

```sh
cd /Users/jwomble/Development/fruitcake_v5
(
  set -e
  umask 077
  config="$(mktemp /private/tmp/fruitcake-persona-migration.XXXXXX)"
  trap 'rm -f "$config"' EXIT
  sed \
    -e 's|^script_location = .*|script_location = /Users/jwomble/.codex/worktrees/persona-authoring/fruitcake_v5/app/db/migrations|' \
    -e 's|^prepend_sys_path = .*|prepend_sys_path = /Users/jwomble/.codex/worktrees/persona-authoring/fruitcake_v5|' \
    alembic.ini > "$config"
  .venv/bin/python scripts/database_admin.py migrate -c "$config" upgrade 061_persona_visual_assets
)
```

Then use the branch-specific `uvicorn --app-dir` launch above, with the deployment's
normal bind/port policy. Private .env/keystore/workspace files are not copied into
worktrees. The existing dependency environment already contains Pillow.

Operator rollout on 2026-10-20: the running persona backend initially returned
500 for `/personas` because the database was still at `059_coding_reviews`.
A private custom-format backup was created under
`storage/.database_backups/persona-20261020-140251/` and its archive listing was
validated. Migrations 060/061 then applied successfully through the wrapper.
A read-only runtime-account check verified all seven tables, successful catalog
loading, and preserved schema/ledger write restrictions. The running backend's
`/health` returned 200. No process restart was performed. A full backup restore
was not exercised during this repair.

Acceptance walkthrough: create from Neutral, save, edit/save again, verify history,
attach a portrait/turnaround image and save, preview prompt/access, choose the
personal default, and open a fresh test chat with recall off. Verify Chat Profile
can explicitly choose a newer revision while other chats remain pinned. Archive
only after changing the personal default. References are not handed to image
MCPs in this slice; that adapter verification remains separate.
