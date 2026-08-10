# Release Notes v0.7.31

## Summary

This release establishes Fruitcake's reusable visual-chat foundation: authenticated workspace image attachments, optional local vision inspection, durable inline image artifacts, and first-class progress metadata for long-running FruitcakeImageLab generation.

## Included Changes

- added user-scoped workspace image upload and authenticated image-serving endpoints with size and path-boundary enforcement
- added the optional `describe_image` tool with configurable vision model, byte limit, and resize limit
- added structured image evidence and artifact metadata that survives chat reloads and session switches
- added generated-image response guidance and normalization so image references remain associated with the relevant assistant response
- added an `image_rendering` websocket state with bounded generation details for prompt, model, workflow, steps, seed, and output dimensions
- enabled the first-party FruitcakeImageLab MCP companion with a 600-second timeout for synchronous local generation and checkpoint loading
- documented the current local chat, vision, and image workflow model inventory in `Docs/LOCAL_MODEL_CARDS.md`

## Notes

- the FruitcakeImageLab server remains an optional local companion and must be running separately at its configured MCP endpoint
- image description remains disabled until `IMAGE_VISION_MODEL` is configured with a compatible vision-capable model
- focused verification passed before release: `.venv/bin/pytest tests/test_auth.py tests/test_chat_streaming.py -q` (`86 passed`)
- Python compile checks and `git diff --check` were also run against the touched backend modules and release diff
