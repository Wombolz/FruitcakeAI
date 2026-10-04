# Release Notes v0.7.37

## Summary

This release improves chat reliability at several provider boundaries. Web search can now use Brave with a temporary DuckDuckGo fallback, malformed tool schemas are stopped before they reach strict model parsers, structured catalog results survive compaction intact, and research-link validation is applied only when sources are actually required.

## Included Changes

- added Brave Search API support through `BRAVE_SEARCH_API_KEY`
- retained DuckDuckGo as an optional fallback controlled by `BRAVE_SEARCH_FALLBACK_TO_DDG`
- made web-search caches provider-aware and added bounded provider diagnostics
- normalized object-shaped tool parameters so strict local model parsers receive valid JSON Schema
- quarantined malformed MCP tool schemas and exposed them through existing registry diagnostics
- added catalog-aware tool-result compaction that retains every item while omitting verbose detail fields
- limited mandatory source-link validation to research prompts and turns grounded by web or feed tools
- configured application tracebacks to omit frame locals that can contain private chat history, tool payloads, or credentials

## Compatibility And Operations

- existing `web_search` callers require no changes
- installations without a Brave key continue using DuckDuckGo
- invalid MCP tools are excluded individually rather than breaking the complete tool catalogue
- no database migration or public API change is required
- add `BRAVE_SEARCH_API_KEY` to the runtime environment to enable Brave Search

## Verification

- focused agent, chat-validation, MCP, logging, and web-search suites: 173 passed
- `git diff --check` passed before release preparation

