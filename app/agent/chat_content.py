"""Bounded, versioned rich-content metadata for assistant chat messages."""

from __future__ import annotations

import hashlib
import re
from typing import Any

CONTENT_BLOCK_SCHEMA_VERSION = 1
CONTENT_BLOCK_LIMIT = 8

_NEWS_SOURCE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_NEWS_SECTION_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")
_NEWS_ITEM_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*\s*$")
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,4}\s+(.+?)\s*$")
_NEWS_TOOL_NAMES = {
    "get_feed_items",
    "list_recent_feed_items",
    "search_feeds",
    "search_my_feeds",
    "search_my_feeds_timeline",
}


def _source_fingerprint(source_markdown: str) -> str:
    return hashlib.sha256(source_markdown.encode("utf-8")).hexdigest()[:16]


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
            index += 1
            summary_lines: list[str] = []
            sources: list[dict[str, str]] = []

            while index < len(lines):
                if _NEWS_SECTION_RE.match(lines[index]) or _NEWS_ITEM_RE.match(lines[index]):
                    break
                raw_line = lines[index].strip()
                links = _NEWS_SOURCE_LINK_RE.findall(raw_line)
                if links:
                    for label, url in links[:4]:
                        sources.append({"label": label.strip()[:100], "url": url.strip()[:2_000]})
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
        news_block = _build_news_block(content)
        if news_block:
            blocks.append(news_block)
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
        else:
            cleaned = None
        if cleaned:
            cleaned_blocks.append(cleaned)
    return cleaned_blocks


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
