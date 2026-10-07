"""Bounded, versioned rich-content metadata for assistant chat messages."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

CONTENT_BLOCK_SCHEMA_VERSION = 1
CONTENT_BLOCK_LIMIT = 8

_NEWS_SOURCE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_NEWS_SECTION_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")
_NEWS_ITEM_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*(?:\s+(.+?))?\s*$")
_BARE_SOURCE_URL_RE = re.compile(r"https?://[^\s)>]+")
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,4}\s+(.+?)\s*$")
_STAT_ITEM_RE = re.compile(r"^\s*[-*]?\s*\*\*([^*:\n]{1,80}):\*\*\s*(.+?)\s*$")
_TIMELINE_HEADING_HINTS = ("timeline", "chronology", "history", "sequence", "events", "schedule", "milestones", "incident")
_TIMELINE_ITEM_RE = re.compile(
    r"^\s*[-*]\s+\*\*([^*]{1,100}?)(?::)?\*\*\s*(?:[—–-]\s*)?(.+?)\s*$"
)
_NEWS_TOOL_NAMES = {
    "get_feed_items",
    "list_recent_feed_items",
    "search_feeds",
    "search_my_feeds",
    "search_my_feeds_timeline",
}
_FILE_ARTIFACT_TOOL_NAMES = {"write_file", "append_file"}
_SOURCE_RESULT_TOOL_NAMES = _NEWS_TOOL_NAMES | {"web_search", "web_context"}
_RESULT_ITEM_RE = re.compile(r"^\s*(?:\[\d+\]|(?:Source\s+)?\d+[.):])\s+(.+?)\s*$", re.I)
_RESULT_FIELD_RE = re.compile(r"^\s*(Feed|Source|Published|Published/updated|Summary|URL):\s*(.+?)\s*$", re.I)


def _source_fingerprint(source_markdown: str) -> str:
    return hashlib.sha256(source_markdown.encode("utf-8")).hexdigest()[:16]


def _safe_source_url(value: Any) -> str:
    url = str(value or "").strip()[:500]
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _url_match_key(value: Any) -> str:
    url = _safe_source_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"


def _clean_source_item(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        url = _safe_source_url(item)
        return {"url": url} if url else None
    if not isinstance(item, dict):
        return None
    url = _safe_source_url(item.get("url"))
    document = " ".join(str(item.get("document") or "").split()).strip()[:300]
    path = " ".join(str(item.get("path") or "").split()).strip()[:500]
    if not (url or document or path):
        return None
    cleaned: dict[str, str] = {}
    if url:
        cleaned["url"] = url
    if document:
        cleaned["document"] = document
    if path:
        cleaned["path"] = path
    for key, limit in (("title", 200), ("label", 120), ("source", 120), ("published_at", 80)):
        value = " ".join(str(item.get(key) or "").split()).strip()
        if value:
            cleaned[key] = value[:limit]
    return cleaned


def _sources_from_result_text(result_summary: str) -> list[dict[str, str]]:
    """Extract bounded title/URL/source tuples from Fruitcake's text tool formats."""
    sources: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        cleaned = _clean_source_item(current)
        if cleaned:
            sources.append(cleaned)
        current = {}

    for raw_line in str(result_summary or "").splitlines():
        item_match = _RESULT_ITEM_RE.match(raw_line)
        if item_match:
            flush()
            current["title"] = item_match.group(1).strip()[:200]
            continue
        field_match = _RESULT_FIELD_RE.match(raw_line)
        if not field_match:
            continue
        field, value = field_match.groups()
        field = field.casefold()
        if field == "url":
            current["url"] = value
        elif field in {"feed", "source"}:
            current["source"] = value
        elif field in {"published", "published/updated"}:
            current["published_at"] = value
    flush()
    return sources[:12]


def build_assistant_citations(executed_tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalize structured and legacy text sources into one compact citation contract."""
    citations: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        candidates: list[Any] = []
        structured = record.get("structured_content")
        if isinstance(structured, dict):
            raw = structured.get("citations") or structured.get("sources") or []
            if isinstance(raw, list):
                candidates.extend(raw)
        raw_citations = record.get("citations")
        if isinstance(raw_citations, list):
            candidates.extend(raw_citations)
        if tool_name in _SOURCE_RESULT_TOOL_NAMES:
            candidates.extend(_sources_from_result_text(str(record.get("result_summary") or "")))
        for candidate in candidates:
            cleaned = _clean_source_item(candidate)
            key = tuple(sorted(cleaned.items())) if cleaned else ()
            if not cleaned or key in seen:
                continue
            seen.add(key)
            citations.append(cleaned)
            if len(citations) >= 12:
                return citations
    return citations


def build_assistant_source_details(executed_tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Expose bounded query/provider/document details without leaking tool payloads."""
    details: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    query_tools = {
        "web_search", "web_context", "search_library", "search_my_feeds",
        "search_my_feeds_timeline", "search_feeds",
    }
    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        arguments = record.get("arguments") or {}
        structured = record.get("structured_content") or {}
        if not tool_name or not isinstance(arguments, dict):
            continue
        candidates: list[tuple[str, str, str]] = []
        if tool_name in query_tools:
            query = " ".join(str(arguments.get("query") or "").split()).strip()
            if query:
                candidates.append(("query", "Query", query[:500]))
        if tool_name in {"web_search", "web_context"} and isinstance(structured, dict):
            provider = " ".join(str(structured.get("provider") or "").split()).strip()
            if provider:
                candidates.append(("provider", "Provider", provider[:120]))
        if tool_name == "fetch_page":
            url = _safe_source_url(arguments.get("url"))
            if url:
                candidates.append(("url", "Page", url))
        if tool_name == "summarize_document":
            document = " ".join(str(arguments.get("document_name") or "").split()).strip()
            if document:
                candidates.append(("document", "Document", document[:500]))
        if tool_name == "describe_image":
            for kind, label, key in (("image", "Image", "path"), ("question", "Question", "question")):
                value = " ".join(str(arguments.get(key) or "").split()).strip()
                if value:
                    candidates.append((kind, label, value[:500]))
        for kind, label, value in candidates:
            key = (tool_name, kind, value)
            if key in seen:
                continue
            seen.add(key)
            detail = {"tool_name": tool_name, "detail_kind": kind, "label": label, "value": value}
            if tool_name == "fetch_page" and kind == "url":
                parsed = urlparse(value)
                detail["source_kind"] = (
                    "pdf" if parsed.path.casefold().endswith(".pdf")
                    else "wiki" if (parsed.hostname or "").casefold().endswith("wikipedia.org")
                    else "web"
                )
                source_title = (
                    str(structured.get("title") or "").strip()
                    if isinstance(structured, dict)
                    else ""
                )
                if not source_title:
                    first_line = str(record.get("result_summary") or "").split("\n", 1)[0]
                    if first_line.startswith("Title: "):
                        source_title = first_line.removeprefix("Title: ").strip()
                if source_title:
                    detail["source_title"] = source_title[:200]
            details.append(detail)
            if len(details) >= 8:
                return details
    return details


def _split_markdown_table_row(line: str) -> list[str]:
    """Split one simple GFM table row while preserving escaped pipes."""
    text = str(line or "").strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _table_separator(line: str, expected_columns: int) -> tuple[bool, list[str]]:
    cells = _split_markdown_table_row(line)
    if len(cells) != expected_columns:
        return False, []
    compact = [cell.replace(" ", "") for cell in cells]
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in compact):
        return False, []
    alignments = [
        "center" if cell.startswith(":") and cell.endswith(":") else
        "right" if cell.endswith(":") else
        "left"
        for cell in compact
    ]
    return True, alignments


def _markdown_cell_number(value: str) -> float | None:
    cleaned = re.sub(r"[*_`]", "", str(value or "")).strip()
    cleaned = cleaned.replace(",", "").replace("$", "").replace("%", "")
    cleaned = cleaned.removeprefix("+")
    if not cleaned or not re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)", cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _chart_hint(columns: list[str], rows: list[list[str]]) -> dict[str, Any] | None:
    if len(columns) < 2 or len(rows) < 2:
        return None
    numeric_columns = [
        index
        for index in range(1, len(columns))
        if all(_markdown_cell_number(row[index]) is not None for row in rows if row[index].strip())
        and sum(1 for row in rows if row[index].strip()) >= 2
    ][:4]
    if not numeric_columns:
        return None
    labels = [str(row[0] or "").strip() for row in rows]
    header = str(columns[0] or "").casefold()
    date_like = any(marker in header for marker in ("date", "time", "day", "month", "year", "period")) or all(
        re.search(
            r"\d{4}[-/]\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
            label,
            re.I,
        )
        for label in labels
    )
    return {
        "kind": "line" if date_like else "bar",
        "category_column": 0,
        "value_columns": numeric_columns,
    }


def _heading_before(lines: list[str], table_index: int) -> tuple[int, str | None]:
    cursor = table_index - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    if cursor < 0:
        return table_index, None
    match = _MARKDOWN_HEADING_RE.match(lines[cursor])
    if not match:
        return table_index, None
    return cursor, match.group(1).strip()[:160]


def _build_news_block(content: str) -> dict[str, Any] | None:
    lines = str(content or "").splitlines()
    sections: list[dict[str, Any]] = []
    first_line: int | None = None
    last_line = 0
    index = 0
    item_count = 0

    while index < len(lines) and len(sections) < 8 and item_count < 30:
        heading = _NEWS_SECTION_RE.match(lines[index])
        if not heading:
            index += 1
            continue
        section_start = index
        section_title = heading.group(1).strip()[:160]
        index += 1
        items: list[dict[str, Any]] = []

        while index < len(lines) and not _NEWS_SECTION_RE.match(lines[index]) and item_count < 30:
            headline = _NEWS_ITEM_RE.match(lines[index])
            if not headline:
                index += 1
                continue
            item_title = headline.group(1).strip()[:240]
            source_label_hint = ""
            trailing = str(headline.group(2) or "").strip().strip("* _()")
            if trailing:
                source_label_hint = trailing.split(",", 1)[0].strip()[:100]
            index += 1
            summary_lines: list[str] = []
            sources: list[dict[str, str]] = []

            while index < len(lines):
                if _NEWS_SECTION_RE.match(lines[index]) or _NEWS_ITEM_RE.match(lines[index]):
                    break
                raw_line = lines[index].strip()
                links = _NEWS_SOURCE_LINK_RE.findall(raw_line)
                if not links:
                    links = [
                        (source_label_hint or (urlparse(url).hostname or "Source"), url)
                        for url in _BARE_SOURCE_URL_RE.findall(raw_line)
                    ]
                if links:
                    for label, url in links[:4]:
                        sources.append({"label": label.strip()[:100], "url": url.strip()[:2_000]})
                    index += 1
                    break
                elif raw_line:
                    summary_lines.append(raw_line)
                index += 1

            summary = " ".join(summary_lines).strip()[:1_200]
            if item_title and (summary or sources):
                items.append({"title": item_title, "summary": summary, "sources": sources})
                item_count += 1
                last_line = index

        if items:
            if first_line is None:
                first_line = section_start
            sections.append({"title": section_title, "items": items})

    if first_line is None or item_count < 2:
        return None
    source_markdown = "\n".join(lines[first_line:last_line]).strip()
    if not source_markdown or len(source_markdown) > 24_000:
        return None
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": "news_digest_1",
        "type": "news_digest",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": "News Briefing",
        "sections": sections,
    }


def _rss_items_from_result_text(result_summary: str) -> list[dict[str, Any]]:
    """Recover the structured fields emitted by Fruitcake's RSS tools."""
    items: list[dict[str, Any]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        url = _safe_source_url(current.get("url"))
        title = " ".join(current.get("title", "").split()).strip()[:240]
        if url and title:
            source = " ".join(current.get("source", "").split()).strip()[:100]
            summary = " ".join(current.get("summary", "").split()).strip()[:1_200]
            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "sources": [{"label": source or (urlparse(url).hostname or "Source"), "url": url}],
                }
            )
        current = {}

    for raw_line in str(result_summary or "").splitlines():
        item_match = _RESULT_ITEM_RE.match(raw_line)
        if item_match:
            flush()
            current["title"] = item_match.group(1).strip()
            continue
        field_match = _RESULT_FIELD_RE.match(raw_line)
        if field_match:
            field, value = field_match.groups()
            field = field.casefold()
            if field == "url":
                current["url"] = value
            elif field in {"feed", "source"}:
                current["source"] = value
            elif field == "summary":
                current["summary"] = value
            continue
        compact = raw_line.strip()
        if current and compact.lower().startswith("summary:"):
            current["summary"] = compact.split(":", 1)[1].strip()
        elif current and compact and not compact.endswith(":"):
            current.setdefault("summary", compact)
    flush()
    return items[:30]


def _selected_news_source_markdown(content: str, selected_urls: set[str]) -> str:
    lines = str(content or "").splitlines()
    matching_lines = [
        index
        for index, line in enumerate(lines)
        if any(url in line for url in selected_urls)
    ]
    if not matching_lines:
        return ""
    start = matching_lines[0]
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = matching_lines[-1] + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].strip() == "---"):
        end += 1
    return "\n".join(lines[start:end]).strip()


def _build_news_block_from_rss_evidence(
    content: str,
    executed_tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build a stable card from RSS evidence when final Markdown varies."""
    output_urls = set(_BARE_SOURCE_URL_RE.findall(str(content or "")))
    output_by_key = {_url_match_key(url): url for url in output_urls if _url_match_key(url)}
    if len(output_by_key) < 2:
        return None
    selected_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for record in executed_tools:
        if not isinstance(record, dict) or str(record.get("tool") or "") not in _NEWS_TOOL_NAMES:
            continue
        for item in _rss_items_from_result_text(str(record.get("result_summary") or "")):
            url = str(((item.get("sources") or [{}])[0]).get("url") or "")
            match_key = _url_match_key(url)
            if match_key not in output_by_key or match_key in seen_urls:
                continue
            seen_urls.add(match_key)
            selected_items.append(item)
            if len(selected_items) >= 30:
                break
    if len(selected_items) < 2:
        return None
    selected_items.sort(
        key=lambda item: str(content).find(
            output_by_key.get(
                _url_match_key(str(((item.get("sources") or [{}])[0]).get("url") or "")),
                "",
            )
        )
    )
    selected_output_urls = {output_by_key[key] for key in seen_urls}
    source_markdown = _selected_news_source_markdown(content, selected_output_urls)
    if not source_markdown or len(source_markdown) > 24_000:
        return None
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": "news_digest_1",
        "type": "news_digest",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": "News Briefing",
        "sections": [{"title": "Selected Headlines", "items": selected_items}],
    }


def _build_stat_blocks(content: str, available: int) -> list[dict[str, Any]]:
    lines = str(content or "").splitlines()
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines) and len(blocks) < available:
        heading = _MARKDOWN_HEADING_RE.match(lines[index])
        if not heading:
            index += 1
            continue
        if any(hint in heading.group(1).casefold() for hint in _TIMELINE_HEADING_HINTS):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        items: list[dict[str, str]] = []
        end = cursor
        while end < len(lines) and len(items) < 8:
            match = _STAT_ITEM_RE.match(lines[end])
            if not match:
                break
            label = " ".join(match.group(1).split()).strip()[:80]
            value = " ".join(match.group(2).split()).strip()[:240]
            if label and value:
                items.append({"label": label, "value": value})
            end += 1
        if len(items) < 3:
            index += 1
            continue
        source_markdown = "\n".join(lines[index:end]).strip()
        if len(source_markdown) <= 4_000:
            blocks.append(
                {
                    "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
                    "id": f"stat_group_{len(blocks) + 1}",
                    "type": "stat_group",
                    "source_markdown": source_markdown,
                    "source_fingerprint": _source_fingerprint(source_markdown),
                    "title": heading.group(1).strip()[:160],
                    "items": items,
                }
            )
        index = max(end, index + 1)
    return blocks


def _build_timeline_blocks(content: str, available: int) -> list[dict[str, Any]]:
    lines = str(content or "").splitlines()
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines) and len(blocks) < available:
        heading = _MARKDOWN_HEADING_RE.match(lines[index])
        if not heading:
            index += 1
            continue
        title = heading.group(1).strip()[:160]
        if not any(hint in title.casefold() for hint in _TIMELINE_HEADING_HINTS):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        events: list[dict[str, str]] = []
        end = cursor
        while end < len(lines) and len(events) < 20:
            match = _TIMELINE_ITEM_RE.match(lines[end])
            if not match:
                break
            label = " ".join(match.group(1).split()).strip().rstrip(":")[:100]
            detail = " ".join(match.group(2).split()).strip()[:600]
            if label and detail:
                events.append({"label": label, "detail": detail})
            end += 1
        if len(events) < 2:
            index += 1
            continue
        source_markdown = "\n".join(lines[index:end]).strip()
        if len(source_markdown) <= 12_000:
            blocks.append(
                {
                    "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
                    "id": f"timeline_{len(blocks) + 1}",
                    "type": "timeline",
                    "source_markdown": source_markdown,
                    "source_fingerprint": _source_fingerprint(source_markdown),
                    "title": title,
                    "events": events,
                }
            )
        index = max(end, index + 1)
    return blocks


def _workspace_relative_artifact_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    user_workspace = re.search(r"(?:^|/)workspace/\d+/(.+)$", normalized)
    if user_workspace:
        return user_workspace.group(1)
    return normalized.removeprefix("/workspace/").removeprefix("workspace/")


def _artifact_source_line(content: str, path: str, relative_path: str) -> str:
    candidates = {path, relative_path}
    candidates.discard("")
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if line and len(line) <= 1_000 and any(candidate in line for candidate in candidates):
            return line
    return ""


def _artifact_media_type(filename: str) -> str:
    suffix = filename.casefold().rsplit(".", 1)[-1] if "." in filename else ""
    overrides = {
        "md": "text/markdown",
        "markdown": "text/markdown",
        "csv": "text/csv",
        "json": "application/json",
        "yaml": "application/yaml",
        "yml": "application/yaml",
    }
    return overrides.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _build_file_artifact_blocks(
    content: str,
    executed_tools: list[dict[str, Any]],
    available: int,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for record in executed_tools or []:
        if len(blocks) >= available:
            break
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        if tool_name not in _FILE_ARTIFACT_TOOL_NAMES or bool(record.get("is_error")):
            continue
        arguments = record.get("arguments")
        if not isinstance(arguments, dict):
            continue
        path = str(arguments.get("path") or "").strip()[:500]
        if not path or "\x00" in path or path in seen_paths:
            continue
        relative_path = _workspace_relative_artifact_path(path)
        source_markdown = _artifact_source_line(content, path, relative_path)
        if not source_markdown:
            continue
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        filename = relative_path.rstrip("/").rsplit("/", 1)[-1][:200]
        media_type = _artifact_media_type(filename)
        blocks.append(
            {
                "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
                "id": f"file_artifact_{len(blocks) + 1}",
                "type": "file_artifact",
                "source_markdown": source_markdown,
                "source_fingerprint": _source_fingerprint(source_markdown),
                "title": filename or "Workspace file",
                "file": {
                    "path": relative_path,
                    "filename": filename or "Workspace file",
                    "media_type": media_type[:120],
                    "operation": "appended" if tool_name == "append_file" else "written",
                },
            }
        )
    return blocks


def build_assistant_content_blocks(
    content: str,
    executed_tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract bounded structured blocks while preserving exact fallback Markdown."""
    lines = str(content or "").splitlines()
    blocks: list[dict[str, Any]] = []
    index = 0
    while index + 2 < len(lines) and len(blocks) < CONTENT_BLOCK_LIMIT:
        columns = _split_markdown_table_row(lines[index])
        valid_separator, alignments = _table_separator(lines[index + 1], len(columns))
        if not (2 <= len(columns) <= 8) or not valid_separator:
            index += 1
            continue
        row_lines: list[str] = []
        rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines) and len(rows) < 30:
            raw = lines[cursor]
            if "|" not in raw:
                break
            cells = _split_markdown_table_row(raw)
            if len(cells) != len(columns):
                break
            row_lines.append(raw)
            rows.append([cell[:240] for cell in cells])
            cursor += 1
        if not rows:
            index += 1
            continue
        source_start, title = _heading_before(lines, index)
        source_markdown = "\n".join(lines[source_start:cursor]).strip()
        if len(source_markdown) > 12_000:
            index = cursor
            continue
        block: dict[str, Any] = {
            "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
            "id": f"table_{len(blocks) + 1}",
            "type": "table",
            "source_markdown": source_markdown,
            "source_fingerprint": _source_fingerprint(source_markdown),
            "columns": [column[:120] for column in columns],
            "column_alignments": alignments,
            "rows": rows,
        }
        if title:
            block["title"] = title
        chart = _chart_hint(columns, rows)
        if chart:
            block["chart"] = chart
        blocks.append(block)
        index = cursor

    executed_tool_names = {
        str(record.get("tool") or "").strip()
        for record in (executed_tools or [])
        if isinstance(record, dict)
    }
    if executed_tool_names & _NEWS_TOOL_NAMES and len(blocks) < CONTENT_BLOCK_LIMIT:
        news_block = _build_news_block(content) or _build_news_block_from_rss_evidence(
            content,
            executed_tools or [],
        )
        if news_block:
            blocks.append(news_block)
    if len(blocks) < CONTENT_BLOCK_LIMIT:
        blocks.extend(
            _build_file_artifact_blocks(
                content,
                executed_tools or [],
                CONTENT_BLOCK_LIMIT - len(blocks),
            )
        )
    if len(blocks) < CONTENT_BLOCK_LIMIT:
        blocks.extend(_build_stat_blocks(content, CONTENT_BLOCK_LIMIT - len(blocks)))
    if len(blocks) < CONTENT_BLOCK_LIMIT:
        blocks.extend(_build_timeline_blocks(content, CONTENT_BLOCK_LIMIT - len(blocks)))
    return blocks


def _normalized_version(item: dict[str, Any]) -> int | None:
    raw = item.get("schema_version", CONTENT_BLOCK_SCHEMA_VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return None
    return version if version == CONTENT_BLOCK_SCHEMA_VERSION else None


def normalize_assistant_content_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned_blocks: list[dict[str, Any]] = []
    for item in value[:CONTENT_BLOCK_LIMIT]:
        if not isinstance(item, dict) or _normalized_version(item) is None:
            continue
        block_type = str(item.get("type") or "")
        source_markdown = str(item.get("source_markdown") or "")
        if block_type == "news_digest":
            cleaned = _normalize_news_block(item, source_markdown)
        elif block_type == "table":
            cleaned = _normalize_table_block(item, source_markdown, len(cleaned_blocks))
        elif block_type == "stat_group":
            cleaned = _normalize_stat_block(item, source_markdown, len(cleaned_blocks))
        elif block_type == "timeline":
            cleaned = _normalize_timeline_block(item, source_markdown, len(cleaned_blocks))
        elif block_type == "file_artifact":
            cleaned = _normalize_file_artifact_block(item, source_markdown, len(cleaned_blocks))
        else:
            cleaned = None
        if cleaned:
            cleaned_blocks.append(cleaned)
    return cleaned_blocks


def _normalize_file_artifact_block(
    item: dict[str, Any],
    source_markdown: str,
    block_index: int,
) -> dict[str, Any] | None:
    raw_file = item.get("file")
    if not source_markdown or len(source_markdown) > 1_000 or not isinstance(raw_file, dict):
        return None
    path = str(raw_file.get("path") or "").strip()[:500]
    if not path or "\x00" in path:
        return None
    filename = str(raw_file.get("filename") or "").strip()[:200]
    if not filename:
        filename = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1][:200]
    operation = str(raw_file.get("operation") or "written").strip().casefold()
    if operation not in {"written", "appended"}:
        operation = "written"
    media_type = str(raw_file.get("media_type") or "application/octet-stream").strip()[:120]
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": str(item.get("id") or f"file_artifact_{block_index + 1}")[:80],
        "type": "file_artifact",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": str(item.get("title") or filename or "Workspace file").strip()[:200],
        "file": {
            "path": path,
            "filename": filename or "Workspace file",
            "media_type": media_type or "application/octet-stream",
            "operation": operation,
        },
    }


def _normalize_timeline_block(
    item: dict[str, Any],
    source_markdown: str,
    block_index: int,
) -> dict[str, Any] | None:
    raw_events = item.get("events")
    if not source_markdown or len(source_markdown) > 12_000 or not isinstance(raw_events, list):
        return None
    events: list[dict[str, str]] = []
    for raw_event in raw_events[:20]:
        if not isinstance(raw_event, dict):
            continue
        label = " ".join(str(raw_event.get("label") or "").split()).strip()[:100]
        detail = " ".join(str(raw_event.get("detail") or "").split()).strip()[:600]
        if label and detail:
            events.append({"label": label, "detail": detail})
    if len(events) < 2:
        return None
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": str(item.get("id") or f"timeline_{block_index + 1}")[:80],
        "type": "timeline",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": str(item.get("title") or "Timeline").strip()[:160] or "Timeline",
        "events": events,
    }


def _normalize_stat_block(
    item: dict[str, Any],
    source_markdown: str,
    block_index: int,
) -> dict[str, Any] | None:
    raw_items = item.get("items")
    if not source_markdown or len(source_markdown) > 4_000 or not isinstance(raw_items, list):
        return None
    cleaned_items: list[dict[str, str]] = []
    for raw_item in raw_items[:8]:
        if not isinstance(raw_item, dict):
            continue
        label = " ".join(str(raw_item.get("label") or "").split()).strip()[:80]
        value = " ".join(str(raw_item.get("value") or "").split()).strip()[:240]
        if label and value:
            cleaned_items.append({"label": label, "value": value})
    if len(cleaned_items) < 3:
        return None
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": str(item.get("id") or f"stat_group_{block_index + 1}")[:80],
        "type": "stat_group",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": str(item.get("title") or "Summary").strip()[:160] or "Summary",
        "items": cleaned_items,
    }


def _normalize_news_block(item: dict[str, Any], source_markdown: str) -> dict[str, Any] | None:
    title = str(item.get("title") or "News Briefing").strip()[:160]
    sections = item.get("sections")
    if not source_markdown or len(source_markdown) > 24_000 or not isinstance(sections, list):
        return None
    cleaned_sections: list[dict[str, Any]] = []
    total_items = 0
    for section in sections[:8]:
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("title") or "").strip()[:160]
        raw_items = section.get("items")
        if not section_title or not isinstance(raw_items, list):
            continue
        cleaned_items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            if total_items >= 30 or not isinstance(raw_item, dict):
                break
            item_title = str(raw_item.get("title") or "").strip()[:240]
            summary = str(raw_item.get("summary") or "").strip()[:1_200]
            sources: list[dict[str, str]] = []
            if isinstance(raw_item.get("sources"), list):
                for source in raw_item["sources"][:4]:
                    if not isinstance(source, dict):
                        continue
                    label = str(source.get("label") or "").strip()[:100]
                    url = str(source.get("url") or "").strip()[:2_000]
                    if label and re.match(r"^https?://", url):
                        sources.append({"label": label, "url": url})
            if not item_title or not (summary or sources):
                continue
            cleaned_items.append({"title": item_title, "summary": summary, "sources": sources})
            total_items += 1
        if cleaned_items:
            cleaned_sections.append({"title": section_title, "items": cleaned_items})
    if not cleaned_sections or total_items < 2:
        return None
    return {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": str(item.get("id") or "news_digest_1")[:80],
        "type": "news_digest",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "title": title or "News Briefing",
        "sections": cleaned_sections,
    }


def _normalize_table_block(
    item: dict[str, Any],
    source_markdown: str,
    block_index: int,
) -> dict[str, Any] | None:
    columns = item.get("columns")
    rows = item.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or not source_markdown:
        return None
    if len(source_markdown) > 12_000:
        return None
    cleaned_columns = [str(value)[:120] for value in columns[:8]]
    cleaned_rows = [
        [str(value)[:240] for value in row[: len(cleaned_columns)]]
        for row in rows[:30]
        if isinstance(row, list) and len(row) == len(cleaned_columns)
    ]
    if len(cleaned_columns) < 2 or not cleaned_rows:
        return None
    raw_alignments = item.get("column_alignments")
    alignments = [
        str(value) if str(value) in {"left", "center", "right"} else "left"
        for value in (raw_alignments if isinstance(raw_alignments, list) else [])[: len(cleaned_columns)]
    ]
    if len(alignments) != len(cleaned_columns):
        alignments = ["left"] * len(cleaned_columns)
    cleaned: dict[str, Any] = {
        "schema_version": CONTENT_BLOCK_SCHEMA_VERSION,
        "id": str(item.get("id") or f"table_{block_index + 1}")[:80],
        "type": "table",
        "source_markdown": source_markdown,
        "source_fingerprint": _source_fingerprint(source_markdown),
        "columns": cleaned_columns,
        "column_alignments": alignments,
        "rows": cleaned_rows,
    }
    title = str(item.get("title") or "").strip()[:160]
    if title:
        cleaned["title"] = title
    chart = item.get("chart")
    if isinstance(chart, dict) and str(chart.get("kind") or "") in {"bar", "line"}:
        value_columns = [
            int(value)
            for value in (chart.get("value_columns") or [])[:4]
            if isinstance(value, int) and 0 < value < len(cleaned_columns)
        ]
        if value_columns:
            cleaned["chart"] = {
                "kind": str(chart["kind"]),
                "category_column": 0,
                "value_columns": value_columns,
            }
    return cleaned


def _fetch_page_title(result_summary: str) -> str:
    first_line = (result_summary or "").split("\n", 1)[0]
    return first_line.removeprefix("Title: ").strip() if first_line.startswith("Title: ") else ""


def build_assistant_activity(executed_tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    activities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        arguments = record.get("arguments") or {}
        if not tool_name or not isinstance(arguments, dict):
            continue
        action = "Used a tool"
        value = tool_name.replace("_", " ")
        if tool_name in {"web_search", "web_context"}:
            action, value = "Searched the web", str(arguments.get("query") or "").strip()
        elif tool_name in {"search_my_feeds", "search_my_feeds_timeline", "search_feeds"}:
            action, value = "Searched your feeds", str(arguments.get("query") or "").strip()
        elif tool_name == "fetch_page":
            action, value = "Read a webpage", str(arguments.get("url") or "").strip()
            value = _fetch_page_title(str(record.get("result_summary") or "")) or value
        elif tool_name == "search_library":
            action, value = "Searched your library", str(arguments.get("query") or "").strip()
        elif tool_name == "summarize_document":
            action, value = "Summarized a document", str(arguments.get("document_name") or "").strip()
        elif tool_name in {"read_file", "stat_file", "find_files", "list_directory"}:
            action = "Inspected workspace files"
            value = str(arguments.get("path") or arguments.get("pattern") or "").strip()
        elif tool_name in {"get_daily_market_data", "get_intraday_market_data"}:
            action, value = "Checked market data", str(arguments.get("symbol") or "").strip()
        key = (action, value)
        if key in seen:
            continue
        seen.add(key)
        item = {"tool_name": tool_name[:80], "label": action[:120]}
        if value:
            item["value"] = value[:500]
        activities.append(item)
        if len(activities) >= 8:
            break
    return activities


def normalize_assistant_activity(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    cleaned_activity: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        label = str(item.get("label") or "").strip()
        detail = str(item.get("value") or "").strip()
        if not tool_name or not label:
            continue
        cleaned = {"tool_name": tool_name[:80], "label": label[:120]}
        if detail:
            cleaned["value"] = detail[:500]
        cleaned_activity.append(cleaned)
    return cleaned_activity
