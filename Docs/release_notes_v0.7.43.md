# Release Notes v0.7.43

## Summary

Completes the independent coding-review runtime and pairs it with native review
recovery, visual polish, shared inspector tabs and read-only checkout Explorer.

## Included Changes

- Independent reviewers over frozen captures, with explicit start, read approvals,
  Stop, durable history, evidence-validated findings and explicit dispositions.
- Reviewer profile/effort provenance, complete-source input budgets and named
  output-limit/validation failures rather than silently successful empty reviews.
- Owner-scoped Explorer metadata, lazy directory pages and complete bounded UTF-8
  file reads for the exact ready managed checkout, with no source-folder fallback.
- Persisted per-event timestamps for the native coding transcript.

## Notes

- Migration 059 is required for reviews; normal bootstrap applies it. Reviews
  remain opt-in through `CODING_REVIEW_ENABLED`. Explorer needs no new setting.
- Pair with native v0.2.4. Review and Explorer contracts are documented in
  `coding_ai_review.md` and `coding_explorer.md`.
- Existing sensitive-path/Git-ignore exclusions and authorization checks remain;
  neither Explorer nor reviews grant commands, networking or source editing.
- Wrap-up coding verification: 356 passed, 6 skipped and one sandbox-denied `ps`
  process-cleanup check; that exact check passed separately with permission.
  Isolated database-role checks: 23 passed, 1 skipped. No real-model run or live
  migration was started. Python compilation and staged whitespace checks passed.
- Native verification recorded 123 tests / 134 cases passing, excluding the
  documented pre-existing missing-state MCP fixture. Synthetic renders and
  operator appearance feedback supplement, not replace, full interaction testing.
- Code themes/syntax highlighting, browser testing and GitHub delivery are not
  included. See `code_appearance_plan.md` for the next bounded branch.
