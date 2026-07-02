# Release Notes v0.7.28

## Summary

This release closes the remaining linked-source indexing v1 gaps by tightening trust guards and making manual rescans actually incremental for unchanged files.

## Included Changes

- single-file links now reject sensitive filename patterns such as `.env`
- once `LINKED_SOURCE_ALLOWED_ROOTS` is configured, linked files now honor the same root boundary as linked folders
- unchanged files in linked-folder rescans are now skipped from stored stat metadata alone instead of being reopened on every manual rescan
- linked documents that go missing and later reappear with identical size/mtime now re-ingest correctly
- source modified-time comparison is now timezone-defensive for SQLite and other naive-datetime environments
- linked-source coverage now includes guard regressions, cached retrieval retention when backing files disappear, source-path citation exposure, and stat-only rescan proof

## Notes

- focused verification passed before release:
  - `tests/test_linked_sources.py tests/test_library_api.py tests/test_rag.py`
  - `tests/test_document_processor.py tests/test_host_root_access.py tests/test_filesystem_mcp.py`
- total focused result count for this release slice: `75 passed`
- accepted edge left intentionally unchanged in this slice: `secrets.env`-style names remain indexable unless product policy later broadens the sensitive filename blocklist
