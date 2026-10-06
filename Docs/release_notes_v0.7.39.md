# Release Notes v0.7.39

## Summary

This release makes Fruitcake's context and web-research behavior fit the selected model instead of relying on early conservative global limits. Model profiles now carry explicit context budgets, chat and task synthesis retain evidence according to its value and available capacity, and operators can inspect how each request used its context window. Web research now has a provider-neutral foundation and can use Brave LLM Context for faster, citation-rich multi-source research while retaining ordinary Brave or DuckDuckGo search paths.

## Included Changes

- added model-profile fields for context-window size plus output, reasoning, and safety reserves
- added request-level context planning across chat, streaming, overflow recovery, and task final synthesis
- added evidence classes with larger bounded budgets for source pages, provider context, document summaries, and structured datasets
- preserved recent web evidence and source/citation boundaries during compaction
- added context-budget lifecycle events and chat-run inspection metrics without storing prompt content
- added a provider-neutral web-research service with configurable Brave and DuckDuckGo selection and fallback
- added the Brave LLM Context tool with structured citations, source metadata, bounded depth, and visible query details
- added deterministic first-turn routing for prompts that benefit from consolidated web context
- increased configurable fetched-page capacity from the previous fixed limit
- corrected false-positive compaction accounting and prevented compacted wrappers from increasing retained content
- added one bounded retry for empty model answers and a visible fallback if the retry is also empty
- added migration `049_model_context_budgets`

## Compatibility And Operations

- apply database migration `049_model_context_budgets` before starting the updated backend
- existing model profiles receive conservative context defaults and can be adjusted through the existing administrator model-profile API
- set `WEB_SEARCH_PROVIDER` to `auto`, `brave`, or `duckduckgo`; `auto` prefers Brave when its key is configured
- Brave LLM Context is only advertised when `BRAVE_CONTEXT_ENABLED=true` and `BRAVE_SEARCH_API_KEY` is configured
- existing `web_search` and `fetch_page` tool contracts remain available
- no existing public API field was removed

## Verification

- focused compaction and empty-answer guardrails: 5 passed
- broader agent, chat, runtime-event, MCP, context-budget, and web-research suites: 307 passed
- `git diff --check` and Python compile checks passed before release preparation
- the known third-party pytest shutdown-thread issue required terminating the already-completed test process afterward
