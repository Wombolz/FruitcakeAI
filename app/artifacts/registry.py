"""Artifact type registry with explicit renderer trust boundaries."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.artifacts.contracts import ArtifactEnvelope


class ArtifactRendererClass(StrEnum):
    NATIVE_BUILTIN = "native_builtin"
    NATIVE_DECLARATIVE = "native_declarative"
    MCP_APP = "mcp_app"
    GENERIC_FALLBACK = "generic_fallback"


class ArtifactTypeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    display_name: str = Field(min_length=1, max_length=120)
    schema_versions: tuple[int, ...] = (1,)
    renderer_class: ArtifactRendererClass
    renderer: str = Field(min_length=1, max_length=160)
    fallback_renderer: str = Field(default="generic_json", max_length=160)
    preferred_presentation: str = Field(default="inline", pattern=r"^(inline|inspector|window)$")
    capabilities: tuple[str, ...] = ()
    source: str = Field(default="builtin", pattern=r"^(builtin|declarative|mcp_app|fallback)$")

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        candidate = value.strip().casefold()
        parts = candidate.split(".")
        if len(parts) < 2 or any(
            not part or not part[0].isalpha() or not all(char.isalnum() or char in "_-" for char in part)
            for part in parts
        ):
            raise ValueError("artifact type must be a namespaced lowercase identifier")
        return candidate

    @field_validator("schema_versions")
    @classmethod
    def validate_versions(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        versions = tuple(sorted(set(value)))
        if not versions or any(version < 1 for version in versions):
            raise ValueError("artifact type requires at least one positive schema version")
        return versions


class ArtifactTypeRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ArtifactTypeDefinition] = {}

    def register(self, definition: ArtifactTypeDefinition) -> None:
        existing = self._definitions.get(definition.type)
        if existing is not None:
            if existing.source == "builtin" or definition.source != existing.source:
                raise ValueError(f"artifact type is already registered: {definition.type}")
        self._definitions[definition.type] = definition

    def resolve(self, artifact_type: str) -> ArtifactTypeDefinition:
        definition = self._definitions.get(artifact_type)
        if definition is not None:
            return definition
        return ArtifactTypeDefinition(
            type=artifact_type,
            display_name="Artifact",
            renderer_class=ArtifactRendererClass.GENERIC_FALLBACK,
            renderer="generic_json",
            source="fallback",
        )

    def validate(self, value: ArtifactEnvelope | dict) -> ArtifactEnvelope:
        envelope = value if isinstance(value, ArtifactEnvelope) else ArtifactEnvelope.model_validate(value)
        definition = self.resolve(envelope.type)
        if definition.source != "fallback" and envelope.schema_version not in definition.schema_versions:
            raise ValueError(
                f"unsupported schema version {envelope.schema_version} for {envelope.type}"
            )
        return envelope

    def definitions(self) -> tuple[ArtifactTypeDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def diagnostics(self) -> list[dict[str, object]]:
        return [
            {
                "type": definition.type,
                "schema_versions": list(definition.schema_versions),
                "renderer_class": definition.renderer_class.value,
                "renderer": definition.renderer,
                "source": definition.source,
            }
            for definition in self.definitions()
        ]


def _builtin(
    artifact_type: str,
    display_name: str,
    renderer: str,
    *,
    presentation: str = "inline",
    capabilities: tuple[str, ...] = (),
) -> ArtifactTypeDefinition:
    return ArtifactTypeDefinition(
        type=artifact_type,
        display_name=display_name,
        renderer_class=ArtifactRendererClass.NATIVE_BUILTIN,
        renderer=renderer,
        preferred_presentation=presentation,
        capabilities=capabilities,
    )


artifact_registry = ArtifactTypeRegistry()
for _definition in (
    _builtin("core.document", "Document", "document", presentation="inspector", capabilities=("copy", "download", "search")),
    _builtin("core.file", "File", "file", presentation="inspector", capabilities=("open", "download")),
    _builtin("core.image", "Image", "image", capabilities=("zoom", "download")),
    _builtin("core.svg", "SVG", "sanitized_svg", capabilities=("zoom", "download")),
    _builtin("core.html", "HTML", "sandboxed_html", presentation="inspector", capabilities=("open",)),
    _builtin("core.table", "Data", "table", capabilities=("filter", "sort", "copy_csv", "export_csv")),
    _builtin("core.chart", "Chart", "chart", capabilities=("inspect_data", "export")),
    _builtin("core.timeline", "Timeline", "timeline", capabilities=("open_source",)),
    _builtin("core.places", "Places", "places", capabilities=("open_map", "open_source")),
    _builtin("core.web_source", "Web Source", "web_source", presentation="inspector", capabilities=("open", "cite")),
    _builtin("core.code", "Code", "code", presentation="inspector", capabilities=("copy", "download")),
    _builtin("core.collection", "Collection", "collection", capabilities=("expand",)),
    _builtin("fruitcake.news_digest", "News Digest", "news_digest", capabilities=("open_source",)),
    _builtin("fruitcake.stat_group", "Key Facts", "stat_group"),
):
    artifact_registry.register(_definition)

artifact_registry.register(ArtifactTypeDefinition(
    type="core.mcp_app",
    display_name="MCP App",
    renderer_class=ArtifactRendererClass.MCP_APP,
    renderer="mcp_app",
    preferred_presentation="inline",
    capabilities=("open_link",),
    source="mcp_app",
))
