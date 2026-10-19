# Independent Coding Review Runtime (R1)

R1 adds a fresh, read-only AI reviewer over R0 retained captures. It does not
modify the native interface, enable GitHub delivery or workers, edit code, execute
tests/commands, reuse builder memory, or grant networking/MCP access.

## Operator Rollout

Both review flags default off. This implementation does not change the local
`.env`, migrate the running database, or start a real model turn.

1. Stop all backends sharing the database, including older versions.
2. Apply migrations using the existing maintenance-role command:
   `.venv/bin/python scripts/database_admin.py migrate upgrade head`.
3. Confirm revision `059_coding_reviews`; it follows capture migration 058.
4. Deliberately opt in with `CODING_REVIEW_ENABLED=true` and restart one backend.
   This implies capture storage even if the capture-only flag is off.
5. Use an authorized disposable managed checkout and a declared local reviewer
   model profile for headless acceptance before enabling any future native UI.

`CODING_RUNTIME_ENABLED` and the operator-installed Pi 1.3.0 executable remain
prerequisites. Reviewer reads honor `CODING_REQUIRE_READ_APPROVAL`. No Pi upgrade,
new model/provider, or dependency installation is part of this slice.

The normal `scripts/start.sh` path delegates to bootstrap, which also runs
`database_admin.py migrate upgrade head`. Starting this branch normally will
therefore apply 059 even with both review flags off; schema migration and reviewer
enablement are separate actions.

Rollback disables AI reviews and restarts. Existing R0 captures may stay enabled
separately. Do not downgrade populated review tables, delete retained findings,
remove checkouts or revoke unrelated grants as rollback cleanup. Both new
migrations refuse destructive downgrades with retained data.

## Explicit Start And Isolation

Start requires an owned, authorized, idle session with a ready managed checkout.
Model selection is independent of the parent builder profile. Read/profile policy
is checked at admission, startup, captured reads and approval resolution, and by a
watchdog during otherwise silent model execution. Historical review access still
requires an active user/workspace grant and an undeleted originating session.

A review atomically admits a NEW capture and holds the R0 checkout reservation
until its runtime has closed. It cannot adopt an older capture/options observation.
Busy work is rejected, never queued or used to stop another run. An identical
idempotency key replays the admitted operation; changed inputs conflict.

Each reviewer gets a new isolated Pi process with empty history, no personas or
memory, and only a privately materialized capture after/context view as its cwd.
There is no `.git`, source-folder fallback, builder process reuse, repository
context files, extensions/skills/MCP discovery, edit or shell tool.

The initial prompt contains capture metadata and file/side sizes, not inline
patches or source. This avoids delivering source twice when the reviewer reads
it. The only model tool is `fruitcake_review_read(path, side)`. It uses a private host
result channel, not direct filesystem access. The host checks capture membership,
reads integrity-verified retained bytes and returns the entire file or a named
unavailable/denied result. Before sides come only from retained blobs. Unselected
untracked files remain excluded by R0. Built-in read/write/bash/session/tool-set
RPC bypasses are denied independently. This is a trusted-local tool boundary,
not an OS sandbox for a malicious installed runtime.

`before` and `after` results prefix each original source line with `L<number>|`.
The header and labels are presentation only; evidence must quote the source
after the separator. Source whitespace, line endings and final-newline state
are preserved. `patch` explicitly returns the selected file's retained unified
diff; it is not a source side for finding anchors. A finding must cite a selected
before/after side successfully delivered through an approved captured read.
Denied, unavailable, context-only or patch-only reads do not authorize a finding
on unseen selected source. There is no guessed citation repair or reanchoring.

## API

All endpoints use the existing authenticated `/coding` roles, owner checks and
no-store responses. No reviewer execution occurs from opening setup/history.

| Route | Contract |
| --- | --- |
| `GET /status` | Separate capture and `ai_reviews` capabilities |
| `GET /sessions/{id}/review-options` | Scope/base/path observation plus authorized reviewer profiles and supported efforts |
| `POST /sessions/{id}/reviews` | Explicit fresh capture and review admission; 202 |
| `GET /sessions/{id}/reviews?limit=50&offset=0` | Bounded history, at most 100 per page |
| `GET /reviews/{id}` | Snapshot provenance, status, findings, qualifications, retained content status and live applicability |
| `GET /reviews/{id}/events?after=0&follow=true` | Durable independent SSE cursor, heartbeats and owner/grant rechecks |
| `POST /reviews/{id}/stop` | Idempotent Stop of only that review; cleanup precedes reservation release |
| `GET /reviews/{id}/approvals` | Bounded ordered captured-read decisions; no human wait expiry |
| `POST /reviews/{id}/approvals/{approval_id}/resolve` | Strict approve/deny; chat text cannot approve |
| `PATCH /reviews/{id}/findings/{finding_id}` | Dismiss with nonempty reason or explicitly reopen; actor/reason/time audited |

Start body:

```json
{"scope":"checkout_changes","paths":["src/example.py"],"model_profile_id":1,"effort":"default","client_request_id":"new-review-key"}
```

Finding disposition body:

```json
{"disposition":"dismissed","reason":"Expected behavior under this documented invariant"}
```

SSE uses `review_id`, sequence, kind, schema version and allowlisted payloads,
separate from builder run history. `Last-Event-ID` resumes without writing or
duplicating findings. A disconnected observer does not cancel the reviewer.
Read approvals have no timeout; execution budgets pause during human waits.

## Output And Evidence

The model is instructed to return JSONL finding records, then exactly one summary
record. Parsing also accepts formatting whitespace inside/between complete JSON
objects, so pretty-printing is not a failed review. Schema validation stays
strict: no Markdown/code fences, arrays, prose final reports or arbitrary
model-supplied disposition fields.
Findings are validated against captured membership, before/after line counts and
exact evidence within the anchored range. Invalid/malformed/oversized output is a
named failed review, not a successful empty report. Already validated records are
retained as partial findings on later output failure, Stop or interruption.

The independent Pi adapter now supplies the reviewer rubric as its system prompt,
instead of inheriting the general coding-assistant role. Bounded assistant
narration attached to a tool-call turn is not a final report and is not retained.
Final prose still fails; fields, paths, line ranges and exact evidence are never
repaired or guessed. Model output-token exhaustion is explicitly qualified as
`review_output_limit`, not a malformed/empty report. Coverage records safe output
byte counts, stop reason and a validation reason; visible limitations explain
schema/format, capture-membership, line-range and evidence failures separately.
No raw model output or hidden reasoning is persisted for diagnosis.

Severity values are `blocker`, `should_fix`, `consider`. Dispositions are only
`open` and `dismissed`; live edits never automatically mark a finding addressed.
A re-review starts fresh and carries no dispositions or reviewer conversation.
No findings means **No findings reported**, not correct/complete/verified code.

The retained capture remains authoritative. Later live changes may mark current
applicability stale; they do not rewrite old findings or their reviewed diff.
Expired or corrupt content is unavailable and never replaced with a live file.
R0's owner-only storage, historical/current privacy checks and retention bounds
remain in force. Active reviews and cleanup-quarantined reservations pin content,
including an indefinitely waiting human approval. Settled metadata/findings remain
after content expiry. The private Library ingestion exclusions also cover views.

Host coverage distinguishes captured scope/context, patches/text delivered to the
runtime, successful captured reads (paths/sides), complete read failures/denials,
byte budgets and truncation. Mode-only patches do not count as delivered file
text. These measurements do not prove provider receipt or semantic coverage.
`tests_executed` is always false; the summary cannot override that qualification.

## Reasoning Provenance

Selectable effort is the model profile's declarations intersected with the
tested Pi mapping: `none/off -> off`, `minimal`, `low`, `medium`, `high`.
Every profile also offers explicit `default`. Unsupported explicit efforts are
rejected, not silently replaced. Default resolves from the declared profile
default, or off, and is recorded separately from the requested selector.

Only the reviewer model document enables reasoning for declared supporting
profiles; builder model documents/settings are unchanged. Before any prompt, Pi
must acknowledge the resolved setting and return matching state readback.
Mismatch/unsupported runtime level fails without inference. Records distinguish
requested, resolved, sent, RPC acknowledgement, readback, observation source/time,
runtime version and **provider confirmation = null**. Scripted outgoing request
tests prove the adapter's `reasoning_effort` mapping, not provider-internal effort.
No raw hidden reasoning is retained or sent through the public review events.

## Bounds, Stop And Recovery

R0 capture quotas still apply. Reviews share the configured coding process budget
with builders and the bounded capture worker budget. Additional review bounds:

- 100 findings, 64 KiB model output, 2 KiB exact evidence per finding.
- 128 captured read attempts; complete file or explicit refusal, never a prefix.
- Conservative input byte budget: one UTF-8 byte per declared context token
  remaining after the output reserve and 8,192 tokens of protocol/safety
  allowance, at most 512 KiB. The initial extra halving was removed after it
  rejected a three-file capture before any inference. For a 65,536-token profile
  with an 8,192-token output reserve, the allowance is 49,152 bytes, not 24,576.
  This is a defensive bound, not exact provider tokenization. Exceeding it asks
  for narrower scope; source is never shortened to fit. Complete read
  presentation bytes, including line labels and headers, count against this
  allowance. Raw retained bytes are recorded separately as
  `source_bytes_delivered`; neither measure proves semantic review coverage.
- Existing execution/startup/Stop budgets; human approval waits are excluded.
- At most 2,000 durable events and 16 KiB per event; no raw provider output/errors.

October 19 input-limit correction: the first live three-file attempt retained a
30,024-byte prompt but failed against the initial 24,576-byte host allowance
before any model startup. The corrected 49,152-byte allowance accepts that exact
retained input without truncation. Coverage records `prompt_bytes`,
`input_byte_budget` and the byte-budget basis; future rejected prompts include
the required/allowed sizes and scope/context guidance in visible limitations.
Existing failed review records remain immutable and are not retroactively marked
successful. Start a new review to test the corrected code. Reviewer regression
suite after this change: 34 passed, including complete three-file input,
oversized UTF-8 input rejection and complete-read budget refusal without a prefix.
Five installed-Pi scripted reviewer checks also passed; no real inference was
started for this diagnosis or regression run.

Stop cancels the owned operation once, joins process cleanup, cancels pending
approvals, preserves partial findings and only then releases its fenced checkout
reservation. Repeated Stop or a disconnected HTTP caller cannot interrupt that
cleanup. Failure to close quarantines the runtime and keeps its reservation and
capture pin until cleanup succeeds; it is not reported as safe completion.

Startup acquires the R0 sole-supervisor fence before marking old active reviews
interrupted, cancelling old approvals and reclaiming reservations. Findings and
events survive; no process memory, model run or queue is resumed automatically.

## Initial Verification Checkpoint (October 19, 2026)

R0 is checkpointed as `4fa91a9`. R1 is implemented on
`codex/coding-review-runtime` and remains uncommitted pending operator review.

- Full isolated coding suite: 362 passed, including scripted installed-Pi and
  disposable-container checks.
- Final reviewer-focused rerun after the last refinements: 32 passed.
- Disposable PostgreSQL role/migration, lifecycle, privacy and linked-source
  integration checks: 50 passed; existing deprecation warnings remain.
- The running backend was healthy at revision 058 with both review flags off.
  The operator's normal startup between sessions explains that migration.
  No live migration was run by the agent; 059 remains unapplied at this checkpoint.
- No native source changes or real-model inference were performed. Scripted Pi
  checks do not constitute provider behavior or native visual acceptance.

## Next Gate

October 19 citation/input-delivery correction: the latest project attempt ran
about 25 minutes and failed with the safe subtype `evidence_mismatch`. Its
30,301-byte initial prompt duplicated source subsequently requested through
tools; the host refused two reads after cumulative input exhausted the allowance.
The rejected quote itself was not retained, so its exact wording/line cannot be
recovered. The host still rejects mismatched evidence rather than turning an
invalid review into a successful empty report.

The replacement metadata-only prompt and complete numbered reads were replayed
offline against the same integrity-verified retained capture. The prompt was
2,908 bytes; all three requested file sides plus the repeated first read totalled
43,542 bytes including that prompt, below the unchanged 49,152-byte allowance.
Removing line labels reproduced every original source byte. This establishes
input delivery and citation presentation, not actual Qwen project acceptance.
No real model inference was started for this correction. Regression checks cover
line labels, exact citations, lazy delivery, UTF-8/read limits, denied-source
findings, retained patches and the installed-Pi scripted protocol: 58 focused
tests plus five installed-Pi checks passed. The latter require a temporary
localhost fixture socket; a sandbox-denied attempt was rerun with socket
permission. No native rebuild, migration, model change or `.env` change is required.

October 19 output-handling diagnosis: the next project attempt reached the model
and failed `review_output_invalid`, but its original record lacks a subtype; do
not claim that its exact invalid text/anchor was recovered. A bounded retained
input replay under the old contract timed out without retaining raw output. The
subsequent local Qwen smoke check used only a two-line synthetic file and passed
the revised protocol: one synthetic read, two schema/anchor-validated findings
and one summary. This is real local protocol evidence, not validation of the
reported findings' usefulness or acceptance of the operator's project review.
After the output changes, 52 reviewer/parser/supervisor and installed-Pi scripted
checks passed. No native client rebuild or migration is required for this fix.

The operator restarted the backend and applied 059 on October 19; both review
flags remained off in the subsequent read-only check. The operator then explicitly
enabled AI reviews for testing. No agent-run live migration or feature enablement
was performed.

R2 is implemented locally on the paired native `codex/coding-review-client`
branch: Review setup/history, Files/Findings, historically anchored Show Reviewed
Diff, honest qualifications, read approvals, dismiss/reopen and Draft Correction.
Native rollout and acceptance are documented in `Docs/CODING_AI_REVIEW.md` in that
repository. The operator subsequently reported completing a real review/correction
pass and accepted the polish and Explorer appearance. The v0.7.43/v0.2.4 release
checkpoint includes these paired changes. Automated fixtures and that feedback do
not establish exhaustive signed-in focus/scroll/window acceptance. GitHub delivery,
orchestration, automatic fixes and builder reasoning controls remain outside this
work. Code Appearance And Syntax Highlighting is the next approved branch; see
`code_appearance_plan.md`.
