from __future__ import annotations

import pytest


async def _admin_headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "role": "admin",
        },
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_memory_graph_diagnostics_returns_counts_and_entities(client):
    headers = await _admin_headers(client, "graphadmin")

    person = await client.post(
        "/memories/graph/entities",
        json={"name": "James", "entity_type": "person"},
        headers=headers,
    )
    org = await client.post(
        "/memories/graph/entities",
        json={"name": "Acme Corp", "entity_type": "organization"},
        headers=headers,
    )
    assert person.status_code == 201
    assert org.status_code == 201

    relation = await client.post(
        "/memories/graph/relations",
        json={
            "from_entity_id": person.json()["id"],
            "to_entity_id": org.json()["id"],
            "relation_type": "works_at",
        },
        headers=headers,
    )
    assert relation.status_code == 201

    diag = await client.get("/admin/memory-graph/diagnostics", headers=headers)
    assert diag.status_code == 200
    data = diag.json()
    assert data["total_entities"] == 2
    assert data["total_relations"] == 1
    assert data["total_observations"] == 0
    assert any(item["name"] == "James" for item in data["entities"])


@pytest.mark.asyncio
async def test_admin_memory_graph_entity_inspect_returns_named_relations(client):
    headers = await _admin_headers(client, "graphinspectadmin")

    person = await client.post(
        "/memories/graph/entities",
        json={"name": "James", "entity_type": "person"},
        headers=headers,
    )
    org = await client.post(
        "/memories/graph/entities",
        json={"name": "Acme Corp", "entity_type": "organization"},
        headers=headers,
    )
    await client.post(
        "/memories/graph/relations",
        json={
            "from_entity_id": person.json()["id"],
            "to_entity_id": org.json()["id"],
            "relation_type": "works_at",
        },
        headers=headers,
    )

    opened = await client.get(f"/admin/memory-graph/entities/{person.json()['id']}", headers=headers)
    assert opened.status_code == 200
    payload = opened.json()
    assert payload["entity"]["name"] == "James"
    assert payload["entity"]["relation_count"] == 1
    assert payload["relations"][0]["from_entity_name"] == "James"
    assert payload["relations"][0]["to_entity_name"] == "Acme Corp"
