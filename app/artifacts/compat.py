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
