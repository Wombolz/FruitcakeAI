from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any


RESEARCH_KEYWORDS = {
    "research",
    "headline",
    "headlines",
    "news",
    "sources",
    "source",
    "citation",
    "citations",
    "latest",
    "web",
    "search",
    "compare",
}

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)", re.IGNORECASE)
PLACEHOLDER_HOST_PARTS = ("example.", "localhost", "127.0.0.1", "::1")
CALENDAR_MUTATION_KEYWORDS = {
    "calendar",
    "event",
    "meeting",
    "appointment",
    "schedule",
}
CALENDAR_WRITE_ACTIONS = {
    "create",
    "add",
    "put",
    "schedule",
    "set up",
    "move",
    "reschedule",
    "update",
    "change",
}
CALENDAR_SUCCESS_PATTERNS = (
    "i've created",
    "i created",
    "i've added",
    "i added",
    "successfully added",
    "successfully created",
    "has been added to your calendar",
    "has been created on your calendar",
    "i've moved",
    "i moved",
    "rescheduled",
    "updated your calendar",
)


@dataclass
class ChatValidationResult:
    is_research_style: bool
    is_empty_result: bool
    valid_urls: list[str]
    invalid_urls: list[str]
    mutation_unconfirmed: bool
    should_retry: bool
    retry_reason: str | None
    cleaned_content: str


def validate_chat_response(
    user_prompt: str,
    response: str,
    executed_tools: list[dict[str, Any]] | None = None,
) -> ChatValidationResult:
    prompt = (user_prompt or "").strip().lower()
    text = (response or "").strip()
    is_research_style = any(word in prompt for word in RESEARCH_KEYWORDS)
    mutation_unconfirmed = _is_calendar_mutation_prompt(prompt) and _claims_calendar_mutation_success(text) and not _calendar_mutation_confirmed(executed_tools or [])

    urls = _extract_urls(text)
    valid_urls = [u for u in urls if _is_valid_public_url(u)]
    invalid_urls = [u for u in urls if u not in valid_urls]
    cleaned = _strip_invalid_links(text, invalid_urls)
    empty_result = len(cleaned.strip()) < 40

    should_retry = False
    retry_reason: str | None = None

    if is_research_style:
        if mutation_unconfirmed:
            should_retry = True
            retry_reason = "calendar_mutation_unconfirmed"
        elif invalid_urls:
            should_retry = True
            retry_reason = "invalid_links"
        elif not valid_urls:
            should_retry = True
            retry_reason = "missing_links"
        elif empty_result:
            should_retry = True
            retry_reason = "empty_result"
    else:
        if mutation_unconfirmed:
            should_retry = True
            retry_reason = "calendar_mutation_unconfirmed"
        elif empty_result:
            should_retry = True
            retry_reason = "empty_result"
        elif invalid_urls:
            should_retry = True
            retry_reason = "invalid_links"

    return ChatValidationResult(
        is_research_style=is_research_style,
        is_empty_result=empty_result,
        valid_urls=valid_urls,
        invalid_urls=invalid_urls,
        mutation_unconfirmed=mutation_unconfirmed,
        should_retry=should_retry,
        retry_reason=retry_reason,
        cleaned_content=cleaned,
    )


def build_chat_retry_instruction(reason: str | None) -> str:
    if reason == "empty_result":
        return (
            "Your previous answer was too brief/empty. Provide a complete answer now. "
            "If you reference sources, include direct HTTP/HTTPS URLs."
        )
    if reason == "missing_links":
        return (
            "This request expects grounded sources. Re-answer with direct HTTP/HTTPS source URLs. "
            "Do not use placeholder links or generic site homepages."
        )
    if reason == "invalid_links":
        return (
            "Your previous answer included invalid/placeholder links. Re-answer using only valid "
            "public HTTP/HTTPS links. Omit any item you cannot ground."
        )
    if reason == "calendar_mutation_unconfirmed":
        return (
            "Do not claim that a calendar event was created, moved, or updated unless a calendar "
            "tool explicitly confirmed success. If the mutation was not confirmed, say that clearly."
        )
    return "Re-answer with a complete grounded response."


def _extract_urls(text: str) -> list[str]:
    urls = set(URL_RE.findall(text))
    for _, link in MD_LINK_RE.findall(text):
        urls.add(link)
    return sorted(urls)


def _is_valid_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    if not host:
        return False
    if any(part in host for part in PLACEHOLDER_HOST_PARTS):
        return False
    return True


def _strip_invalid_links(text: str, invalid_urls: list[str]) -> str:
    out = text
    for url in invalid_urls:
        escaped = re.escape(url)
        out = re.sub(rf"\[([^\]]+)\]\({escaped}\)", r"\1", out)
        out = out.replace(url, "")
    return out


def _is_calendar_mutation_prompt(prompt: str) -> bool:
    return any(word in prompt for word in CALENDAR_MUTATION_KEYWORDS) and any(
        action in prompt for action in CALENDAR_WRITE_ACTIONS
    )


def _claims_calendar_mutation_success(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in CALENDAR_SUCCESS_PATTERNS)


def _calendar_mutation_confirmed(executed_tools: list[dict[str, Any]]) -> bool:
    for record in executed_tools:
        tool = str(record.get("tool", "")).strip()
        summary = str(record.get("result_summary", "")).strip().lower()
        if tool == "create_event" and summary.startswith("event created:"):
            return True
        if tool in {"update_event", "move_event"} and (
            summary.startswith("event updated:") or summary.startswith("event moved:")
        ):
            return True
    return False
