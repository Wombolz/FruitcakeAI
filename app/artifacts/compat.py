"""Compatibility adapters for Fruitcake's existing rich chat blocks."""

from __future__ import annotations

from typing import Any

from app.artifacts.contracts import (
    ArtifactEnvelope,
    ArtifactFallback,
    ArtifactPresentation,
    ArtifactProvenance,
)
from app.artifacts.registry import artifact_registry
from app.artifacts.sanitize import sanitize_static_html, sanitize_static_svg

_CONTENT_BLOCK_TYPES = {
    "table": "core.table",
    "timeline": "core.timeline",
    "place_group": "core.places",
    "file_artifact": "core.file",
    "code_artifact": "core.code",
    "news_digest": "fruitcake.news_digest",
    "stat_group": "fruitcake.stat_group",
}

_BLOCK_DEFAULT_TITLES = {
    "table": "Data",
    "timeline": "Timeline",
    "place_group": "Places",
    "file_artifact": "File",
    "code_artifact": "Code",
    "news_digest": "News",
    "stat_group": "Key Facts",
}


def content_block_to_artifact(block: dict[str, Any]) -> ArtifactEnvelope | None:
    """Translate one normalized legacy chat block without changing stored metadata."""
    if not isinstance(block, dict):
        return None
    block_type = str(block.get("type") or "").strip()
    artifact_type = _CONTENT_BLOCK_TYPES.get(block_type)
    if artifact_type is None:
        return None

    payload = {
        key: value
        for key, value in block.items()
        if key not in {"schema_version", "id", "type", "title", "source_markdown"}
    }
    source_markdown = str(block.get("source_markdown") or "").strip()
    fallback = (
        ArtifactFallback(media_type="text/markdown", content=source_markdown[:24_000])
        if source_markdown
        else None
    )
    definition = artifact_registry.resolve(artifact_type)
    envelope = ArtifactEnvelope(
        id=str(block.get("id") or "").strip()[:160] or None,
        type=artifact_type,
        schema_version=int(block.get("schema_version") or 1),
        title=str(block.get("title") or _BLOCK_DEFAULT_TITLES[block_type]),
        payload=payload or None,
        provenance=ArtifactProvenance(provider="fruitcake", tool="chat_content"),
        presentation=ArtifactPresentation(preferred=definition.preferred_presentation),
        fallback=fallback,
    )
    return artifact_registry.validate(envelope)


def content_blocks_to_artifacts(value: Any) -> list[ArtifactEnvelope]:
    if not isinstance(value, list):
        return []
    artifacts: list[ArtifactEnvelope] = []
    for block in value[:8]:
        artifact = content_block_to_artifact(block) if isinstance(block, dict) else None
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def _sanitize_renderable_artifact(envelope: ArtifactEnvelope) -> ArtifactEnvelope:
    if envelope.type not in {"core.html", "core.svg"}:
        return envelope
    payload = dict(envelope.payload or {})
    content = payload.get("content")
    if envelope.type == "core.html":
        payload["content"] = sanitize_static_html(content)
    else:
        payload["content"] = sanitize_static_svg(content)
    return envelope.model_copy(update={"payload": payload})


def artifact_envelopes_from_tool_records(records: Any) -> list[ArtifactEnvelope]:
    """Extract validated display artifacts without treating tool payloads as model context."""
    if not isinstance(records, list):
        return []
    artifacts: list[ArtifactEnvelope] = []
    seen: set[tuple[str, str | None]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        structured = record.get("structured_content")
        if not isinstance(structured, dict):
            continue
        candidates: list[Any] = []
        if isinstance(structured.get("artifact"), dict):
            candidates.append(structured["artifact"])
        if isinstance(structured.get("artifacts"), list):
            candidates.extend(structured["artifacts"])
        if {"type", "schema_version", "title"}.issubset(structured):
            candidates.append(structured)
        for envelope in normalize_artifact_envelopes(candidates):
            key = (
                envelope.type,
                envelope.id or envelope.model_dump_json(exclude_none=True),
            )
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(envelope)
            if len(artifacts) >= 8:
                return artifacts
    return artifacts


def normalize_artifact_envelopes(value: Any) -> list[ArtifactEnvelope]:
    if not isinstance(value, list):
        return []
    artifacts: list[ArtifactEnvelope] = []
    for candidate in value[:8]:
        if not isinstance(candidate, dict):
            continue
        try:
            envelope = artifact_registry.validate(candidate)
            envelope = _sanitize_renderable_artifact(envelope)
            envelope = artifact_registry.validate(envelope)
        except (TypeError, ValueError):
            continue
        artifacts.append(envelope)
    return artifacts
