"""Versioned artifact contracts and renderer registration."""

from app.artifacts.contracts import (
    ArtifactAction,
    ArtifactEnvelope,
    ArtifactFallback,
    ArtifactPresentation,
    ArtifactProvenance,
    ArtifactResource,
)
from app.artifacts.registry import (
    ArtifactRendererClass,
    ArtifactTypeDefinition,
    ArtifactTypeRegistry,
    artifact_registry,
)
from app.artifacts.compat import artifact_envelopes_from_tool_records, normalize_artifact_envelopes

__all__ = [
    "ArtifactAction",
    "ArtifactEnvelope",
    "ArtifactFallback",
    "ArtifactPresentation",
    "ArtifactProvenance",
    "ArtifactRendererClass",
    "ArtifactResource",
    "ArtifactTypeDefinition",
    "ArtifactTypeRegistry",
    "artifact_registry",
    "artifact_envelopes_from_tool_records",
    "normalize_artifact_envelopes",
]
