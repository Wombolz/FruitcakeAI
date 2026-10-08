"""Bounded, transport-neutral contracts for Fruitcake artifacts."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ARTIFACT_ENVELOPE_VERSION = 1
ARTIFACT_PAYLOAD_MAX_BYTES = 64_000
ARTIFACT_FALLBACK_MAX_CHARS = 24_000
ARTIFACT_RESOURCE_LIMIT = 12
ARTIFACT_ACTION_LIMIT = 8

_ARTIFACT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SAFE_RESOURCE_SCHEMES = {"https", "http", "workspace", "library", "artifact", "ui"}


def _compact_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


class ArtifactResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1, max_length=1000)
    media_type: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=80)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        candidate = value.strip()
        scheme = candidate.partition(":")[0].casefold()
        if not scheme or scheme not in _SAFE_RESOURCE_SCHEMES:
            raise ValueError("artifact resource URI uses an unsupported scheme")
        return candidate


class ArtifactFallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content: str = Field(min_length=1, max_length=ARTIFACT_FALLBACK_MAX_CHARS)


class ArtifactProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=120)
    server: str | None = Field(default=None, max_length=160)
    tool: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=160)


class ArtifactPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred: Literal["inline", "inspector", "window"] = "inline"
    expandable: bool = True
    renderer: str | None = Field(default=None, max_length=160)
    ui_resource: str | None = Field(default=None, max_length=1000)

    @field_validator("ui_resource")
    @classmethod
    def validate_ui_resource(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate.startswith("ui://"):
            raise ValueError("artifact UI resources must use the ui:// scheme")
        return candidate


class ArtifactAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=80)
    label: str = Field(min_length=1, max_length=120)
    style: Literal["default", "primary", "destructive"] = "default"
    requires_confirmation: bool = False


class ArtifactEnvelope(BaseModel):
    """One artifact instance independent of its eventual renderer."""

    model_config = ConfigDict(extra="forbid")

    envelope_version: Literal[ARTIFACT_ENVELOPE_VERSION] = ARTIFACT_ENVELOPE_VERSION
    id: str | None = Field(default=None, max_length=160)
    type: str = Field(min_length=3, max_length=160)
    schema_version: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=600)
    payload: dict[str, Any] | None = None
    resources: list[ArtifactResource] = Field(default_factory=list, max_length=ARTIFACT_RESOURCE_LIMIT)
    provenance: ArtifactProvenance | None = None
    presentation: ArtifactPresentation = Field(default_factory=ArtifactPresentation)
    fallback: ArtifactFallback | None = None
    actions: list[ArtifactAction] = Field(default_factory=list, max_length=ARTIFACT_ACTION_LIMIT)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        candidate = value.strip().casefold()
        if not _ARTIFACT_TYPE_RE.fullmatch(candidate):
            raise ValueError("artifact type must be a namespaced lowercase identifier")
        return candidate

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = _compact_text(value, 200)
        if not title:
            raise ValueError("artifact title must not be blank")
        return title

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _compact_text(value, 600) or None

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact payload must be JSON serializable") from exc
        if len(encoded) > ARTIFACT_PAYLOAD_MAX_BYTES:
            raise ValueError("artifact payload exceeds the inline size limit")
        return value

    @model_validator(mode="after")
    def require_content(self) -> "ArtifactEnvelope":
        if self.payload is None and not self.resources and self.fallback is None:
            raise ValueError("artifact requires payload, resources, or fallback content")
        return self
