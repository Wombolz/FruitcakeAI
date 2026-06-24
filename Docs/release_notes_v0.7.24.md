# Release Notes v0.7.24

## Summary

This release rounds out the backend support for the task redesign by adding editor-facing task presentation metadata and a duplicate-draft endpoint that lets the client reopen an existing automation as a safe prefilled copy.

## Included Changes

- tasks now accept and return a normalized `presentation` payload, including validated `accent_hex` styling metadata
- task create, patch, list, and detail responses now preserve that presentation metadata end to end
- added `POST /tasks/{id}/duplicate-draft` so the client can request an editor-ready duplicate payload instead of rebuilding task fields by hand
- duplicate drafts include the source task’s schedule, delivery settings, timezone, recipe family/params, model override, and presentation metadata
- added the `presentation_json` task migration to persist styling metadata cleanly in the task record

## Notes

- focused backend task API coverage passed for presentation validation, presentation persistence, and duplicate-draft payload generation
- this release is backend-only; the matching client-side task redesign work can build on these fields without needing another API contract pass
