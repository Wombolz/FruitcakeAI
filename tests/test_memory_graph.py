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

    search = await client.get("/memories/graph/search", params={"q": "jim"}, headers=headers)
    assert search.status_code == 200
    data = search.json()
    assert len(data) == 1
    assert data[0]["name"] == "James"

    opened = await client.get(f"/memories/graph/entities/{person_id}", headers=headers)
    assert opened.status_code == 200
    node = opened.json()
    assert node["entity"]["name"] == "James"
    assert len(node["relations"]) == 1
    assert node["relations"][0]["relation_type"] == "works_at"
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
