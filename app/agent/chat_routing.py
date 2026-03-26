from __future__ import annotations

import re
from dataclasses import dataclass


HIGH_STAKES_KEYWORDS = {
    "medical",
    "legal",
    "contract",
    "lawsuit",
    "diagnosis",
    "symptom",
    "dosage",
    "tax",
    "investment",
    "portfolio",
    "mortgage",
    "compliance",
}

TOOL_HEAVY_KEYWORDS = {
    "search",
    "research",
    "compare",
    "sources",
    "citations",
    "summarize",
    "latest",
    "headlines",
    "find",
    "analyze",
}

LOCATION_LOOKUP_KEYWORDS = {
    "address",
    "addresses",
    "location",
    "locations",
    "hours",
    "phone",
    "directions",
    "nearby",
}

MULTI_STEP_MARKERS = {
    "step by step",
    "plan",
    "for each",
    "pros and cons",
    "first",
    "then",
    "finally",
}


@dataclass
class ChatComplexityDecision:
    score: int
    reasons: list[str]
    is_complex: bool
    mode: str  # "chat" or "task"


def classify_chat_complexity(
    content: str,
    *,
    threshold: int = 3,
    routing_enabled: bool = True,
) -> ChatComplexityDecision:
    text = (content or "").strip()
    lowered = text.lower()
    score = 0
    reasons: list[str] = []

    if len(text) >= 500:
        score += 1
        reasons.append("long_prompt")

    if text.count("?") >= 2:
        score += 1
        reasons.append("multi_question")

    parts = re.split(r"\b(?:and|then|also)\b|[;\n]", lowered)
    substantial_parts = [p for p in parts if len(p.strip()) >= 25]
    if len(substantial_parts) >= 3:
        score += 1
        reasons.append("multi_part_request")

    if any(k in lowered for k in HIGH_STAKES_KEYWORDS):
        score += 1
        reasons.append("high_stakes")

    if sum(1 for k in TOOL_HEAVY_KEYWORDS if k in lowered) >= 2:
        score += 1
        reasons.append("tool_heavy")

    has_location_lookup = any(k in lowered for k in LOCATION_LOOKUP_KEYWORDS)
    has_place_constraint = bool(
        re.search(r"\b[a-z .'-]+,\s*[a-z]{2}\b", lowered)
        or re.search(r"\b\d{5}(?:-\d{4})?\b", lowered)
        or "near me" in lowered
    )
    if has_location_lookup and has_place_constraint:
        score += 2
        reasons.append("location_lookup")

    if any(m in lowered for m in MULTI_STEP_MARKERS):
        score += 1
        reasons.append("multi_step_marker")

    is_complex = routing_enabled and score >= max(1, int(threshold))
    return ChatComplexityDecision(
        score=score,
        reasons=reasons,
        is_complex=is_complex,
        mode="task" if is_complex else "chat",
    )
