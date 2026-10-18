# Python Dependency Maintenance

## Dependency Contract

The supported bootstrap/test interpreter is CPython 3.11 (see `.python-version`).
Do not change Python, the runtime adapter, and model assets as an incidental
side effect of a package refresh. This baseline does not install or upgrade Pi,
Ollama, Docker, Tesseract, model weights, or the native client.

- `requirements.in`: directly used runtime packages, supported API families,
  and minimum security/compatibility versions.
- `requirements.txt`: generated exact versions and distribution hashes,
  including transitive and platform-specific dependencies.
- `requirements-dev.in`: test/audit tools constrained to the runtime baseline.
- `requirements-dev.txt`: generated complete runtime plus development baseline.

The `.in` files express intent. The generated `.txt` files make installations
reviewable and repeatable; they are not another hand-maintained list of historical
pins. Do not replace them with an unbounded `pip install --upgrade` on the server.
Generated headers fingerprint their inputs; regression tests reject stale files
and runtime/development baseline drift.
Hashes verify distributions against the reviewed baseline, not the security of
their code. The resolver uses PyPI; do not add arbitrary package indexes to work
around a conflict. Bootstrap installs wheels only and does not execute source
builds. Universal markers retain macOS/Linux differences; availability on every
architecture or later Python release is not implied.

Runtime dependencies now explicitly include Pydantic, cryptography,
Hugging Face Hub, MCP types, and the optional OCR Python adapter rather than
relying on unrelated transitive installs. SQLAlchemy stays below 2.1 because the
current PostgreSQL retrieval adapter requires it; APScheduler stays on its 3.x
API. These are active compatibility boundaries, not forgotten pins. The umbrella
`llama-index` distribution and its OpenAI LLM integration remain unnecessary:
Fruitcake uses LiteLLM and disables LlamaIndex's LLM layer.

## Refresh And Verify

Use an operator-installed uv 0.13.1 in a maintenance environment. uv is required
only to generate the baseline, not to start Fruitcake. Regeneration upgrades
within the declared ranges; it is an explicit maintenance action.

```bash
./scripts/update_dependencies.sh
python3.11 -m venv /tmp/fruitcake-dependencies-test
source /tmp/fruitcake-dependencies-test/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
python -m pip check
./scripts/audit_dependencies.sh
LITELLM_LOCAL_MODEL_COST_MAP=True HF_HUB_OFFLINE=1 python -m pytest -q
```

Use a new temporary directory if that example environment already exists.
Inspect both the direct version changes and transitive changes. Test auth,
OAuth state, uploads/extraction, MCP transports, Ollama translation/streaming,
and cached embedding loading. Unit tests alone do not verify live model output,
production PostgreSQL, all calendar services, or native UI behavior. Fresh model
downloads and arbitrary remote model code are not part of a dependency refresh.

The backend workflow installs the development baseline, checks consistency,
audits the installed environment, and runs integration smoke tests. Its weekly
scheduled run also catches new advisories against an unchanged baseline.

### Refresh Verification (2026-10-18)

The replacement environment installs on Apple Silicon / Python 3.11, passes
`pip check`, and has no unexcepted findings in its installed-package audit. The
unsuppressed audit still reports the one NLTK advisory documented below. A
Linux x86-64 / Python 3.11 wheel resolution also succeeds; that is not a Linux
runtime test. The existing cached BAAI embedding model loads offline and returns
384-dimension text/query vectors, and the real BM25 adapter retrieves correctly.

The installed-Pi scripted-localhost regression run passes 298 tests, with six
opt-in tests skipped (including Docker fixtures). The broad suite's initial run
passes 1,322 tests and skips 55. It exposes ten existing failures reproduced
against unchanged main and the original environment: agent preset metadata,
configured-executor validation/history, loop-stop message expectations, task
profile history, and two task-runner tests. They are not suppressed or repaired
as part of this refresh. An additional compatibility failure was a test relying
on FastAPI's private flat route representation; it now checks public OpenAPI
paths instead and passes. Authentication, OAuth, extraction, MCP transport,
streaming, baseline, installer, and lifecycle regressions pass independently.
The final exact-baseline acceptance run passes 93 tests. The broad run used the
initial FastAPI 0.144.0 candidate; regeneration selected 0.145.0, which was synced
only into the disposable environment and rechecked by the audit and focused
acceptance suite. No broad-suite pass is claimed for that final revision.

These results do not claim a completely green broad suite or live production
acceptance. At the end of isolated validation, the original `.venv` and live
database had not been upgraded.

### Local Environment Rollout (2026-10-18)

With the backend and reloader stopped, the original environment was preserved
as `.venv.before-dependency-refresh-20261018-105501` (excluded from Git). A new
CPython 3.11 environment was created at the final `.venv` path and installed from
the hash-verified runtime and development baselines. No staging environment was
relocated. `pip check`, the 93-test acceptance suite, and the dependency audit
pass with the documented NLTK exception; retired authentication packages are
absent. The cached embedding model also loads offline on `mps:0` and produces
384-dimension vectors. Normal bootstrap recognizes the new requirements stamp.
Docker, the live database, and migrations were not changed in that initial
environment rollout. The backend was left stopped pending normal operator startup.

### Application Soak Follow-Ups (2026-10-18)

Review of saved post-update chats found application-level presentation and retry
issues, not evidence of an Ollama server downgrade or a dependency crash:

- News cards now accept inline `**Headline**: summary` items and prefer the
  evidence-backed extraction when mixed Markdown would otherwise omit stories.
- Follow-ups can render cards from paired RSS results in the same loaded
  conversation history. Reuse is bounded to the last 100 messages, at most four
  results and 96,000 characters. Only links matching that evidence are promoted;
  historical reads are not recorded as newly executed tools or fresh searches.
- Once a turn has tool execution records, validation retries are answer-only
  repairs with tools disabled at both the offered-schema and execution boundary.
  They receive bounded saved results and source details instead of rerunning
  successful reads, artifact creation, or other mutations. Visible anchor links
  in successful, validated and sanitized HTML artifacts count as citations.
- Usage provider labels follow the selected model's provider rather than the
  global backend default. Local Ollama calls are labeled `ollama`, including
  responses returning an unprefixed model name. Existing usage rows are unchanged.

Offline replay of the saved briefing produces all 12 headlines instead of nine;
the saved follow-up produces a three-story card without another feed fetch.
No production records were rewritten and no live model/tool requests were
replayed. Existing saved cards are not backfilled.

The focused chat, content, artifact, usage, runtime-event, streaming and model
profile regression run passes 359 tests. Two previously reproduced baseline
loop-stop expectation failures are deselected; this is not a full-suite pass.

For manual soak testing, request a feed briefing, filter its existing stories in
a follow-up, then request article details. Check that cards keep their headlines,
details include source links, and citation repair does not repeat tool activity.
An explicit request for newer news should still fetch fresh evidence normally.
Also check new usage rows for the correct provider. Native news cards do not
require HTML; the static HTML sanitizer's styling and script restrictions remain.

## Native Runtime Baseline

The reviewed maintenance targets are CPython 3.11.17, PostgreSQL 16.15, pgvector
0.8.7 and Tesseract 5.5.3. CI reads `.python-version`. Bootstrap still uses
`python3.11`; installing dependencies into an existing environment does not
upgrade its interpreter. Preserve the old environment and create the replacement
at its final `.venv` path after upgrading Python. Do not bump the Python minor
series as part of a security patch rollout without a separate compatibility pass.

The PostgreSQL 16 Bookworm image is pinned to an explicitly tested pgvector tag
and manifest digest in `docker-compose.yml`. Port 5432 is published only on
`127.0.0.1`. The Python `pgvector` package is a client adapter, not the installed
server extension. Replacing the image does not update existing extension catalogs.
For each existing database, after backup and restore validation, run:

```sql
ALTER EXTENSION vector UPDATE TO '0.8.7';
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

This is an operator maintenance step, not an application schema migration.
`CREATE EXTENSION IF NOT EXISTS` on normal app startup does not upgrade it.
New databases use the image's default extension version. PostgreSQL remains on
major version 16; changing the major version requires a separate migration.

### Backup And Rollback

Stop the backend before the final backup, including its reloader. Save a custom
`pg_dump` archive, role definitions, the old image identity, Compose configuration
and table counts. Stop PostgreSQL cleanly before taking a physical volume snapshot.
Restore-test the logical backup against the candidate image and the cold snapshot
against the original image using disposable volumes and no published ports.
Never run two PostgreSQL servers against the same data directory.

The local maintenance artifacts are private and Git-ignored under
`storage/runtime-upgrade-20261018-184426/`. They include the pre-upgrade image
identity, final dump, cold volume archive, checksums, table counts and restore
test logs. The prior environment is retained as
`.venv.before-runtime-refresh-20261018-184426`; its Python executable is anchored
to the retained 3.11.14 keg rather than the moving Homebrew `opt` symlink.
Console-script paths in a moved environment still reference the original path;
invoke its Python explicitly for checks, and restore it to `.venv` for rollback.

Rollback after an extension upgrade requires restoring the saved data, not merely
selecting the old container image or issuing an extension downgrade. Restore the
cold snapshot into a **new** volume with the saved original image, verify it, then
switch configuration while the backend is stopped. Do not overwrite the current
volume or discard post-upgrade writes without deciding how to preserve them.

Homebrew upgrades use `HOMEBREW_NO_INSTALL_CLEANUP=1` to retain previous kegs.
Python and OCR also depend on shared native libraries, so an environment backup
alone is not a full host-runtime rollback. Retain the Homebrew transaction logs
and old dependency kegs. Do not run cleanup until the soak period is accepted.
The supported Python bottle selects [OpenSSL 3.5 LTS](https://openssl-library.org/post/2025-04-08-openssl-35-final-release/); verify TLS and certificate
loading after this change rather than assuming a numerically lower OpenSSL
minor version means an unsupported release.

### Local Runtime Rollout (2026-10-18)

The four maintenance targets above are installed and the database extension
catalog is upgraded. The candidate image reports PostgreSQL 16.15 and the live
catalog reports vector 0.8.7. Every public table row count matched before and
after the database replacement, before normal application writes resumed.
All 6,871 document vectors remain 384-dimensional; nearest-neighbor queries pass.
The logical restore/extension upgrade and the cold rollback restore were tested
in separate network-isolated containers, then those disposable containers and
volumes were removed. Backups and the original image remain available.

The final-path `.venv` was rebuilt on 3.11.17 from the unchanged, hash-verified
runtime/development lockfiles. `pip check` passes. Verified HTTPS and certificate
loading, actual scanned-PDF OCR through the application extractor, native library
linkage, and the configured cached embedding model on Metal all pass. The audit
has no unexcepted findings; the existing temporary NLTK exception still applies.

Homebrew itself needed updating from 6.0.6 to 7.0.9 to understand the current
formula post-install steps. Targeted Python/Tesseract upgrades also updated their
native dependencies, selecting OpenSSL 3.5.9. No general `brew upgrade` or old-keg
cleanup was run. The interrupted certificate/Python post-install steps were
completed with the updated installer; a broken template-placeholder symlink
left in the checkout by the old installer was removed.

The focused acceptance run passes 392 tests, with the two already known loop-stop
expectation failures deselected. The broad run reports 1,358 passes, 55 skips and
11 failures: the ten previously identified baseline failures plus a checkout
serialization test that passes standalone on both the new 3.11.17 and retained
3.11.14 environments. This is not a claim of a fully green suite, nor proof that
the intermittent checkout failure is harmless.

Normal `scripts/start.sh` startup, migrations, RAG initialization, the coding
session service and `/health` all pass after replacement. The temporary validation
server was stopped at the operator's request; the operator subsequently restarted
the backend normally. Startup reported
FieldKit unavailable because nothing is listening at its configured localhost
port 47834; other enabled MCP connections and client API requests succeeded.
No FieldKit configuration was changed.

## Node And Node-Based Integrations (2026-10-18)

The local Node runtime was updated from 24.10.0 to Homebrew `node@24` 24.22.0,
remaining on the 24 LTS line rather than following the unversioned formula to
Node 26. Pi remains at the adapter-supported 1.3.0. Its installed-runtime tests
against a scripted localhost model pass: 298 passed, six opt-in tests skipped.
No real model calls or Docker fixture tests are implied by that result.

Homebrew bundled npm 11.19.1, whose installed dependencies had advisories in
brace-expansion, undici, ip-address, http-cache-semantics and
postcss-selector-parser. npm 11.21.1 was staged and audited, then installed with
installation hooks disabled into `~/.local`, without changing Pi. With
`~/.local/bin` ahead of `/opt/homebrew/bin` on PATH, both `npm` and `npx` now use
11.21.1. The unused Homebrew copy remains installed; this is not a claim that
every retained package or historical cache on the machine is vulnerability-free.
Use `--global --prefix "$HOME/.local"` for intentional user-global installs;
do not manually patch Homebrew's bundled npm tree.

The enabled Elgato bridge remains 0.1.7. Its previous cached MCP SDK 1.30.0 had
[GHSA-6qxp-vccf-f47h](https://github.com/modelcontextprotocol/typescript-sdk/security/advisories/GHSA-6qxp-vccf-f47h).
The advisory concerns HTTP OAuth clients, not this stdio server, but the compatible
1.31.0 patch is now explicitly selected alongside the bridge. Both packages are
specified to `npx`, creating a separate cache from the old single-package launch.
Installation hooks are disabled; the explicitly selected bridge executable still
runs. Initialization, tool discovery and clean disconnection pass through
Fruitcake's MCP client, including a final probe using the configured user-global
npx. The runtime-configuration and MCP transport regression suite passes all 21
tests; its first sandboxed attempt could not bind four local fixture ports, so
the suite was rerun with loopback permission. No device-action tools were invoked. Only `bridge_status`
was advertised during the probe; full Stream Deck action availability remains a
live application check. Existing backend children were not replaced: restart the
backend normally to pick up the new config and Node runtime.

Homebrew's Node bottle uses OpenSSL 4.0.3. Python retains its versioned OpenSSL
3.5.9 dependency and passes its SSL import check. The generic Homebrew OpenSSL
prefix links now select version 4; the `openssl@3` keg and versioned `opt` link
remain installed. No old-keg cleanup was run.
Python certificate-validated HTTPS and the existing backend's `/health` both
pass after the final Node/npm changes; the backend was not restarted for this pass.

### Node Rollback Artifacts

Private, Git-ignored transaction logs and advisory reports are under
`storage/node-upgrade-20261018/`. Node 24.10.0 and simdjson 4.0.7 are retained;
the old binary needs its old simdjson ABI, not the current `opt/simdjson` link.
This isolated version check passes without changing the active runtime:

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/Cellar/simdjson/4.0.7/lib \
  /opt/homebrew/Cellar/node/24.10.0/bin/node --version
```

That is a rollback smoke check, not a complete application rollback test. A full
rollback must restore matching library/prefix bindings with the backend stopped.
The pre-existing real `/opt/homebrew/lib/node_modules/npm` directory was moved,
not deleted, into `npm-global-prefix-before/` in the private maintenance directory
so Homebrew could link the new npm. Pi's separate `~/.local` installation was not
modified.

The final installed-package advisory scan covers Pi, the new Elgato npx project
and the active user-global npm: 321 distinct package names, 361 installed package
instances, no skipped symlinks and no returned advisories. The report and the
scanner used are preserved privately as `final-node-package-audit.json` and
`audit-installed-node.mjs` in the maintenance directory. The scanner submits only
public package names and exact versions to npm's advisory endpoint. It covers
installed manifests, not code embedded inside bundles or native binaries, and is
a point-in-time check rather than a reproducible transitive lock for npx.
Re-audit after a fresh npx resolution or package refresh.

Node has [announced another security release for October 21](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases),
including high-severity fixes for Node 24. Those fixes are not available in the
installed 24.22.0 release; recheck after publication. This dependency pass is not
a container-OS vulnerability scan, credential rotation or full security audit.

## First Rollout Of The Refreshed Baseline

Do this only after validation and with the backend stopped. Preserve the existing
environment as a rollback artifact. Do not transplant a staging virtualenv into
`.venv`: console-script shebangs embed the environment's original path.

```bash
# From the repository root, after stopping the backend:
mv .venv ".venv.before-dependency-refresh-$(date +%Y%m%d-%H%M%S)"
./scripts/start.sh
```

Bootstrap creates `.venv` at its final path, installs the runtime baseline, and
runs `pip check` before saving its requirements stamp. It refuses an in-place
upgrade from environments containing retired `python-jose`/`passlib`, because
pip installation does not remove obsolete distributions. For local development,
install `requirements-dev.txt` into the new environment as a separate step.
Normal startup remains `./scripts/start.sh` (or `./start.sh` from `scripts`).

Do not delete the saved environment until acceptance. Rollback requires stopping
the backend, restoring the matching application revision and environment to the
original `.venv` path, and considering any new password hashes described below.
No database schema migration or live environment replacement is performed by
the dependency generation scripts.

## Authentication Compatibility

`python-jose` is replaced by PyJWT. Existing HS256 token signatures, claims,
expiration checks, and access/refresh distinction are preserved. OAuth state
uses the same explicit configured algorithm restriction. Tests include a token
generated by the retired library; no mass token invalidation is required by
this library replacement.

Passlib and its obsolete bcrypt restriction are removed. New passwords are hashed
with Argon2id. Existing `$2a$`, `$2b$`, and `$2y$` bcrypt hashes remain verifiable,
including Passlib's legacy UTF-8 byte truncation at 72 bytes. New Argon2id hashes
do not truncate passwords. Password resets are not required, and existing hashes
are not rewritten automatically. Old application versions using bcrypt-only
Passlib cannot verify newly created Argon2id hashes: account creation or password
changes after rollout must be considered before rolling back the application.

## Advisory Exception

- Package: NLTK, currently transitive through LlamaIndex.
- Advisory: `PYSEC-2026-3740` / `GHSA-8mgp-746c-j5xp` / `CVE-2026-81726`.
- Reviewed: 2026-10-18. Review deadline: 2026-11-18.
- Upstream fix: none listed at review time.
- Scope: model-artifact read/write APIs bypass NLTK's `pathsec` sandbox.
- Applicability: Fruitcake does not expose these model persistence APIs, let
  users choose their paths, or rely on NLTK `pathsec` to isolate agent access.
  Retrieval tokenization is not a file sandbox.
- Disposition: explicit temporary exception, not a claim that NLTK is safe for
  arbitrary model imports. Reassess if upstream publishes a fix or application
  usage changes. Remove the exception after updating and testing.

`scripts/audit_dependencies.sh` suppresses only this advisory and fails after the
review deadline. Other findings remain failures. Run `python -m pip_audit --local`
to see the unsuppressed report. See the [upstream advisory](https://github.com/advisories/GHSA-8mgp-746c-j5xp).
