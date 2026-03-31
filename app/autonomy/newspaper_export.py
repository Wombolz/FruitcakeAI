from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.config import settings
from app.time_utils import format_localized_datetime, localize_datetime, to_utc, utc_compact_timestamp, utc_day_folder


@dataclass
class NewspaperEditionExport:
    manifest: Dict[str, Any]
    edition_dir: Path
    pdf_path: Path
    markdown_path: Path
    manifest_path: Path


def normalize_magazine_markdown(text: str) -> str:
    if not text:
        return ""

    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []

    def ensure_blank_line() -> None:
        if out and out[-1] != "":
            out.append("")

    for raw in lines:
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue

        if _is_masthead_line(line):
            if out:
                ensure_blank_line()
            out.append(f"# {_normalize_masthead(line)}")
            out.append("")
            continue

        if _looks_like_section_heading(line):
            ensure_blank_line()
            out.append(f"## {_strip_heading_marker(line)}")
            out.append("")
            continue

        if line.startswith("- **Headline:**"):
            ensure_blank_line()
            out.append(line)
            continue

        if line.startswith("[Read More]("):
            out.append(line)
            out.append("")
            continue

        out.append(line)

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def export_newspaper_edition(
    *,
    task_id: int,
    task_run_id: int,
    session_id: Optional[int],
    profile: str,
    final_markdown: str,
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    duration_seconds: Optional[float],
    publish_mode: str,
    dataset_stats: Dict[str, Any],
    refresh_stats: Dict[str, Any],
    active_skills: List[str],
    timezone_name: Optional[str] = None,
) -> NewspaperEditionExport:
    finished_utc = to_utc(finished_at) or datetime.now(timezone.utc)
    finished_local = localize_datetime(finished_utc, timezone_name)
    started_utc = to_utc(started_at)
    edition_stamp = utc_compact_timestamp(finished_utc)
    day_folder = utc_day_folder(finished_utc)

    storage_root = Path(settings.storage_dir)
    edition_dir = (
        storage_root
        / "exports"
        / "newspapers"
        / f"task-{task_id}"
        / day_folder
        / f"{edition_stamp}-task-{task_id}-run-{task_run_id}"
    )
    edition_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = edition_dir / "edition.md"
    pdf_path = edition_dir / "edition.pdf"
    manifest_path = edition_dir / "edition.json"

    normalized_markdown = normalize_magazine_markdown(final_markdown)
    markdown_path.write_text(normalized_markdown, encoding="utf-8")
    render_newspaper_pdf(
        pdf_path=pdf_path,
        markdown=normalized_markdown,
        edition_label=format_localized_datetime(
            finished_utc,
            timezone_name=timezone_name,
        ),
        task_id=task_id,
        task_run_id=task_run_id,
    )

    manifest = {
        "task_id": task_id,
        "task_run_id": task_run_id,
        "session_id": session_id,
        "profile": profile,
        "started_at": started_utc.isoformat() if started_utc else None,
        "finished_at": finished_utc.isoformat(),
        "display_timezone": finished_local.tzname() if finished_local else "UTC",
        "display_published_at": finished_local.isoformat() if finished_local else finished_utc.isoformat(),
        "duration_seconds": duration_seconds,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "publish_mode": publish_mode,
        "dataset_stats": dataset_stats or {},
        "refresh_stats": refresh_stats or {},
        "active_skills": active_skills or [],
        "edition_dir": _relative_to_storage(edition_dir),
        "pdf_relative_path": _relative_to_storage(pdf_path),
        "markdown_relative_path": _relative_to_storage(markdown_path),
        "manifest_relative_path": _relative_to_storage(manifest_path),
        "download_path": f"/admin/task-runs/{task_run_id}/edition.pdf",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")

    return NewspaperEditionExport(
        manifest=manifest,
        edition_dir=edition_dir,
        pdf_path=pdf_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
    )


def render_newspaper_pdf(
    *,
    pdf_path: Path,
    markdown: str,
    edition_label: str,
    task_id: int,
    task_run_id: int,
) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.75 * inch,
        title="Fruitcake News",
        author="FruitcakeAI",
    )

    styles = _build_styles()
    story: list[Any] = []
    lines = markdown.splitlines()
    masthead = "Fruitcake News"
    body_lines = lines
    if lines and lines[0].startswith("# "):
        masthead = lines[0][2:].strip() or masthead
        body_lines = lines[1:]

    story.append(Paragraph(_escape(_pdf_safe_text(masthead)), styles["masthead"]))
    story.append(Paragraph(_escape(_pdf_safe_text(edition_label)), styles["edition"]))
    story.append(Spacer(1, 0.22 * inch))

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 0.12 * inch))
            continue

        if stripped.startswith("## "):
            story.append(Spacer(1, 0.06 * inch))
            story.append(Paragraph(_escape(_pdf_safe_text(stripped[3:].strip())), styles["section"]))
            story.append(Spacer(1, 0.06 * inch))
            continue

        if stripped.startswith("- **Headline:**"):
            headline = stripped.split(":", 1)[1].strip()
            story.append(Paragraph(_escape(_pdf_safe_text(headline)), styles["headline"]))
            continue

        if stripped.startswith("**Source:**") or stripped.startswith("Source:"):
            story.append(Paragraph(_escape(_pdf_safe_text(_strip_label_markdown(stripped))), styles["meta"]))
            continue

        if stripped.startswith("**Published at:**") or stripped.startswith("Published at:"):
            story.append(Paragraph(_escape(_pdf_safe_text(_strip_label_markdown(stripped))), styles["meta"]))
            continue

        if stripped.startswith("**Summary:**") or stripped.startswith("Summary:"):
            story.append(Paragraph(_escape(_pdf_safe_text(_strip_label_markdown(stripped))), styles["body"]))
            continue

        if stripped.startswith("[Read More]("):
            url = stripped[len("[Read More](") : -1]
            story.append(Paragraph(f'<link href="{_escape_attr(url)}">Read More</link>', styles["link"]))
            story.append(Spacer(1, 0.14 * inch))
            continue

        story.append(Paragraph(_escape(_pdf_safe_text(stripped)), styles["body"]))

    def _draw_footer(canvas, _doc) -> None:
        canvas.saveState()
        footer = _pdf_safe_text(f"Task {task_id} · Run {task_run_id}")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(colors.HexColor("#666666"))
        width = stringWidth(footer, "Helvetica", 9)
        canvas.drawString((letter[0] - width) / 2.0, 0.45 * inch, footer)
        canvas.restoreState()

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)


def _build_styles():
    styles = getSampleStyleSheet()
    return {
        "masthead": ParagraphStyle(
            "Masthead",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#111111"),
            spaceAfter=4,
        ),
        "edition": ParagraphStyle(
            "Edition",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#555555"),
        ),
        "section": ParagraphStyle(
            "Section",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=18,
            textColor=colors.HexColor("#1f2937"),
            borderWidth=0,
            borderPadding=0,
            spaceBefore=4,
            spaceAfter=4,
        ),
        "headline": ParagraphStyle(
            "Headline",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#111111"),
            spaceBefore=2,
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            textColor=colors.HexColor("#4b5563"),
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#222222"),
            spaceAfter=2,
        ),
        "link": ParagraphStyle(
            "Link",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=colors.HexColor("#0b5fff"),
            spaceAfter=2,
        ),
    }


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(value: str) -> str:
    return _escape(value).replace('"', "&quot;")


def _pdf_safe_text(value: str) -> str:
    text = str(value)
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": "-",
        "\u00b7": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii")


def _relative_to_storage(path: Path) -> str:
    storage_root = Path(settings.storage_dir).resolve()
    return str(path.resolve().relative_to(storage_root))



def _is_masthead_line(line: str) -> bool:
    lowered = line.lower()
    return ("news magazine" in lowered or lowered == "fruitcake news") and not line.startswith("#")


def _normalize_masthead(line: str) -> str:
    lowered = line.lower().strip()
    if lowered == "fruitcake news" or "news magazine" in lowered:
        return "Fruitcake News"
    return line.strip()


def _looks_like_section_heading(line: str) -> bool:
    if line.startswith("## "):
        return True
    return line in {
        "Top Stories",
        "World",
        "Business",
        "Technology",
        "Tech",
        "Science",
        "Politics",
        "Health",
        "Sports",
        "Culture",
        "Other",
    }


def _strip_heading_marker(line: str) -> str:
    return line[3:].strip() if line.startswith("## ") else line.strip()


def _strip_label_markdown(line: str) -> str:
    return re.sub(r"^\*\*([^*]+)\*\*:\s*", r"\1: ", line).strip()
