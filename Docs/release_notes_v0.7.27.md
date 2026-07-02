# Release Notes v0.7.27

## Summary

This release removes a persistent local runtime warning by setting the Hugging Face tokenizer parallelism environment flag early enough to apply before tokenization or later subprocess launches.

## Included Changes

- exported `TOKENIZERS_PARALLELISM=false` during config import so the real process environment is set before any Hugging Face tokenizer work occurs
- kept `setdefault` semantics so explicit deployment overrides still win
- clarified `.env.example` so operators understand the app now manages this flag by default

## Notes

- focused verification for this slice was a compile check of `app/config.py`
- this is a small runtime-hygiene release with no API or schema changes
