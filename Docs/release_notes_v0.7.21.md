# Release Notes v0.7.21

## Summary

This release tightens approval-backed task execution by making file-mutation resumes deterministic after user approval and by cleaning up workspace-path normalization for report-writing flows.

## Included Changes

- blocked `append_file` and `write_file` task steps now persist exact tool-call intent across the approval boundary
- approved waiting steps replay the stored mutation on the same run instead of restarting the step from scratch
- waiting-approval diagnostics now preserve clearer resume context for blocked tool calls
- bare `workspace/...` prefixes are normalized consistently, preventing duplicated `workspace/workspace/...` paths in configured executor and filesystem flows

## Notes

- focused approval-resume and path-normalization tests passed before release
- a separate stale-test cleanup follow-up was noted locally and intentionally not mixed into this release
