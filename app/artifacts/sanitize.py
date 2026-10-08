"""Conservative sanitizers for static HTML and SVG artifact payloads."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_xml_fromstring

_HTML_ALLOWED_TAGS = {
    "a", "article", "aside", "b", "blockquote", "br", "caption", "code",
    "dd", "details", "div", "dl", "dt", "em", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i",
    "li", "main", "mark", "ol", "p", "pre", "s", "section", "small",
    "span", "strong", "summary", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
}
_HTML_VOID_TAGS = {"br", "hr"}
_HTML_BLOCKED_WITH_CONTENT = {"canvas", "embed", "form", "iframe", "object", "script", "style"}
_HTML_GLOBAL_ATTRIBUTES = {"aria-label", "colspan", "rowspan", "scope", "title"}

_SVG_ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "defs", "linearGradient", "radialGradient",
    "stop", "clipPath", "mask", "title", "desc",
}
_SVG_ALLOWED_ATTRIBUTES = {
    "viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2",
    "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "fill-opacity",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "opacity", "transform", "font-family", "font-size",
    "font-weight", "text-anchor", "dominant-baseline", "dx", "dy", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform",
    "spreadMethod", "id", "class", "clip-path", "mask",
}
_SAFE_FRAGMENT_REFERENCE_RE = re.compile(r"^url\(#[A-Za-z_][A-Za-z0-9_.:-]*\)$")


def _safe_link(value: Any) -> str:
    candidate = str(value or "").strip()[:1000]
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class _StaticHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _HTML_BLOCKED_WITH_CONTENT:
            self._blocked_depth += 1
            return
        if self._blocked_depth or tag not in _HTML_ALLOWED_TAGS:
            return
        cleaned: list[str] = []
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            value = str(raw_value or "")
            if name.startswith("on") or name in {"style", "src", "srcdoc"}:
                continue
            if tag == "a" and name == "href":
                safe_href = _safe_link(value)
                if safe_href:
                    cleaned.extend([
                        f'href="{html.escape(safe_href, quote=True)}"',
                        'rel="noopener noreferrer"',
                    ])
                continue
            if name in _HTML_GLOBAL_ATTRIBUTES:
                cleaned.append(f'{name}="{html.escape(value[:300], quote=True)}"')
        suffix = f" {' '.join(cleaned)}" if cleaned else ""
        self.parts.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _HTML_BLOCKED_WITH_CONTENT:
            self._blocked_depth = max(self._blocked_depth - 1, 0)
            return
        if not self._blocked_depth and tag in _HTML_ALLOWED_TAGS and tag not in _HTML_VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(html.escape(data))


def sanitize_static_html(value: Any) -> str:
    content = str(value or "").strip()
    if not content:
        raise ValueError("HTML artifact content is empty")
    parser = _StaticHTMLSanitizer()
    parser.feed(content)
    parser.close()
    sanitized = "".join(parser.parts).strip()
    if not sanitized:
        raise ValueError("HTML artifact contains no supported static content")
    return sanitized


def _svg_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def sanitize_static_svg(value: Any) -> str:
    content = str(value or "").strip()
    if not content:
        raise ValueError("SVG artifact content is empty")
    try:
        root = safe_xml_fromstring(content)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("SVG artifact is not valid XML") from exc
    if _svg_local_name(root.tag) != "svg":
        raise ValueError("SVG artifact root element must be svg")

    def clean(element: ET.Element) -> None:
        for child in list(element):
            if _svg_local_name(child.tag) not in _SVG_ALLOWED_TAGS:
                element.remove(child)
                continue
            clean(child)
        cleaned_attributes: dict[str, str] = {}
        for raw_name, raw_value in element.attrib.items():
            name = _svg_local_name(raw_name)
            value = str(raw_value).strip()[:2000]
            if name.startswith("on") or name not in _SVG_ALLOWED_ATTRIBUTES:
                continue
            if "url(" in value.casefold() and not _SAFE_FRAGMENT_REFERENCE_RE.fullmatch(value):
                continue
            cleaned_attributes[name] = value
        element.attrib.clear()
        element.attrib.update(cleaned_attributes)

    clean(root)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)
