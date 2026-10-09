from __future__ import annotations

import json

import httpx
import pytest

from app.agent.local_model_lifecycle import (
    clear_tracked_local_models,
    release_tracked_local_models,
    track_local_model_use,
    tracked_local_models,
)


@pytest.fixture(autouse=True)
def _clear_model_tracking():
    clear_tracked_local_models()
    yield
    clear_tracked_local_models()


def test_tracks_only_normalized_ollama_models():
    track_local_model_use("ollama_chat/qwen3.6:35b")
    track_local_model_use("ollama/qwen3.6:35b")
    track_local_model_use("gpt-5-mini")

    assert tracked_local_models() == ("qwen3.6:35b",)


@pytest.mark.asyncio
async def test_shutdown_releases_each_tracked_model_with_keep_alive_zero():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"done": True})

    track_local_model_use("ollama_chat/qwen3.6:35b")
    track_local_model_use("ollama_chat/qwen3.8:27b-q4_K_M")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await release_tracked_local_models(
            api_base="http://localhost:11434/v1",
            client=client,
        )

    assert report == {
        "requested": 2,
        "released": ["qwen3.6:35b", "qwen3.8:27b-q4_K_M"],
        "failed": [],
    }
    assert [request.url.path for request in requests] == ["/api/generate", "/api/generate"]
    assert [json.loads(request.read()) for request in requests] == [
        {"model": "qwen3.6:35b", "keep_alive": 0},
        {"model": "qwen3.8:27b-q4_K_M", "keep_alive": 0},
    ]
    assert tracked_local_models() == ()


@pytest.mark.asyncio
async def test_shutdown_release_is_best_effort():
    async def handler(request: httpx.Request) -> httpx.Response:
        if b"missing" in request.read():
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"done": True})

    track_local_model_use("ollama_chat/missing:model")
    track_local_model_use("ollama_chat/present:model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await release_tracked_local_models(client=client)

    assert report["released"] == ["present:model"]
    assert report["failed"] == ["missing:model"]


@pytest.mark.asyncio
async def test_shutdown_release_is_a_noop_without_tracked_models():
    report = await release_tracked_local_models()

    assert report == {"requested": 0, "released": [], "failed": []}
