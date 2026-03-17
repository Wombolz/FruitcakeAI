from __future__ import annotations

import re


LIBRARY_TERMS = {
    "library",
    "document",
    "documents",
    "doc",
    "docs",
    "file",
    "files",
    "uploaded",
}

LIST_TERMS = {
    "list",
    "show",
    "what",
    "which",
    "find",
}

DETAIL_TERMS = {
    "detail",
    "details",
    "metadata",
    "status",
    "error",
}

EXCERPT_TERMS = {
    "excerpt",
    "excerpts",
    "quote",
    "quotes",
    "section",
    "sections",
    "passage",
    "passages",
}


def is_library_lookup_intent(content: str) -> bool:
    text = (content or "").strip().lower()
    if not text:
        return False
    tokens = set(re.findall(r"[a-z0-9_']+", text))
    has_library_target = any(term in tokens for term in LIBRARY_TERMS)
    has_lookup_verb = any(term in tokens for term in LIST_TERMS)
    return has_library_target and has_lookup_verb


def is_library_detail_or_excerpt_intent(content: str) -> bool:
    text = (content or "").strip().lower()
    if not text:
        return False
    tokens = set(re.findall(r"[a-z0-9_']+", text))
    has_library_target = any(term in tokens for term in LIBRARY_TERMS)
    has_detail_hint = any(term in tokens for term in DETAIL_TERMS | EXCERPT_TERMS)
    return has_library_target and has_detail_hint
