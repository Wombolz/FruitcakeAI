from __future__ import annotations

import pytest

from app.db.models import Memory
from tests.conftest import TestSessionLocal


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _user_id(client, headers: dict[str, str]) -> int:
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    return int(me.json()["id"])


async def _seed_memory(user_id: int, content: str = "James works at Acme Corp.") -> int:
    async with TestSessionLocal() as db:
        memory = Memory(
            user_id=user_id,
            memory_type="semantic",
            content=content,
            importance=0.8,
            access_count=0,
            tags="[]",
            is_active=True,
        )
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return int(memory.id)


@pytest.mark.asyncio
async def test_memory_graph_entity_relation_observation_flow(client):
    headers = await _headers(client, "graphuser")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id)

    person = await client.post(
        "/memories/graph/entities",
        json={"name": "James", "entity_type": "person", "aliases": ["Jim"]},
        headers=headers,
    )
    assert person.status_code == 201
    person_id = person.json()["id"]

    org = await client.post(
        "/memories/graph/entities",
        json={"name": "Acme Corp", "entity_type": "organization"},
        headers=headers,
    )
    assert org.status_code == 201
    org_id = org.json()["id"]

    relation = await client.post(
        "/memories/graph/relations",
        json={
            "from_entity_id": person_id,
            "to_entity_id": org_id,
            "relation_type": "works_at",
            "source_memory_id": memory_id,
        },
        headers=headers,
    )
    assert relation.status_code == 201
    assert relation.json()["relation_type"] == "works_at"
    assert relation.json()["source_memory_id"] == memory_id

    observation = await client.post(
        "/memories/graph/observations",
        json={"entity_id": person_id, "source_memory_id": memory_id},
        headers=headers,
    )
    assert observation.status_code == 201
    assert observation.json()["source_memory_id"] == memory_id
    assert observation.json()["content"] is None

    listed = await client.get("/memories/graph/entities", headers=headers)
    assert listed.status_code == 200
    listed_data = listed.json()
    assert len(listed_data) == 2
    james_row = next(item for item in listed_data if item["name"] == "James")
    assert james_row["relation_count"] == 1
    assert james_row["observation_count"] == 1

    search = await client.get("/memories/graph/search", params={"q": "jim"}, headers=headers)
    assert search.status_code == 200
    data = search.json()
    assert len(data) == 1
    assert data[0]["name"] == "James"
    assert data[0]["relation_count"] == 1
    assert data[0]["observation_count"] == 1

    opened = await client.get(f"/memories/graph/entities/{person_id}", headers=headers)
    assert opened.status_code == 200
    node = opened.json()
    assert node["entity"]["name"] == "James"
    assert node["relation_count"] == 1
    assert node["observation_count"] == 1
    assert len(node["relations"]) == 1
    assert node["relations"][0]["relation_type"] == "works_at"
    assert node["relations"][0]["from_entity"]["name"] == "James"
    assert node["relations"][0]["to_entity"]["name"] == "Acme Corp"
    assert len(node["observations"]) == 1
    assert node["observations"][0]["source_memory_id"] == memory_id


@pytest.mark.asyncio
async def test_memory_graph_rejects_cross_user_relation(client):
    headers_a = await _headers(client, "graphowner")
    headers_b = await _headers(client, "graphother")

    entity_a = await client.post(
        "/memories/graph/entities",
        json={"name": "Owner Project", "entity_type": "project"},
        headers=headers_a,
    )
    entity_b = await client.post(
        "/memories/graph/entities",
        json={"name": "Other Project", "entity_type": "project"},
        headers=headers_b,
    )
    assert entity_a.status_code == 201
    assert entity_b.status_code == 201

    resp = await client.post(
        "/memories/graph/relations",
        json={
            "from_entity_id": entity_a.json()["id"],
            "to_entity_id": entity_b.json()["id"],
            "relation_type": "related_to",
        },
        headers=headers_a,
    )
    assert resp.status_code == 422
    assert "not found" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_memory_graph_entity_and_observation_updates_and_deactivation(client):
    headers = await _headers(client, "graphmutate")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id, content="Jim lives in Savannah.")

    entity = await client.post(
        "/memories/graph/entities",
        json={"name": "Jim", "entity_type": "person", "aliases": ["James"]},
        headers=headers,
    )
    assert entity.status_code == 201
    entity_id = entity.json()["id"]

    observation = await client.post(
        "/memories/graph/observations",
        json={"entity_id": entity_id, "content": "Lives in Savannah", "source_memory_id": memory_id},
        headers=headers,
    )
    assert observation.status_code == 201
    observation_id = observation.json()["id"]
    assert observation.json()["is_active"] is True

    patched_entity = await client.patch(
        f"/memories/graph/entities/{entity_id}",
        json={"name": "James", "aliases": ["Jim"], "confidence": 0.9},
        headers=headers,
    )
    assert patched_entity.status_code == 200
    assert patched_entity.json()["name"] == "James"
    assert patched_entity.json()["aliases"] == ["Jim"]
    assert patched_entity.json()["confidence"] == pytest.approx(0.9)

    patched_observation = await client.patch(
        f"/memories/graph/observations/{observation_id}",
        json={"content": "Lives in Savannah, Georgia", "confidence": 0.8},
        headers=headers,
    )
    assert patched_observation.status_code == 200
    assert patched_observation.json()["content"] == "Lives in Savannah, Georgia"
    assert patched_observation.json()["confidence"] == pytest.approx(0.8)
    assert patched_observation.json()["is_active"] is True

    listed_before = await client.get("/memories/graph/entities", headers=headers)
    assert listed_before.status_code == 200
    assert listed_before.json()[0]["observation_count"] == 1

    deactivated_observation = await client.delete(
        f"/memories/graph/observations/{observation_id}",
        headers=headers,
    )
    assert deactivated_observation.status_code == 204

    opened_after_observation_delete = await client.get(f"/memories/graph/entities/{entity_id}", headers=headers)
    assert opened_after_observation_delete.status_code == 200
    assert opened_after_observation_delete.json()["observation_count"] == 0
    assert opened_after_observation_delete.json()["observations"] == []

    listed_after = await client.get("/memories/graph/entities", headers=headers)
    assert listed_after.status_code == 200
    assert listed_after.json()[0]["observation_count"] == 0

    deactivated_entity = await client.delete(f"/memories/graph/entities/{entity_id}", headers=headers)
    assert deactivated_entity.status_code == 204

    list_after_entity_delete = await client.get("/memories/graph/entities", headers=headers)
    assert list_after_entity_delete.status_code == 200
    assert list_after_entity_delete.json() == []

    search_after_entity_delete = await client.get("/memories/graph/search", params={"q": "james"}, headers=headers)
    assert search_after_entity_delete.status_code == 200
    assert search_after_entity_delete.json() == []

    opened_after_entity_delete = await client.get(f"/memories/graph/entities/{entity_id}", headers=headers)
    assert opened_after_entity_delete.status_code == 404
