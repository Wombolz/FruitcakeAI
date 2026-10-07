# Release Notes v0.7.40

## Summary

This release gives Fruitcake a durable structured-content layer for richer native chat. Grounded tables, charts, news, metrics, timelines, workspace files, places, and code can now travel as versioned assistant content blocks alongside readable Markdown fallback text. Evidence and source details survive persistence, and place search now uses a provider-neutral path that can route through Brave or Nominatim.

## Included Changes

- added versioned and bounded structured assistant content blocks
- added native-ready table, chart, news, stat, timeline, file, place, and code payloads
- preserved structured evidence, source links, search provenance, and tool context across reloads
- added contextual titles for tables when nearby response prose provides a useful heading
- added provider-neutral place search with Brave and Nominatim selection and bounded fallback
- added configuration for `PLACE_SEARCH_PROVIDER` and `BRAVE_PLACE_FALLBACK_TO_NOMINATIM`
- expanded agent, chat persistence, and JSON API coverage for structured content

## Compatibility And Operations

- no database migration is required
- existing clients can continue rendering the Markdown assistant content
- newer clients can render the additive structured content blocks and evidence metadata
- place search remains backward compatible; configure the provider only when overriding automatic selection
- no existing public API field was removed

## Verification

- focused structured-content, persistence, and JSON API suites passed
- Python compile checks and `git diff --check` passed before release preparation
- the known third-party pytest shutdown-thread issue required terminating already-completed test processes afterward
