# Dependency Refresh Closeout

Branch: `codex/dependency-refresh`. Reviewed: 2026-10-18.

This closes the implementation scope, not every security or test-maintenance
item. Do not describe this branch as a clean full-system vulnerability audit.
See [dependency maintenance](dependencies.md) and [credential maintenance](SECURITY_BASELINE.md#credential-maintenance)
for rollout, compatibility and private rollback details.

## Completed Scope

- Generated hash-verified runtime/development Python baselines, audit tooling,
  weekly CI audits and safe fresh-environment bootstrap checks.
- PyJWT/Argon2id migration with legacy token/bcrypt compatibility, admin-only
  registration, private password maintenance and random initial seed passwords.
- Native Python/PostgreSQL/pgvector/Tesseract maintenance, localhost-only database
  publication, and migrations using the configured database URL.
- Tested Node 24 LTS/npm refresh, Pi compatibility verification without changing
  its 1.3.0 contract, and compatible Elgato MCP SDK selection.
- Credential rotation and verification on this development machine. Credentials,
  database dumps, generated passwords and old environments are not part of Git.
- Evidence-backed news presentation, bounded same-chat RSS reuse, answer-only
  validation repair and corrected selected-model usage-provider labels.

## Current Audit Results

The active Python environment passes `pip check`. `pip-audit` reports no findings
other than the explicitly ignored NLTK advisory. The exception expires after
2026-11-18; the [upstream advisory](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp)
still lists no fix. Current chunking and retrieval were exercised with all six
affected model-artifact entry points blocked; none were called. This applicability
check does not patch NLTK or remove the need to review the exception.

GitHub currently lists 41 open Dependabot alerts against the old `main` manifest:
two critical, 20 high, 13 medium and six low. Comparing each alert's declared
affected range with this branch's installed distributions puts 40 outside their
affected ranges; the remaining alert concerns the removed `python-jose` package.
These alerts are not marked resolved here: GitHub must rescan the merged default
branch before its UI status can be confirmed.

The actual installed Pi, configured Elgato npx project and active user-global npm
trees have no returned npm advisory findings: 321 package names, 361 package
instances, no skipped symlinks. Historical caches and unused npm installations
are outside that result. Node has [announced October 21 security releases](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases);
the installed 24.22.0 release does not contain those future fixes.

## Container Findings: Remediation Follow-Up Completed

The initial scan below triggered the additional
[container and CI maintenance slice](container_maintenance.md), completed locally
on 2026-10-18. The patched effective database filesystem now reports zero critical
and ten unfixed high findings; the patched shell reports zero critical and one
unfixed high. All available critical/high fixes were applied. The layered database
scan still includes the overwritten upstream gosu binary; the linked report
explains the raw/effective distinction and retains both inventories.

Both images were promoted after logical restore, cold rollback and Docker boundary
tests. All 51 database table counts are preserved and the backend is running.
CI actions now use reviewed Node 24 pins on Ubuntu 24.04, with container build and
behavior tests added. Separate application/maintenance DB roles and review of
unfixed upstream advisories remain open. The following is the **pre-fix** scan.

Docker Scout 1.26.0 scanned the local images, not their mounted data volumes.
Counts are scanner findings, not confirmed reachable application exploits.

| Image | Critical | High | Medium | Low | Unspecified | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pinned PostgreSQL/pgvector Bookworm image | 8 | 45 | 34 | 62 | 2 | 151 |
| Current `fruitcake/mcp-shell` image | 0 | 3 | 7 | 27 | 0 | 37 |

All eight PostgreSQL-image critical findings and 35 of its high findings list
fixed versions; ten high findings have no listed fix. The critical findings
are split between Debian Perl and the Go standard library embedded in
`/usr/local/bin/gosu`. This is not evidence that PostgreSQL itself has eight
critical SQL/network vulnerabilities. Applicability requires review of the
actual component entry points and container configuration.

The original shell image's two fixable high findings were `wheel` 0.45.1 and
`jaraco.context` 5.3.0 vendored inside setuptools. Installing standalone copies
does not replace those vendored packages. Its remaining high finding concerns
Debian zlib and has no listed fix. The original image used a floating
`python:3.11-slim` base and has no pinned, reproducible package-refresh pipeline.

That follow-up slice addressed items 1-4 below; item 5 remains separate work:

1. Rebuild/rebase the runtime images with supported current OS packages and
   minimal runtime/build-tool contents; audit `gosu` independently of package
   manager metadata and update/remove affected vendored build tools correctly.
2. Re-scan by immutable image identity and document any remaining findings with
   applicability evidence, owner and review deadline rather than blanket ignores.
3. Restore-test a candidate PostgreSQL image and verify extension/data compatibility
   before replacing the live container. Never remove the database volume.
4. Exercise shell protocol, offline/network controls, resource limits, approvals
   and coding-container execution before promoting a rebuilt shell image.
5. Introduce separate application and migration/maintenance database roles. The
   current application still uses the bootstrap superuser role, despite its
   password rotation and localhost-only publication.

Raw local logs and SARIF reports are Git-ignored under
`storage/.branch-closeout-20261018-210119/`. Replacement and restore verification
reports are under `storage/.container-maintenance-20261018-215334/`.

## Operational Warnings

`scripts/doctor.sh` reports 13 passes, two degraded checks and zero failures.
The two degraded checks are expected: Ollama model verification is skipped while
the selected backend is OpenAI, and optional admin health is not checked without
explicit operator credentials. Normal application health is OK. Startup also
reports FieldKit unavailable at its configured local endpoint; the other enabled
MCP connections complete. These are distinct from vulnerability findings.

The final broad run emits 24 warnings: native SWIG type deprecations, Pydantic
class-based `Config` deprecations, Starlette's renamed HTTP 422 constant and
Pydantic's warning about a third-party `TypedDict` ReadOnly qualifier. Preserve
warning output rather than suppressing it to make the branch appear cleaner.

## Merge Caveats

The focused credential/auth/access/integration/bootstrap suite passed 195 tests;
installed-Pi verification passed 298 with six skips. The final broad run reports
**1,379 passed, 55 skipped, 10 failed and 24 warnings** in 287 seconds. Its failures
match the previously reproduced baseline set below. The previously intermittent
checkout-serialization test passes in this run; that does not prove it cannot
recur. Focused passes do not establish a green broad suite.

```text
tests/test_agent_definitions.py::test_agent_preset_exposes_expected_metadata
tests/test_configured_executor.py::test_configured_executor_validate_finalize_accepts_grounded_briefing
tests/test_configured_executor.py::test_configured_executor_validate_finalize_allows_heading_before_bullets
tests/test_configured_executor.py::test_configured_executor_strips_memory_candidate_section_from_report_output
tests/test_configured_executor.py::test_prepare_run_context_applies_recent_repeat_pruning_and_light_title_diversity
tests/test_llm_usage.py::test_run_agent_stops_repeated_failed_search_loop_gracefully
tests/test_llm_usage.py::test_run_agent_returns_tool_aware_message_when_hitting_max_turns
tests/test_task_profiles.py::test_topic_watcher_prepare_run_context_loads_same_topic_memory_history
tests/test_task_steps.py::test_recurring_task_auto_plans_once_when_missing_plan
tests/test_task_steps.py::test_runner_claims_task_once_under_duplicate_dispatch
```

Keep the PR unmerged until its checks and the remaining-warning inventory have
been reviewed. The unrelated local `web_research_v2 copy.py` file is deliberately
excluded. Do not remove rollback artifacts during branch cleanup; credential
backups require owner-only access and must not be ingested as library documents.
