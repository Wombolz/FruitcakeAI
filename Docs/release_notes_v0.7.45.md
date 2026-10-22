# Release Notes v0.7.45

## Summary

Adds per-user ImageLab accounts, frozen skill bundles and read-only private image
previews, paired with native client v0.2.6's Skills manager and compact image cards.

## Included Changes

- Deployment-owned ImageLab bearer tokens keyed by Fruitcake user ID. Unmapped
  users retain tokenless Shared access; configured private accounts never fall
  back to Shared after authentication or connection failure.
- Validated, immutable Markdown/text bundles with complete bounded main
  instructions, selected-turn reference reads and admin import/manage APIs.
- Migration `062_skill_bundles`; existing plain-text skills remain compatible.
- Durable asset-only image evidence with registry-attested provider provenance,
  owner-scoped image reads, bounded validated pixels and no automatic exports.
- Native pairing: folder import/preview/install/disable/remove, aspect-ratio image
  cards and click-to-open image inspection with fit/zoom.

## Notes

- Normal bootstrap applies migration 062 using the existing migration-role flow.
  The operator's database already has this revision; do not stamp or downgrade it.
- Account mappings use optional `IMAGELAB_USER_TOKENS_JSON` in the private `.env`.
  No token, local configuration override or unrelated copied source is committed.
- Live ImageLab verification read an existing generated PNG through the configured
  account and confirmed Shared denial. No image was generated or exported. Preview
  support requires the compatible ImageLab resource API and `profile.library.read`.
- Old messages whose image evidence was discarded are not automatically backfilled.
  Persona likeness handoff and general workspace editing remain separate work.
- Local closeout: 372 focused backend tests passed, one opt-in PostgreSQL check
  skipped in that run. The separate disposable PostgreSQL/role suite passed all
  24 checks, including migration 062. Persona foundations passed 41 checks;
  task/skill checks passed 77 with two previously confirmed baseline failures
  excluded. These suites overlap; counts are not additive. The isolated macOS
  build and focused Skills/image/inspector tests passed. Dependency audit found
  no known vulnerabilities beyond the existing time-bounded NLTK exception.
  Hosted checks are recorded in the paired PRs.
- Next sprint is [local inference resource coordination](local_inference_resource_coordination_plan.md).
  This release does not change model retention, unload models during turns, alter
  image quality, or deploy a rented GPU backend.
