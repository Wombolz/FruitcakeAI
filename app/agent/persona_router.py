"""
Intent-based task persona routing.

Deterministic, config-driven matcher:
- Reads optional intent_keywords / intent_phrases from personas.yaml.
- Returns the best matching persona for a task title+instruction.
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

from app.agent.persona_loader import list_personas, persona_exists

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_text(text: str) -> str:
    return " ".join(_TOKEN_RE.findall((text or "").lower()))


def infer_persona_for_task(
    title: str,
    instruction: str,
    *,
    fallback: str = "family_assistant",
) -> Tuple[str, float, str]:
    """
    Infer persona from task intent.

    Returns: (persona_name, confidence, reason)
    """
    personas: Dict[str, Dict] = list_personas()
    if not personas:
        return fallback, 0.0, "fallback_no_personas_configured"

    haystack = _normalize_text(f"{title} {instruction}")
    if not haystack:
        return fallback, 0.0, "fallback_empty_task_text"

    best_name = fallback
    best_score = 0.0
    best_reason = "fallback_no_match"

    for persona_name, cfg in personas.items():
        keywords = [str(x).strip().lower() for x in (cfg.get("intent_keywords") or []) if str(x).strip()]
        phrases = [str(x).strip().lower() for x in (cfg.get("intent_phrases") or []) if str(x).strip()]

        if not keywords and not phrases:
            continue

        score = 0.0
        hit_keywords = []
        hit_phrases = []

        for kw in keywords:
            kw_norm = _normalize_text(kw)
            if not kw_norm:
                continue
            if f" {kw_norm} " in f" {haystack} ":
                score += 1.0
                hit_keywords.append(kw)

        for phrase in phrases:
            phrase_norm = _normalize_text(phrase)
            if not phrase_norm:
                continue
            if phrase_norm in haystack:
                score += 2.0
                hit_phrases.append(phrase)

        if score > best_score:
            best_name = persona_name
            best_score = score
            reason_bits = []
            if hit_keywords:
                reason_bits.append(f"keywords={hit_keywords[:3]}")
            if hit_phrases:
                reason_bits.append(f"phrases={hit_phrases[:2]}")
            best_reason = "matched_" + ",".join(reason_bits) if reason_bits else "matched"

    if best_score <= 0:
        return fallback, 0.0, "fallback_no_match"

    if not persona_exists(best_name):
        return fallback, 0.0, "fallback_matched_persona_missing"

    # Simple bounded confidence from score; deterministic and interpretable.
    confidence = min(0.99, 0.2 + (best_score * 0.15))
    return best_name, confidence, best_reason

