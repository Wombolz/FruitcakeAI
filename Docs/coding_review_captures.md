# Coding Review Capture Foundation (R0)

This backend-only slice retains bounded review evidence. It does **not** run an
AI reviewer, return findings, expose reviewer reasoning controls, or change the
native interface. Existing builder permissions and model profiles are unchanged.

## Rollout And Rollback

The default is `CODING_REVIEW_CAPTURE_ENABLED=false`. No live migration or local
configuration change is part of implementing this slice.

For a deliberate operator rollout:

1. Stop all backends sharing this database, including older builds.
2. Use the existing migration role, not the restricted application role:
   `.venv/bin/python scripts/database_admin.py migrate upgrade head`.
3. Verify revision `058_coding_review_captures` before enabling the flag.
4. Opt in with `CODING_REVIEW_CAPTURE_ENABLED=true` and restart one backend.
5. Test using an owned disposable managed checkout before real work.

Rollback disables the flag and restarts the backend. Do not downgrade a populated
capture database or delete retained evidence as cleanup. The migration refuses
downgrade while capture rows or reservations exist. With the flag off, no new
capture table is queried; this is tested against the prior schema.

## Admission And Ownership

Every updated coding supervisor, including feature-disabled instances, acquires
PostgreSQL session advisory lock `(719364, 58001)` on a dedicated connection
before recovery. A second supervisor is rejected. Admission verifies that the
connection still owns the lock; a lost connection fails closed and requires a
safe restart. This consumes one database connection. File-backed SQLite tests
use a nonblocking advisory file lock; in-memory SQLite is unsupported.

When captures are enabled, a database reservation with a unique checkout ID,
operation kind/ID, random token and service epoch serializes builders, captures,
Git delivery and checkout removal. Busy requests are rejected, not queued or used
to stop existing work. Late release cannot clear a different epoch/token.
Restart recovery marks old captures interrupted before reclaiming reservations,
and only after acquiring supervisor ownership. This is a single-backend design,
not distributed worker orchestration or a hostile-writer filesystem sandbox.

## API

All routes are under authenticated `/coding`, retain existing role/grant checks,
and use no-store responses. Cross-owner IDs are not found. Capture access requires
an active owner/workspace grant and an undeleted originating session.

| Route | Purpose |
| --- | --- |
| `GET /status` | Separate `review_captures` capability; `ai_reviews` remains false |
| `GET /sessions/{id}/review-options?scope=checkout_changes` | Bounded scope/base/HEAD/path observations; no capture/model execution |
| `POST /sessions/{id}/review-captures` | Explicit capture task; 202, owner-scoped idempotency key |
| `GET /review-captures/{id}` | Status, retained inventory/limitations, integrity and live applicability observation |
| `GET /review-captures/{id}/diff?path=...` | Complete retained patch, never a live-worktree fallback |

Create input:

```json
{"scope":"checkout_changes","paths":["src/example.py"],"client_request_id":"operator-chosen-key"}
```

`checkout_changes` compares the checkout's recorded baseline to current content,
including committed changes since that baseline. `uncommitted` compares current
HEAD to current content, regardless of staging. Nonignored untracked files must
be selected explicitly; unselected untracked files are not read as supporting
context either. Renames are delete/add. No arbitrary refs or remote fetch.
Source-only sessions, missing baselines and unready/conflicted checkouts cannot
fall back to the source repository. Options report **path-policy eligibility**,
not a promise that binary/size/link checks will pass capture.

Matching idempotent retries return the original task. Different inputs with the
same key return 409. Busy returns 409, quotas 413, and expired diff content 410.
Selected unsafe/unavailable files fail the capture rather than being truncated.
Supporting-context omissions are counted and make the capture limited.

## Evidence And Privacy

Content lives in `storage/.coding_review_captures/<owner>/<capture-id>/`, separate
from checkouts. Directories are owner-only 0700; files are exclusive-create 0600.
Content-addressed blobs and a versioned manifest are fsynced and atomically
published. Access checks the DB-anchored manifest hash, identity and blob hashes
and sizes. Deleted/missing/tampered bytes disable evidence access. A live file is
never substituted. Later live edits may mark applicability stale without changing
the historical diff. Unobservable applicability is unknown, not current.

Capture reads use no-follow descriptor traversal, regular-file/single-link and
identity checks, strict UTF-8/NUL limits, hardened local Git blob reads with
filters/hooks/helpers disabled, and two bounded repository observations. This
detects ordinary concurrent movement, not an atomic whole-filesystem snapshot.

Hidden paths, credentials/private filename patterns, dependencies, caches,
repository metadata and known storage directories are excluded on historical and
live sides. Both current and baseline `.gitignore` rules are respected, including
tracked files. Historical ignore rules are evaluated by Git from bounded private
temporary policy files. Quote/backslash/control-character paths are unsupported.
This conservative filename policy can exclude harmless files and **does not
detect all secrets** inside otherwise eligible source. Capture disclosure and
careful scope selection remain necessary. Library linking/scanning explicitly
rejects private capture paths, including resolved aliases.

## Bounds And Retention

| Bound | Initial value |
| --- | --- |
| Selected changed paths | 128 |
| File content per retained side | 256 KiB |
| Repository inventory | 4,096 paths; bounded Git output |
| Supporting context | 2,048 files |
| Retained source/context content | 32 MiB |
| Supporting-content read budget per observation | 64 MiB plus at most one bounded file |
| Retained patches / manifest | 2 MiB / 4 MiB |
| Historical ignore policies | 128 files / 2 MiB total |
| Capture duration / concurrent capture jobs | 30 seconds / 2, no queue |
| Unexpired captures per checkout / bytes per owner | 10 / 256 MiB |
| Content retention | 30 days |

Admission reserves 38 MiB until the task settles, then records actual bytes.
Interrupted crash recovery conservatively retains the reservation accounting
until expiry because publication may have completed before the crash. Expiry
purges settled content, preserving metadata/status. Active capture tasks are not
purged. Failed/interrupted captures count toward the retained-count quota; no
silent eviction occurs. R1 must add active-review pins before reviewer execution.

## Verification And Next Gate

Tests cover scope sides, unsafe/historical ignored content, malicious helpers,
change detection, owner/grant isolation, API validation, immutable/corrupt/expired
content, quota rejection, cancellation before task entry, shared admission,
fenced recovery and disposable PostgreSQL migration/default grants. The existing
shared-checkout test's fake-runtime typo (`block` vs `blocked`) is corrected;
new admission tests use explicit barriers rather than timing sleeps.

Installed Pi 1.3.0 is probed with scripted localhost fixtures: supported levels,
set/readback/reset and nonreasoning fallback. No real model is called. This is
runtime capability evidence, **not provider-confirmed reasoning effort**.

Local verification on October 18, 2026:

- Coding suite with installed Pi and offline Docker enabled: 332 passed.
- Final capture/recovery/service regression pass, including the added startup
  ownership-loss guard: 56 passed. These overlap the full suite; do not sum counts.
- Disposable PostgreSQL, application lifecycle, Library/privacy integrations:
  50 passed, with only existing deprecation warnings.
- Live read-only check: captures disabled, migration remains
  `057_coding_approval_wait`, backend `/health` returns HTTP 200.
- Native repository remains clean; no real model inference, live migration,
  local `.env` edit or feature enablement was performed.

CI now includes the isolated coding suite, private-evidence ingestion regression,
and disposable PostgreSQL admission test. Installed-Pi probes remain opt-in;
the local run above supplies compatibility evidence, not a claim of a CI run.

R1 is the next separate slice: fresh read-only reviewer runtime, validated
findings/events/Stop/recovery and truthful reasoning provenance. R2 is the native
client, gated on the remaining layout/focus walkthrough. No AI Review feature is
claimed complete by this foundation.
