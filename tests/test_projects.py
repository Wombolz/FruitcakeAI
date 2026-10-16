"""Project organization and chat membership regressions."""

from __future__ import annotations

import pytest
from uuid import uuid4


async def _token(client, username: str) -> str:
    username = f"{username}{uuid4().hex[:8]}"
    register = await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "testpass123",
        },
    )
    response = await client.post(
        "/auth/login",
        json={"username": username, "password": "testpass123"},
    )
    assert register.status_code == 201, register.text
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_project_crud_and_chat_assignment_survive_reload(client):
    token = await _token(client, "projectowner")
    headers = _headers(token)

    project_response = await client.post(
        "/projects",
        json={"name": "Fruitcake", "description": "Core product work"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project = project_response.json()
    assert project["name"] == "Fruitcake"
    assert project["public_id"]

    session_response = await client.post(
        "/chat/sessions",
        json={"title": "Navigation plan", "project_id": project["id"]},
        headers=headers,
    )
    assert session_response.status_code == 201
    session = session_response.json()
    assert session["project_id"] == project["id"]
    assert session["is_pinned"] is False

    pin_response = await client.patch(
        f"/chat/sessions/{session['id']}/pin",
        json={"pinned": True},
        headers=headers,
    )
    assert pin_response.status_code == 200
    assert pin_response.json()["is_pinned"] is True

    sessions = (await client.get("/chat/sessions", headers=headers)).json()
    assert sessions[0]["project_id"] == project["id"]
    assert sessions[0]["is_pinned"] is True

    detach = await client.patch(
        f"/chat/sessions/{session['id']}/project",
        json={"project_id": None},
        headers=headers,
    )
    assert detach.status_code == 200
    assert detach.json()["project_id"] is None


@pytest.mark.asyncio
async def test_deleting_project_unassigns_chat_without_deleting_it(client):
    token = await _token(client, "projectdelete")
    headers = _headers(token)
    project = (await client.post("/projects", json={"name": "Temporary"}, headers=headers)).json()
    session = (
        await client.post(
            "/chat/sessions",
            json={"title": "Keep me", "project_id": project["id"]},
            headers=headers,
        )
    ).json()

    response = await client.delete(f"/projects/{project['id']}", headers=headers)
    assert response.status_code == 204

    sessions = (await client.get("/chat/sessions", headers=headers)).json()
    kept = next(item for item in sessions if item["id"] == session["id"])
    assert kept["project_id"] is None


@pytest.mark.asyncio
async def test_projects_and_chat_assignment_are_user_scoped(client):
    owner = await _token(client, "projectownertwo")
    outsider = await _token(client, "projectoutsider")
    owner_headers = _headers(owner)
    outsider_headers = _headers(outsider)

    project = (await client.post("/projects", json={"name": "Private"}, headers=owner_headers)).json()
    session = (await client.post("/chat/sessions", json={"title": "Owner chat"}, headers=owner_headers)).json()
    outsider_session = (
        await client.post("/chat/sessions", json={"title": "Outsider chat"}, headers=outsider_headers)
    ).json()

    assert (await client.patch(
        f"/chat/sessions/{session['id']}/project",
        json={"project_id": project["id"]},
        headers=outsider_headers,
    )).status_code == 404
    assert (await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Stolen"},
        headers=outsider_headers,
    )).status_code == 404
    assert (await client.patch(
        f"/chat/sessions/{outsider_session['id']}/project",
        json={"project_id": project["id"]},
        headers=outsider_headers,
    )).status_code == 404
    assert (await client.post(
        "/chat/sessions",
        json={"title": "Foreign project", "project_id": project["id"]},
        headers=outsider_headers,
    )).status_code == 404
    assert (await client.get("/projects", headers=outsider_headers)).json() == []


@pytest.mark.asyncio
async def test_archived_project_rejects_new_chat_assignment(client):
    token = await _token(client, "projectarchive")
    headers = _headers(token)
    project = (await client.post("/projects", json={"name": "Archived"}, headers=headers)).json()
    session = (await client.post("/chat/sessions", json={"title": "Loose"}, headers=headers)).json()

    archive = await client.patch(
        f"/projects/{project['id']}",
        json={"archived": True},
        headers=headers,
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    assign = await client.patch(
        f"/chat/sessions/{session['id']}/project",
        json={"project_id": project["id"]},
        headers=headers,
    )
    assert assign.status_code == 409


@pytest.mark.asyncio
async def test_project_reorder_requires_exact_active_project_set(client):
    token = await _token(client, "projectorder")
    headers = _headers(token)
    first = (await client.post("/projects", json={"name": "First"}, headers=headers)).json()
    second = (await client.post("/projects", json={"name": "Second"}, headers=headers)).json()

    response = await client.patch(
        "/projects/order",
        json={"project_ids": [first["id"], second["id"]]},
        headers=headers,
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [first["id"], second["id"]]

    invalid = await client.patch(
        "/projects/order",
        json={"project_ids": [first["id"], first["id"]]},
        headers=headers,
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_sidebar_layout_atomically_reorders_projects_and_moves_chats(client):
    token = await _token(client, "sidebarlayout")
    headers = _headers(token)
    first_project = (await client.post("/projects", json={"name": "First"}, headers=headers)).json()
    second_project = (await client.post("/projects", json={"name": "Second"}, headers=headers)).json()
    first_chat = (await client.post("/chat/sessions", json={"title": "One"}, headers=headers)).json()
    second_chat = (await client.post("/chat/sessions", json={"title": "Two"}, headers=headers)).json()
    third_chat = (await client.post("/chat/sessions", json={"title": "Three"}, headers=headers)).json()

    response = await client.patch(
        "/chat/sidebar-layout",
        json={
            "project_ids": [first_project["id"], second_project["id"]],
            "sessions": [
                {
                    "session_id": first_chat["id"],
                    "project_id": second_project["id"],
                    "is_pinned": True,
                },
                {
                    "session_id": third_chat["id"],
                    "project_id": first_project["id"],
                    "is_pinned": False,
                },
                {
                    "session_id": second_chat["id"],
                    "is_pinned": False,
                },
            ],
        },
        headers=headers,
    )
    assert response.status_code == 204

    projects = (await client.get("/projects", headers=headers)).json()
    assert [project["id"] for project in projects] == [first_project["id"], second_project["id"]]
    sessions = (await client.get("/chat/sessions", headers=headers)).json()
    assert [session["id"] for session in sessions] == [
        first_chat["id"],
        third_chat["id"],
        second_chat["id"],
    ]
    assert sessions[0]["project_id"] == second_project["id"]
    assert sessions[0]["is_pinned"] is True
    assert sessions[1]["project_id"] == first_project["id"]
    assert sessions[2]["project_id"] is None


@pytest.mark.asyncio
async def test_sidebar_layout_rejects_partial_or_archived_destinations(client):
    token = await _token(client, "sidebarlayoutreject")
    headers = _headers(token)
    project = (await client.post("/projects", json={"name": "Archived"}, headers=headers)).json()
    chat = (await client.post("/chat/sessions", json={"title": "Loose"}, headers=headers)).json()
    await client.patch(
        f"/projects/{project['id']}",
        json={"archived": True},
        headers=headers,
    )

    archived_destination = await client.patch(
        "/chat/sidebar-layout",
        json={
            "project_ids": [],
            "sessions": [{
                "session_id": chat["id"],
                "project_id": project["id"],
                "is_pinned": False,
            }],
        },
        headers=headers,
    )
    assert archived_destination.status_code == 409

    partial = await client.patch(
        "/chat/sidebar-layout",
        json={"project_ids": [], "sessions": []},
        headers=headers,
    )
    assert partial.status_code == 422
