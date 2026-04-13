from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import Document, Task, TaskRun, TaskRunArtifact
from tests.conftest import TestSessionLocal


async def _token(client, username: str) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    return login.json()["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _patch_tool_db_sessions():
    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        yield


@pytest.mark.asyncio
async def test_mcp_initialize_returns_server_info(client):
    token = await _token(client, "mcpinituser")

    resp = await client.post(
        "/mcp/fruitcake/initialize",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["result"]["protocolVersion"] == "2024-11-05"
    assert payload["result"]["serverInfo"]["name"] == "FruitcakeAI MCP"


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_task_and_library_tools(client):
    token = await _token(client, "mcplistuser")

    resp = await client.post(
        "/mcp/fruitcake/tools/list",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert "fruitcake_propose_task_draft" in names
    assert "fruitcake_create_task" in names
    assert "fruitcake_list_library_documents" in names
    assert "fruitcake_get_scheduler_health" in names
    assert "fruitcake_list_task_runs" in names
    assert "fruitcake_get_task_run_artifacts" in names
    assert "fruitcake_get_memory_candidates" in names
    assert "fruitcake_inspect_task_run" in names
    assert "fruitcake_get_task_health_rollup" in names


@pytest.mark.asyncio
async def test_mcp_propose_task_draft_returns_recipe_payload(client):
    token = await _token(client, "mcpdraftuser")

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_propose_task_draft",
                "arguments": {
                    "title": "US Politics Daily Briefing",
                    "instruction": "Create a daily cached RSS briefing about US politics and save it to workspace/politics/us-politics.md",
                    "task_type": "recurring",
                    "schedule": "every:24h",
                    "recipe_family": "briefing",
                    "recipe_params": {
                        "briefing_mode": "morning",
                        "topic": "US Politics",
                        "path": "workspace/politics/us-politics.md",
                        "window_hours": 24,
                    },
                },
            },
        },
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    text_payload = resp.json()["result"]["content"][0]["text"]
    draft = json.loads(text_payload)
    assert draft["proposed"] is True
    assert draft["task_recipe"]["family"] == "briefing"
    assert draft["task_recipe"]["params"]["briefing_mode"] == "morning"


@pytest.mark.asyncio
async def test_mcp_create_task_and_list_tasks(client):
    token = await _token(client, "mcpcreateuser")
    headers = _auth_headers(token)

    created = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_create_task",
                "arguments": {
                    "title": "OpenClaw watch",
                    "instruction": "Watch OpenClaw mentions in my RSS feeds.",
                    "task_type": "recurring",
                    "schedule": "every:6h",
                    "recipe_family": "topic_watcher",
                    "recipe_params": {"topic": "OpenClaw", "threshold": "medium"},
                },
            },
        },
        headers=headers,
    )
    assert created.status_code == 200
    created_payload = json.loads(created.json()["result"]["content"][0]["text"])
    assert created_payload["created"] is True
    created_task_id = created_payload["task_id"]

    listed = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "fruitcake_list_tasks", "arguments": {"limit": 10}},
        },
        headers=headers,
    )
    assert listed.status_code == 200
    list_payload = json.loads(listed.json()["result"]["content"][0]["text"])
    assert list_payload["count"] >= 1
    assert any(int(task["id"]) == int(created_task_id) for task in list_payload["tasks"])


@pytest.mark.asyncio
async def test_mcp_list_library_documents_and_search(client):
    token = await _token(client, "mcplibuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        doc = Document(
            owner_id=user_id,
            filename="manual.pdf",
            original_filename="manual.pdf",
            file_path="/tmp/manual.pdf",
            file_size_bytes=128,
            mime_type="application/pdf",
            scope="personal",
            processing_status="ready",
            title="manual.pdf",
        )
        db.add(doc)
        await db.commit()

    listed = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "fruitcake_list_library_documents", "arguments": {"limit": 10}},
        },
        headers=headers,
    )
    assert listed.status_code == 200
    list_payload = json.loads(listed.json()["result"]["content"][0]["text"])
    assert any(doc["filename"] == "manual.pdf" for doc in list_payload["documents"])

    fake_rag = SimpleNamespace(
        is_ready=True,
        query=AsyncMock(
            return_value=[
                {
                    "text": "OpenClaw is mentioned here.",
                    "score": 0.91,
                    "metadata": {"filename": "manual.pdf"},
                }
            ]
        ),
    )
    with patch("app.rag.service.get_rag_service", return_value=fake_rag):
        searched = await client.post(
            "/mcp/fruitcake/tools/call",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "fruitcake_search_library",
                    "arguments": {"query": "OpenClaw", "top_k": 5},
                },
            },
            headers=headers,
        )

    assert searched.status_code == 200
    text = searched.json()["result"]["content"][0]["text"]
    assert "manual.pdf" in text
    assert "OpenClaw is mentioned here." in text


@pytest.mark.asyncio
async def test_mcp_get_scheduler_health_returns_dispatch_state(client):
    token = await _token(client, "mcpscheduleruser")

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "fruitcake_get_scheduler_health", "arguments": {}},
        },
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["status"] in {"ready", "blocked"}
    assert "blocked" in payload
    assert "summary" in payload
    assert "stale_running_count" in payload


@pytest.mark.asyncio
async def test_mcp_list_task_runs_returns_recent_runs_for_user(client):
    token = await _token(client, "mcprunsuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Run-inspect task",
            instruction="Inspect my recent runs.",
            task_type="recurring",
            status="completed",
            schedule="every:6h",
            deliver=True,
            requires_approval=True,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="completed",
            summary="Completed successfully",
        )
        db.add(run)
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_list_task_runs",
                "arguments": {"limit": 10, "task_id": task_id},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["count"] >= 1
    assert any(int(run["task_id"]) == int(task_id) for run in payload["runs"])
    assert "summary_preview" in payload["runs"][0]


@pytest.mark.asyncio
async def test_mcp_list_task_runs_supports_profile_status_and_memory_filters(client):
    token = await _token(client, "mcprunsfilteruser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        watcher = Task(
            user_id=user_id,
            title="Watcher task",
            instruction="Inspect watcher runs.",
            profile="topic_watcher",
            task_type="recurring",
            status="pending",
            schedule="every:6h",
            deliver=True,
            requires_approval=False,
        )
        other = Task(
            user_id=user_id,
            title="Other task",
            instruction="Inspect other runs.",
            profile="maintenance",
            task_type="recurring",
            status="pending",
            schedule="every:6h",
            deliver=True,
            requires_approval=False,
        )
        db.add_all([watcher, other])
        await db.flush()
        watcher_run = TaskRun(task_id=watcher.id, status="completed", summary="Watcher completed")
        other_run = TaskRun(task_id=other.id, status="failed", summary="Other failed")
        db.add_all([watcher_run, other_run])
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=watcher_run.id,
                artifact_type="memory_candidates",
                content_json=json.dumps({"candidates": [{"proposal_key": "watcher_candidate"}]}),
            )
        )
        await db.commit()

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_list_task_runs",
                "arguments": {
                    "limit": 10,
                    "status": "completed",
                    "profile": "topic_watcher",
                    "has_memory_candidates": True,
                },
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["count"] == 1
    assert payload["runs"][0]["status"] == "completed"
    assert payload["runs"][0]["task_title"] == "Watcher task"


@pytest.mark.asyncio
async def test_mcp_list_task_runs_and_inspect_expose_agent_run_metadata(client):
    token = await _token(client, "mcpagentrunsuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Agent run task",
            instruction="Inspect agent-style runs.",
            profile="maintenance",
            task_type="recurring",
            status="pending",
            schedule="every:6h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="completed",
            summary="Agent summary",
            run_kind="agent",
            agent_role="memory_reviewer",
            trigger_source="manual_admin",
            source_context_json=json.dumps({"proposal_id": 42}),
        )
        db.add(run)
        await db.commit()
        task_id = int(task.id)
        run_id = int(run.id)

    listed = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 911,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_list_task_runs",
                "arguments": {"task_id": task_id, "run_kind": "agent", "agent_role": "memory_reviewer"},
            },
        },
        headers=headers,
    )
    assert listed.status_code == 200
    listed_payload = listed.json()["result"]["structuredContent"]
    assert listed_payload["count"] == 1
    assert listed_payload["runs"][0]["run_kind"] == "agent"
    assert listed_payload["runs"][0]["agent_role"] == "memory_reviewer"
    assert listed_payload["runs"][0]["trigger_source"] == "manual_admin"
    assert listed_payload["runs"][0]["source_context"] == {"proposal_id": 42}

    inspected = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 912,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "run_id": run_id},
            },
        },
        headers=headers,
    )
    assert inspected.status_code == 200
    inspected_payload = inspected.json()["result"]["structuredContent"]
    assert inspected_payload["run"]["run_kind"] == "agent"
    assert inspected_payload["run"]["agent_role"] == "memory_reviewer"
    assert inspected_payload["run"]["trigger_source"] == "manual_admin"
    assert inspected_payload["run"]["source_context"] == {"proposal_id": 42}


@pytest.mark.asyncio
async def test_mcp_get_task_run_artifacts_returns_owned_run_outputs(client):
    token = await _token(client, "mcpartifactsuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Artifact task",
            instruction="Inspect my run artifacts.",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=True,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="completed",
            summary="Artifacts written",
        )
        db.add(run)
        await db.flush()
        artifact = TaskRunArtifact(
            task_run_id=run.id,
            artifact_type="report_markdown",
            content_text="# Artifact output",
            content_json='{"path":"workspace/reports/artifact.md"}',
        )
        db.add(artifact)
        await db.commit()
        task_id = task.id
        run_id = run.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_task_run_artifacts",
                "arguments": {"task_id": task_id, "run_id": run_id},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["count"] >= 1
    assert any(artifact["artifact_type"] == "report_markdown" for artifact in payload["artifacts"])
    assert payload["detail"] == "summary"


@pytest.mark.asyncio
async def test_mcp_get_task_run_artifacts_summarizes_large_payloads_by_default(client):
    token = await _token(client, "mcpartifactsummaryuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Prepared dataset task",
            instruction="Inspect prepared dataset artifacts.",
            profile="topic_watcher",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="Prepared dataset ready")
        db.add(run)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="prepared_dataset",
                content_json=json.dumps(
                    {
                        "topic": "OpenClaw",
                        "notes": "Watch OpenClaw",
                        "rss_items": [
                            {"title": "One"},
                            {"title": "Two"},
                            {"title": "Three"},
                        ],
                        "source_inventory": {"active_sources": [{"name": "A"}, {"name": "B"}]},
                    }
                ),
            )
        )
        await db.commit()
        task_id = task.id
        run_id = run.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_task_run_artifacts",
                "arguments": {"task_id": task_id, "run_id": run_id},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    summary = payload["artifacts"][0]["summary"]
    assert summary["kind"] == "prepared_dataset"
    assert summary["rss_item_count"] == 3
    assert summary["active_source_count"] == 2
    assert summary["top_titles"] == ["One", "Two", "Three"]


@pytest.mark.asyncio
async def test_mcp_get_memory_candidates_returns_decoded_candidates(client):
    token = await _token(client, "mcpmemorycandidatesuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Memory candidate task",
            instruction="Inspect my memory candidates.",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=True,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="completed",
            summary="Memory candidates emitted",
        )
        db.add(run)
        await db.flush()
        artifact = TaskRunArtifact(
            task_run_id=run.id,
            artifact_type="memory_candidates",
            content_json=json.dumps(
                {
                    "candidates": [
                        {
                            "proposal_key": "topic_openclaw_1",
                            "content": "OpenClaw is becoming a recurring family discussion topic.",
                            "status": "proposed",
                        }
                    ]
                }
            ),
        )
        db.add(artifact)
        await db.commit()
        task_id = task.id
        run_id = run.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_memory_candidates",
                "arguments": {"task_id": task_id, "run_id": run_id},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["count"] == 1
    assert payload["candidates"][0]["proposal_key"] == "topic_openclaw_1"


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_returns_latest_summary_and_structured_content(client):
    token = await _token(client, "mcpinspectuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Inspect me",
            instruction="Inspect my latest run.",
            profile="topic_watcher",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="Inspection summary")
        db.add(run)
        await db.flush()
        db.add_all(
            [
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="final_output",
                    content_text="Final output text",
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="validation_report",
                    content_json=json.dumps({"fatal": False, "declared_tool_called": True}),
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="run_diagnostics",
                    content_json=json.dumps(
                        {
                            "active_skills": ["repo-map"],
                            "dataset_stats": {"selected_count": 3},
                            "refresh_stats": {"sources_refreshed": 1},
                            "agent_context_budgeting": [
                                {
                                    "stage": "task_execution_step",
                                    "model": "ollama_chat/qwen2.5:14b",
                                    "tool_results_compacted": 4,
                                    "compaction_boundaries": 1,
                                    "overflow_retries": 1,
                                    "overflow_retry_succeeded": True,
                                    "loop_events_count": 1,
                                    "max_estimated_tokens_before": 64000,
                                    "max_estimated_tokens_after": 12000,
                                    "budget_events": [{"kind": "history_projection"}],
                                    "loop_events": [{"type": "repeated_tool_signature"}],
                                }
                            ],
                        }
                    ),
                ),
            ]
        )
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    result = resp.json()["result"]
    payload = result["structuredContent"]
    assert payload["found"] is True
    assert payload["task"]["id"] == task_id
    assert payload["quality_signals"]["has_artifacts"] is True
    assert payload["artifacts"]["types"] == ["run_diagnostics", "validation_report", "final_output"]
    assert payload["diagnostics"]["grounding_fatal"] is False
    assert payload["diagnostics"]["agent_context_budgeting"]["totals"]["tool_results_compacted"] == 4
    assert payload["diagnostics"]["agent_context_budgeting"]["totals"]["loop_events"] == 1
    assert json.loads(result["content"][0]["text"])["task"]["id"] == task_id


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_can_select_latest_run_with_memory_candidates(client):
    token = await _token(client, "mcpinspectmemoryuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Inspect watcher memory",
            instruction="Inspect my memory-candidate run.",
            profile="topic_watcher",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        older = TaskRun(task_id=task.id, status="completed", summary="Older")
        newer = TaskRun(task_id=task.id, status="completed", summary="Newer")
        db.add_all([older, newer])
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=older.id,
                artifact_type="memory_candidates",
                content_json=json.dumps(
                    {"candidates": [{"proposal_key": "topic_openclaw_2", "content": "OpenClaw update"}]}
                ),
            )
        )
        await db.commit()
        task_id = task.id
        older_run_id = older.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest_with_memory_candidates"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["run"]["run_id"] == older_run_id
    assert payload["memory_candidates"]["count"] == 1
    assert payload["quality_signals"]["has_memory_candidates"] is True


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_rejects_other_users_runs(client):
    owner_token = await _token(client, "mcpinspectowner")
    other_token = await _token(client, "mcpinspectother")

    owner_headers = _auth_headers(owner_token)
    other_headers = _auth_headers(other_token)

    owner_me = await client.get("/auth/me", headers=owner_headers)
    owner_id = int(owner_me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=owner_id,
            title="Private task",
            instruction="Do not expose me.",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="Private run")
        db.add(run)
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest"},
            },
        },
        headers=other_headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["found"] is False
    assert payload["message"] == "Task not found."


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_reports_missing_artifacts_on_completed_run(client):
    token = await _token(client, "mcpinspectfindingsuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Artifact-less task",
            instruction="Inspect missing artifacts.",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="No artifacts")
        db.add(run)
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert "completed_run_has_no_artifacts" in payload["inspection_findings"]


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_reports_failed_run_with_no_artifacts(client):
    token = await _token(client, "mcpinspectfaileduser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Failed task",
            instruction="Inspect failed run.",
            task_type="recurring",
            status="failed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="failed", error="Repeated formatting error")
        db.add(run)
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert "failed_run_has_no_artifacts" in payload["inspection_findings"]


@pytest.mark.asyncio
async def test_mcp_inspect_task_run_flags_low_source_overlap_memory_candidate(client):
    token = await _token(client, "mcpinspectoverlapuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="NASA watcher",
            instruction="Inspect candidate/source mismatch.",
            profile="topic_watcher",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="Watcher summary")
        db.add(run)
        await db.flush()
        db.add_all(
            [
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="prepared_dataset",
                    content_json=json.dumps(
                        {
                            "topic": "NASA",
                            "notes": "Artemis and orbital photography",
                            "rss_items": [
                                {"title": "Artemis imagery update"},
                                {"title": "NASA shares new orbital photography"},
                            ],
                            "source_inventory": {"active_sources": [{"name": "NASA"}]},
                        }
                    ),
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="memory_candidates",
                    content_json=json.dumps(
                        {
                            "candidates": [
                                {
                                    "proposal_key": "nasa_bad_candidate",
                                    "topic": "NASA",
                                    "content": "On 2026-04-05, reports about NASA indicated military activity, based on coverage from NASA.",
                                }
                            ]
                        }
                    ),
                ),
            ]
        )
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_inspect_task_run",
                "arguments": {"task_id": task_id, "mode": "latest_with_memory_candidates"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert "memory_candidate_low_source_overlap" in payload["inspection_findings"]


@pytest.mark.asyncio
async def test_mcp_get_task_health_rollup_summarizes_recent_run_patterns(client):
    token = await _token(client, "mcphealthrollupuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Rollup task",
            instruction="Inspect rollup patterns.",
            profile="daily_research_briefing",
            task_type="recurring",
            status="pending",
            schedule="every:6h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        runs = [
            TaskRun(task_id=task.id, status="completed", summary="ok one"),
            TaskRun(task_id=task.id, status="completed", summary="ok two"),
            TaskRun(task_id=task.id, status="failed", error="Same formatting error"),
            TaskRun(task_id=task.id, status="failed", error="Same formatting error"),
            TaskRun(task_id=task.id, status="cancelled", summary="cancelled"),
            TaskRun(task_id=task.id, status="cancelled", summary="cancelled again"),
        ]
        db.add_all(runs)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=runs[0].id,
                artifact_type="memory_candidates",
                content_json=json.dumps({"candidates": [{"proposal_key": "rollup_candidate"}]}),
            )
        )
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_task_health_rollup",
                "arguments": {"task_id": task_id, "window_hours": 24},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["found"] is True
    assert payload["run_count"] == 6
    assert payload["completed_count"] == 2
    assert payload["failed_count"] == 2
    assert payload["cancelled_count"] == 2
    assert payload["memory_candidate_run_count"] == 1
    assert "repeated_error_pattern_detected" in payload["findings"]
    assert "cancellation_churn_detected" in payload["findings"]
    assert "failed_or_cancelled_runs_missing_artifacts" in payload["findings"]


@pytest.mark.asyncio
async def test_mcp_get_task_health_rollup_accepts_named_window_aliases(client):
    from sqlalchemy import select

    from app.db.models import Task, TaskRun, User

    token = await _token(client, "mcpwindowaliasuser")
    headers = _auth_headers(token)

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "mcpwindowaliasuser"))
        ).scalar_one()
        task = Task(
            user_id=user.id,
            title="Window alias task",
            instruction="Inspect window alias handling.",
            profile="topic_watcher",
            task_type="recurring",
            status="pending",
            schedule="every:6h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                TaskRun(
                    task_id=task.id,
                    status="completed",
                    started_at=now - timedelta(hours=12),
                    finished_at=now - timedelta(hours=11),
                ),
                TaskRun(
                    task_id=task.id,
                    status="completed",
                    started_at=now - timedelta(days=3),
                    finished_at=now - timedelta(days=3) + timedelta(minutes=5),
                ),
            ]
        )
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 19,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_task_health_rollup",
                "arguments": {"task_id": task_id, "window": "rollup_7d"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["found"] is True
    assert payload["window_hours"] == 168
    assert payload["run_count"] == 2


@pytest.mark.asyncio
async def test_mcp_get_task_health_rollup_flags_memory_candidate_source_contradiction(client):
    token = await _token(client, "mcprollupoverlapuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="NASA rollup watcher",
            instruction="Inspect rollup candidate/source mismatch.",
            profile="topic_watcher",
            task_type="recurring",
            status="completed",
            schedule="every:24h",
            deliver=True,
            requires_approval=False,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="completed",
            started_at=datetime.now(timezone.utc) - timedelta(days=3),
            finished_at=datetime.now(timezone.utc) - timedelta(days=3) + timedelta(minutes=2),
        )
        db.add(run)
        await db.flush()
        db.add_all(
            [
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="prepared_dataset",
                    content_json=json.dumps(
                        {
                            "topic": "NASA",
                            "notes": "Artemis and orbital photography",
                            "rss_items": [
                                {"title": "Artemis imagery update"},
                                {"title": "NASA shares new orbital photography"},
                            ],
                            "source_inventory": {"active_sources": [{"name": "NASA"}]},
                        }
                    ),
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="memory_candidates",
                    content_json=json.dumps(
                        {
                            "candidates": [
                                {
                                    "proposal_key": "nasa_bad_candidate",
                                    "topic": "NASA",
                                    "content": "On 2026-04-05, reports about NASA indicated military activity, based on coverage from NASA.",
                                }
                            ]
                        }
                    ),
                ),
            ]
        )
        await db.commit()
        task_id = task.id

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_get_task_health_rollup",
                "arguments": {"task_id": task_id, "window": "7d"},
            },
        },
        headers=headers,
    )

    assert resp.status_code == 200
    payload = resp.json()["result"]["structuredContent"]
    assert payload["found"] is True
    assert payload["memory_candidate_run_count"] == 1
    assert payload["inspection_finding_counts"]["memory_candidate_low_source_overlap"] == 1
    assert "memory_candidate_source_contradiction_detected" in payload["findings"]
