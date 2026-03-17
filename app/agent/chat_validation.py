from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


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


@dataclass
class ChatValidationResult:
    is_research_style: bool
    is_empty_result: bool
    valid_urls: list[str]
    invalid_urls: list[str]
    should_retry: bool
    retry_reason: str | None
    cleaned_content: str


def validate_chat_response(user_prompt: str, response: str) -> ChatValidationResult:
    prompt = (user_prompt or "").strip().lower()
    text = (response or "").strip()
    is_research_style = any(word in prompt for word in RESEARCH_KEYWORDS)

    urls = _extract_urls(text)
    valid_urls = [u for u in urls if _is_valid_public_url(u)]
    invalid_urls = [u for u in urls if u not in valid_urls]
    cleaned = _strip_invalid_links(text, invalid_urls)
    empty_result = len(cleaned.strip()) < 40

    should_retry = False
    retry_reason: str | None = None

    if is_research_style:
        if invalid_urls:
            should_retry = True
            retry_reason = "invalid_links"
        elif not valid_urls:
            should_retry = True
            retry_reason = "missing_links"
        elif empty_result:
            should_retry = True
            retry_reason = "empty_result"
    else:
        if empty_result:
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
