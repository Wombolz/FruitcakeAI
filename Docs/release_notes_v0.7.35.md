# Release Notes v0.7.35

## Summary

This release extends Fruitcake's prompt-cache optimization to OpenAI, reducing repeated cloud input processing while preserving provider isolation, incognito behavior, and the existing local-model improvements.

## Included Changes

- separated stable OpenAI persona and policy instructions from volatile turn-specific context
- added stable hashed `prompt_cache_key` values based on model, stable prompt, and tool-schema shape
- added optional `OPENAI_PROMPT_CACHE_RETENTION` configuration while preserving the provider or organization default when unset
- omitted Fruitcake cache-routing and retention hints for incognito sessions
- added `llm.prompt_cache_usage` diagnostics for OpenAI cached input tokens and cache-hit percentage
- centralized native OpenAI and Ollama model-family detection without treating generic OpenAI-compatible local endpoints as OpenAI-hosted models
- preserved the existing Anthropic and generic OpenAI-compatible request layout
- documented OpenAI prompt caching, retention, and privacy considerations

## Notes

- OpenAI performs prompt caching automatically for eligible matching prefixes; Fruitcake improves prefix stability and request bucketing rather than storing a local cloud-response cache
- an empty `OPENAI_PROMPT_CACHE_RETENTION` preserves the selected provider and organization's default retention behavior
- no live OpenAI request was made during automated verification, so the release tests consumed no API credits
- focused provider, message-construction, request-wiring, and usage-diagnostic tests passed
- broader agent, routing, streaming, usage, and chat regression suites passed during branch verification, excluding three previously documented stale tests
- Python compilation and `git diff --check` passed
