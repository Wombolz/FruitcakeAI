# Container And CI Maintenance

Reviewed and rolled out locally: 2026-10-18, `codex/dependency-refresh` / PR #13.
This patches available fixes, not every open upstream advisory. No registry
publication or default-branch merge was performed.

## Reviewed Inputs

- PostgreSQL 16.15 / pgvector 0.8.7 stays on the same upstream engine and extension
  builds. `containers/postgres/Dockerfile` pins the upstream manifest digest,
  refreshes Debian Bookworm and replaces gosu 1.19 with the same verified source
  compiled using digest-pinned Go 1.26.9. No Go compiler is in the final image.
- The shell pins Python 3.11.17 / Debian Trixie, patches OS packages and removes
  pip, setuptools, wheel, packaging and ensurepip, including vendored vulnerable
  packaging modules. The MCP server uses only the standard library. This image
  deliberately does not provide pip; Python, unittest and GNU timeout still work.
- Both recipes use signed Debian snapshot `20261018T000000Z`. Index download
  errors fail the build (`APT::Update::Error-Mode=any`). Historical index expiry
  is disabled, not signature or HTTPS verification. The database bootstraps CA
  trust from its pinned builder before installing Debian's certificate package.
- PGDG sources are removed during OS refresh so changing repository contents
  cannot silently replace the pinned database engine. Future engine/extension
  patches require updating the base digest and repeating restore tests.
- `.dockerignore` allowlists only the shell and container recipe directories.
  Credentials, Git history, backups, model files and workspaces are not build
  inputs. Initial builds need internet; runtime command containers remain offline.

## Local Image Selection

Compose selects `fruitcake/postgres-vector:16.15-pgvector0.8.7-20261018-r1` with
`pull_policy: never`. Bootstrap builds a missing image from the local recipe, then
starts it with `--no-build`; ordinary subsequent starts reuse it. There is no
public-registry fallback for that first-party name. Rebuild explicitly after a
reviewed recipe update:

```sh
docker compose build postgres
docker build -t fruitcake/mcp-shell -f mcp_shell_server/Dockerfile .
```

Do not recreate a running database until its maintenance backups are ready.
Local versioned tags are selection names, not portable immutable digests. Record
actual image IDs after building, and scan that exact local image without pulling.
The promoted local arm64 image identities were:

```text
postgres: sha256:5acbb22e353efb6f8a737f69594931e33f85647d9c6391c08b68780db0e8d45e
shell:    sha256:80e36c296d8711c0dcaaeff4f8426b64c13160f8b89a7efc4097e91ca153b33c
```

Other architectures and fresh builds can have different output identities.
Every upstream base remains digest-pinned in its recipe.

## Scan Results And Applicability

Docker Scout 1.26.0 results, with no severity/advisory filters or new ignores:

| Scan | Critical | High | Medium | Low | Unspecified | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Original database image | 8 | 45 | 34 | 62 | 2 | 151 |
| Patched database image, inherited layers included | 4 | 40 | 31 | 62 | 0 | 137 |
| Patched database, effective exported filesystem | 0 | 10 | 8 | 61 | 0 | 79 |
| Original shell image | 0 | 3 | 7 | 27 | 0 | 37 |
| Patched shell image | 0 | 1 | 1 | 26 | 0 | 28 |

The layered database SBOM includes **both** the original Go 1.24.6 gosu at an
inherited layer and the Go 1.26.9 replacement at `/usr/local/bin/gosu`. Runtime
`gosu --version` reports 1.26.9. Scanning the exported effective filesystem removes
58 findings attached to the overwritten binary: four critical, 30 high, 23 medium
and one low. Both raw reports are retained; do not describe the layered scan as
having zero critical findings or ignore all Go findings globally.

The effective database filesystem has no critical findings and no high findings
with a listed fix. Its ten unfixed high findings are:

| Component | Advisory IDs |
| --- | --- |
| util-linux | CVE-2026-53613, CVE-2026-78409, CVE-2026-78410, CVE-2026-78408, CVE-2026-53615, CVE-2026-76642 |
| cyrus-sasl2 | CVE-2026-107161 |
| Perl | CVE-2026-82560 |
| OpenSSL | CVE-2026-84782 |
| zlib | CVE-2026-85091 |

The shell's remaining high finding is the same zlib advisory. The
[Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-85091)
currently marks stable packages vulnerable while describing an affected upstream
range starting after their versions. Do not treat that inconsistency as proof of
non-applicability; it needs source/backport verification. No VEX or scanner
exception was added. Unfixed findings are not evidence of a reachable exploit,
nor permission to claim the images are clean.

Owner: Fruitcake maintainers. Recheck all remaining findings by **2026-11-01**,
earlier if fixed packages or upstream applicability corrections become available.
Keep the lower-severity inventory visible too. Separate least-privilege database
roles remain follow-up work: the application still uses the bootstrap superuser.

## Verification And Rollback

The local rollout stopped the backend/reloader before final backups, then stopped
PostgreSQL cleanly for a cold volume snapshot. It retained logical/custom and
global-role dumps, prior configuration, image identities, table counts and backup
checksums in owner-only, Git-ignored
`storage/.container-maintenance-20261018-215334/`.

- The candidate initialized a fresh database and restored the production dump
  without errors. All 51 public table counts match the stopped-source snapshot.
- A second disposable volume restored the cold snapshot under the **original**
  image; all 51 table counts match. Test servers had no network or published port.
- The promoted server reused the existing production volume, with all 51 counts
  unchanged. Engine version, pgvector 0.8.7 and a vector-distance query passed.
- Real Docker tests cover shell MCP initialization/tool calls/refusals, network
  and mount isolation, read-only Git metadata, output limits, Stop/cancellation,
  timeout cleanup and the independent container-side watchdog.
- Normal `scripts/start.sh` restarted successfully; backend health and the shell
  MCP connection passed. The pre-existing offline FieldKit warning remains.

Old images are retained as `fruitcake/postgres-vector:rollback-20261018` and
`fruitcake/mcp-shell:rollback-20261018`. Do not prune rollback images, backups or
volumes during soak. Roll back only with the backend stopped, selecting the saved
old image and a verified **separate** restored volume. Never mount one database
volume into two running PostgreSQL servers or delete the production volume.
Do not discard post-rollout writes without a preservation plan.

## CI Changes

Both workflows now use Ubuntu 24.04 explicitly and reviewed commit-SHA pins for
Node 24 actions: checkout 7.0.1, setup-python 7.0.0 and gitleaks-action 3.0.0.
This removes reliance on Node 20 action declarations and the scheduled
`ubuntu-latest` image transition. Existing read-only workflow permissions remain.

Backend CI additionally builds both recipes on the Linux runner, checks fresh
PostgreSQL/vector initialization without network or published ports, and enables
the real Docker shell protocol/isolation tests. These are build and behavior
checks, not an automated container vulnerability scan. Scheduled Python advisory
audits and secret scanning remain enabled.
