# Persona Authoring Closeout

Date: October 21, 2026 (America/New_York).
Status: implementation complete for the bounded persona sprint; paired changes
prepared for review, not merged or released. No additional product phase is
required before closing these branches.

## Delivered

- Source-labelled prompt composition and a Neutral, tool-capable starting point.
  Runtime rules, current account policy and restricted-role protections remain
  separate from editable personality. Direct calls, aliases and approval replay
  enforce current tool restrictions.
- Owner-scoped Settings authoring, duplication, versioned saves, conflict-retained
  drafts, personal defaults, source-labelled prompt/access preview, revision
  history and archival. Built-in definitions remain protected.
- Explicit revision selection in Chat Profile; chats and runs pin their selected
  style rather than adopting every later edit. Fresh test conversations choose a
  model and independently control automatic memory recall, not tool permissions.
- Private portrait and/or visual character-sheet/turnaround attachments, validated,
  normalized, metadata-stripped and pinned by asset ID/hash. No character.md.
- Explicit deletion with affected-chat/default confirmation, stale-impact checks,
  active-run/approval blocking, Assistant replacement and snapshot cleanup.
  Messages remain unchanged; images shared by retained revisions/snapshots remain.
  Deletion is not forensic erasure of messages, metadata or backups.
- Native Settings state is scoped to the signed-in server/account; switching that
  identity discards the old editor state. A stale test-chat callback cannot open a
  conversation under a different account. Returning to Chat refreshes persona
  metadata without reloading or scrolling message history.

## Rollout And Checks

Migrations 060/061 were applied to the operator's database on October 20 after a
private custom-format backup. Runtime-account catalog access and preserved schema/
ledger write restrictions were verified. No further migration is needed for
Delete or this closeout. Use the paired backend/native branches; an older primary
checkout does not include the editor. See [persona authoring](persona_authoring.md)
and the native repository's `Docs/PERSONA_AUTHORING.md` for test-checkout paths and
credential-isolated rollout instructions.

Latest backend verification: 101 passing checks covering persona foundation,
migrations, policy, revisions/editor/deletion APIs and chat runs/inspection.
Earlier broad regression evidence: 475 passes with six baseline failures explicitly
excluded, as documented in the authoring notes. These suites overlap and must not
be summed. The entire repository suite is not claimed green.

Latest approved macOS build/focused verification: 37 tests in four suites pass.
The operator's signing-team project edit is retained locally and excluded from the
product commit. No working app or backend restart, live deletion or model call is
part of closeout verification.

Logs: `/private/tmp/fruitcake-persona-deletion-regressions.log` and
`/private/tmp/fruitcake-persona-closeout-native.log`.

## Closeout CI Security Gate

The first backend PR run stopped at its dependency audit, before persona tests:
`certifi 2026.7.22 / PYSEC-2026-4320`, fixed by `2026.10.19`. The runtime input now
declares that security floor, and both hash-verified baselines were regenerated
with uv 0.13.1 using `--upgrade-package certifi==2026.10.19`. No other package
versions changed; this is a targeted CI blocker repair, not a new dependency
refresh sprint or another audit exception. Upstream release:
https://github.com/certifi/python-certifi/releases/tag/2026.10.19.

The working virtualenv was not modified. Normal bootstrap/install must consume
the new baselines after adopting this branch to deploy the certificate update.
The existing NLTK exception/review deadline remains unchanged; GitHub reports it
once per requirements file, not as two distinct persona findings.

Validation used a temporary certifi-only package overlay, not an installation into
the operator environment: hash-verified wheel installation and TLS CA-bundle
loading passed, `pip check` passed, and 60 dependency-baseline/auth/integration/
MCP-transport/model-stream checks passed. The overlay audit found no unexcepted
vulnerabilities (the one pre-existing NLTK exception remains). Additional logs:
`/private/tmp/fruitcake-persona-certifi-{checks,audit}.log`.

The next hosted run passed the audit and application checks, then exposed a stale
disposable PostgreSQL role fixture: it created current persona tables before
stamping revision 056, so upgrading attempted to create them twice. The fixture
now excludes all post-056 tables from its seed schema, expects head 061, and checks
runtime CRUD grants for the persona tables as well as coding-review tables.
The corrected disposable PostgreSQL role/migration and coding-reservation checks
passed locally: 25 tests. This verifies actual PostgreSQL migration/default-grant
behavior, not persona-specific concurrent editor/delete races. Production
PostgreSQL was not modified. Log:
`/private/tmp/fruitcake-persona-closeout-postgres.log`.

## Explicit Follow-Ups And Residual Checks

- Image-generation handoff/test portraits remain disabled. Coordinate generic,
  authenticated private-reference import with the image companion's M1/M1c:
  retry-safe chunk uploads, required client hash verification, stable caller
  ownership, workflow-discovered identity/sheet intents, delivered-input provenance,
  leases and honest local/remote retention disclosures. Fruitcake retains opt-in,
  owner checks, revision/hash pinning and destination disclosure. Generated images
  do not replace canonical references automatically. Never put bytes in model
  history or grant the MCP database access.
- `ui/message` is a separate portable MCP Apps host slice, not a persona feature.
  Pin the agreed protocol schema, stage source-labelled proposals without replacing
  existing composer text, bind them to the originating account/chat/app instance,
  and require the user to send. Do not advertise unsupported capabilities.
- Unused-upload management/quotas, import/export and model-assisted persona drafting
  are deferred until real usage justifies them.
- Real concurrent PostgreSQL locking, a full signed-in acceptance matrix (including
  account switching), subjective tone/visual acceptance and iOS device checks are
  not established by SQLite/component tests. Record any operator acceptance
  separately; do not infer it from build success. A full backup restore was not
  exercised during the schema repair.
- The next independent product sprint remains My Workspace file management/basic
  editing. Image companion work can proceed in parallel; it is not a reason to
  extend this persona branch indefinitely.

## Local Work Preserved

The original backend checkout retains its small Neutral preview and the unrelated
user-owned `app/mcp/servers/web_research_v2 copy.py`. Neither is staged by this
closeout. Native signing-team changes remain in their existing worktree; do not
archive that worktree or switch the operator's active build checkout automatically.
