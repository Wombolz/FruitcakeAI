import pytest
from pydantic import ValidationError

from app.artifacts.compat import content_block_to_artifact, content_blocks_to_artifacts
from app.artifacts.contracts import ArtifactEnvelope
from app.artifacts.registry import (
    ArtifactRendererClass,
    ArtifactTypeDefinition,
    ArtifactTypeRegistry,
    artifact_registry,
)


def test_core_registry_contains_reusable_native_artifact_types():
    definitions = {definition.type: definition for definition in artifact_registry.definitions()}

    assert {
        "core.document",
        "core.file",
        "core.image",
        "core.svg",
        "core.html",
        "core.table",
        "core.chart",
        "core.timeline",
        "core.places",
        "core.web_source",
        "core.code",
        "core.collection",
    }.issubset(definitions)
    assert definitions["core.html"].renderer == "sandboxed_html"
    assert definitions["core.svg"].renderer == "sanitized_svg"

    diagnostics = artifact_registry.diagnostics()
    assert any(
        item == {
            "type": "core.html",
            "schema_versions": [1],
            "renderer_class": "native_builtin",
            "renderer": "sandboxed_html",
            "source": "builtin",
        }
        for item in diagnostics
    )


def test_artifact_envelope_requires_namespaced_type_and_bounded_content():
    with pytest.raises(ValidationError, match="namespaced lowercase identifier"):
        ArtifactEnvelope(type="table", schema_version=1, title="Data", payload={"rows": []})

    with pytest.raises(ValidationError, match="requires payload, resources, or fallback"):
        ArtifactEnvelope(type="core.table", schema_version=1, title="Data")

    with pytest.raises(ValidationError, match="inline size limit"):
        ArtifactEnvelope(
            type="core.document",
            schema_version=1,
            title="Oversized",
            payload={"body": "x" * 70_000},
        )


def test_registry_rejects_builtin_override_and_unsupported_schema_version():
    registry = ArtifactTypeRegistry()
    definition = ArtifactTypeDefinition(
        type="core.table",
        display_name="Data",
        renderer_class=ArtifactRendererClass.NATIVE_BUILTIN,
        renderer="table",
    )
    registry.register(definition)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)

    with pytest.raises(ValueError, match="unsupported schema version"):
        registry.validate(
            ArtifactEnvelope(
                type="core.table",
                schema_version=2,
                title="Data",
                payload={"rows": []},
            )
        )


def test_unknown_namespaced_type_uses_generic_fallback_renderer():
    definition = artifact_registry.resolve("campus_av.room_status")

    assert definition.renderer_class == ArtifactRendererClass.GENERIC_FALLBACK
    assert definition.renderer == "generic_json"


def test_legacy_timeline_block_adapts_to_common_artifact_envelope():
    artifact = content_block_to_artifact(
        {
            "schema_version": 1,
            "id": "timeline_1",
            "type": "timeline",
            "title": "Launch History",
            "source_markdown": "### Launch History\n- **Day 1:** Started\n- **Day 2:** Finished",
            "events": [
                {"label": "Day 1", "detail": "Started"},
                {"label": "Day 2", "detail": "Finished"},
            ],
        }
    )

    assert artifact is not None
    assert artifact.type == "core.timeline"
    assert artifact.title == "Launch History"
    assert artifact.payload == {
        "events": [
            {"label": "Day 1", "detail": "Started"},
            {"label": "Day 2", "detail": "Finished"},
        ]
    }
    assert artifact.fallback is not None
    assert artifact.fallback.media_type == "text/markdown"
    assert artifact.provenance is not None
    assert artifact.provenance.provider == "fruitcake"


def test_legacy_adapter_is_bounded_and_skips_unknown_blocks():
    blocks = [
        {
            "schema_version": 1,
            "id": f"table_{index}",
            "type": "table",
            "source_markdown": "| A |\n|---|\n| 1 |",
            "columns": ["A"],
            "rows": [["1"]],
        }
        for index in range(10)
    ]
    blocks.insert(0, {"schema_version": 1, "type": "unknown", "payload": {}})

    artifacts = content_blocks_to_artifacts(blocks)

    assert len(artifacts) == 7
    assert all(artifact.type == "core.table" for artifact in artifacts)
