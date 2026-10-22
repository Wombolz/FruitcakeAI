import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from PIL import Image

from app.agent.runtime.models import ToolOutputText, build_tool_call_result
from app.api.artifacts import get_chat_image_preview
from app.api.chat import (
    _build_assistant_image_artifacts, _normalize_assistant_metadata_payload,
    _ensure_generated_image_markdown_references,
)
from app.mcp.image_previews import decode_preview, preview_uri
from app.mcp.imagelab_accounts import IMAGELAB_SERVER
from app.mcp.registry import MCPRegistry


def image_result(**overrides):
    output = io.BytesIO()
    Image.new("RGB", (8, 4)).save(output, "PNG")
    item = {"uri": preview_uri("asset-1"), "mimeType": "image/png",
            "blob": base64.b64encode(output.getvalue()).decode(), **overrides}
    return {"contents": [item]}


def private_record(**overrides):
    result = build_tool_call_result(tool_call_id="call", name="generate_image",
        content=ToolOutputText(json.dumps({"asset_id": "asset-1", "width": 1024, "height": 768,
            "prompt": "Cows on a boat", "source_server": "spoofed", **overrides}),
            source_server=IMAGELAB_SERVER))
    return result.to_execution_record()


def test_private_image_metadata_retains_trusted_provenance_and_survives_history():
    record = private_record()
    images = _build_assistant_image_artifacts([record])
    assert images[0] == {"path": "", "asset_id": "asset-1", "source_server": IMAGELAB_SERVER,
        "source_tool": "generate_image", "prompt": "Cows on a boat", "title": "Cows on a boat",
        "width": 1024, "height": 768}
    metadata = _normalize_assistant_metadata_payload({"evidence": {"image_artifacts": images}})
    assert metadata["evidence"]["image_artifacts"] == images
    assert _ensure_generated_image_markdown_references("Done", [record]) == "Done"


def test_private_asset_text_cannot_attest_its_own_provider():
    result = build_tool_call_result(tool_call_id="c", name="generate_image",
        content='{"asset_id":"asset-1","source_server":"spoofed"}')
    assert "source_server" not in result.artifacts[0]
    assert "source_server" not in _build_assistant_image_artifacts([result.to_execution_record()])[0]


def test_failed_generation_never_emits_an_image_card():
    record = private_record()
    record["is_error"] = True
    assert not _build_assistant_image_artifacts([record])


def test_deduplication_uses_asset_identity_not_empty_path():
    assert len(_build_assistant_image_artifacts([private_record(), private_record(asset_id="asset-2"), private_record()])) == 2


@pytest.mark.parametrize("asset", ["../secret", "a/b", "x?y", "https://example.com", "", "a" * 161])
def test_resource_identifier_rejects_paths_and_urls(asset):
    with pytest.raises(ValueError):
        preview_uri(asset)


def test_preview_decodes_only_a_matching_bounded_image():
    data, mime = decode_preview(image_result(), preview_uri("asset-1"))
    assert data.startswith(b"\x89PNG") and mime == "image/png"


@pytest.mark.parametrize("overrides", [
    {"uri": "imagelab://assets/other/preview"}, {"mimeType": "image/svg+xml"},
    {"mimeType": "image/jpeg"}, {"blob": "not base64"},
    {"blob": base64.b64encode(b"<html>hello</html>").decode()},
])
def test_invalid_resource_is_rejected(overrides):
    with pytest.raises(ValueError):
        decode_preview(image_result(**overrides), preview_uri("asset-1"))


def test_oversize_resource_is_rejected_before_base64_decode():
    with patch("app.mcp.image_previews.MAX_PREVIEW_BYTES", 4), pytest.raises(ValueError):
        decode_preview(image_result(), preview_uri("asset-1"))


def test_oversize_dimensions_are_rejected_even_with_small_compressed_payload():
    output = io.BytesIO()
    Image.new("RGB", (4097, 1)).save(output, "PNG")
    with pytest.raises(ValueError, match="dimension"):
        decode_preview(image_result(blob=base64.b64encode(output.getvalue()).decode()), preview_uri("asset-1"))


@pytest.mark.asyncio
async def test_registry_attests_text_only_image_tool_results_before_persistence():
    registry = MCPRegistry()
    registry._tool_map["generate_image"] = (IMAGELAB_SERVER, "streamable_http")
    registry._clients[IMAGELAB_SERVER] = SimpleNamespace(call_tool=AsyncMock(return_value={
        "success": True, "result": {"content": [{"type": "text", "text": '{"asset_id":"asset-1"}'}]},
    }))
    content = await registry.call_tool("generate_image", {}, SimpleNamespace(id=7))
    result = build_tool_call_result(tool_call_id="call", name="generate_image", content=content)
    images = _build_assistant_image_artifacts([result.to_execution_record()])
    assert images[0]["source_server"] == IMAGELAB_SERVER
    assert "source_server" not in result.to_message()["content"]


@pytest.mark.asyncio
async def test_preview_uses_the_current_account_view_without_shared_fallback():
    registry = MCPRegistry()
    view = MCPRegistry()
    client = SimpleNamespace(read_resource=AsyncMock(return_value={"success": True, "result": image_result()}))
    view._clients[IMAGELAB_SERVER] = client
    user = SimpleNamespace(id=7)
    with patch.object(registry, "_for_user", return_value=view), patch.object(view, "_for_user", return_value=view):
        data, _ = await registry.read_image_preview(IMAGELAB_SERVER, "asset-1", user)
        assert data
        client.read_resource.assert_awaited_once_with(preview_uri("asset-1"))
        client.read_resource.return_value = {"success": False, "error": "401 token-secret"}
        with pytest.raises(RuntimeError, match="unavailable"):
            await registry.read_image_preview(IMAGELAB_SERVER, "asset-1", user)
    assert not registry._clients


def preview_db(images=None, missing=False):
    db = SimpleNamespace(execute=AsyncMock())
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None if missing else SimpleNamespace(
        tool_results=json.dumps({"evidence": {"image_artifacts": images or _build_assistant_image_artifacts([private_record()])}}))
    return db


@pytest.mark.asyncio
async def test_endpoint_requires_owned_persisted_message_and_never_accepts_an_arbitrary_asset():
    db = preview_db()
    registry = SimpleNamespace(read_image_preview=AsyncMock(return_value=(b"pixels", "image/png")))
    user = SimpleNamespace(id=7)
    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await get_chat_image_preview(123, 0, user, db)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    registry.read_image_preview.assert_awaited_once_with(IMAGELAB_SERVER, "asset-1", user_context=user)
    statement = db.execute.call_args.args[0]
    assert "chat_sessions.user_id" in str(statement)
    assert statement.compile().params["user_id_1"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("index,missing", [(0, True), (-1, False), (6, False), (1, False)])
async def test_endpoint_returns_404_before_provider_for_missing_or_inaccessible_image(index, missing):
    with patch("app.api.artifacts.get_mcp_registry") as registry, pytest.raises(HTTPException) as error:
        await get_chat_image_preview(123, index, SimpleNamespace(id=7), preview_db(missing=missing))
    assert error.value.status_code == 404
    registry.assert_not_called()


@pytest.mark.asyncio
async def test_provider_errors_are_not_reflected_to_the_client():
    registry = SimpleNamespace(read_image_preview=AsyncMock(side_effect=RuntimeError("Bearer secret")))
    with patch("app.api.artifacts.get_mcp_registry", return_value=registry), pytest.raises(HTTPException) as error:
        await get_chat_image_preview(123, 0, SimpleNamespace(id=7), preview_db())
    assert error.value.status_code == 503 and "secret" not in error.value.detail
