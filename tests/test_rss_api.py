from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.services import rss_sources


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_sources_seeds_defaults(client):
    headers = await _headers(client, "rssuser1")

    resp = await client.get("/rss/sources", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all("url" in row for row in data)


@pytest.mark.asyncio
async def test_add_and_delete_user_source(client):
    headers = await _headers(client, "rssuser2")

    create = await client.post(
        "/rss/sources",
        headers=headers,
        json={
            "name": "My Feed",
            "url": "https://example.com/feed.xml",
            "category": "tech",
            "update_interval_minutes": 30,
            "active": True,
        },
    )
    assert create.status_code == 201
    source_id = create.json()["id"]

    delete = await client.delete(f"/rss/sources/{source_id}", headers=headers)
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_discover_and_approve_candidate_flow(client):
    headers = await _headers(client, "rssuser3")

    fake_discovery = AsyncMock(
        return_value=[
            {
                "url": "https://example.com/rss.xml",
                "url_canonical": "https://example.com/rss.xml",
                "title_hint": "Example Feed",
            }
        ]
    )

    with patch.object(rss_sources, "discover_candidate_urls", fake_discovery):
        discover = await client.post(
            "/rss/candidates/discover",
            headers=headers,
            json={"seed_url": "https://example.com", "max_candidates": 5},
        )

    assert discover.status_code == 200
    candidates = discover.json()
    assert len(candidates) == 1
    candidate_id = candidates[0]["id"]

    approve = await client.post(
        f"/rss/candidates/{candidate_id}/approve",
        headers=headers,
        json={"name": "Example Approved", "category": "news"},
    )
    assert approve.status_code == 200
    assert approve.json()["name"] == "Example Approved"


@pytest.mark.asyncio
async def test_reject_candidate_flow(client):
    headers = await _headers(client, "rssuser4")

    fake_discovery = AsyncMock(
        return_value=[
            {
                "url": "https://foo.com/feed",
                "url_canonical": "https://foo.com/feed",
                "title_hint": "Foo Feed",
            }
        ]
    )

    with patch.object(rss_sources, "discover_candidate_urls", fake_discovery):
        discover = await client.post(
            "/rss/candidates/discover",
            headers=headers,
            json={"seed_url": "https://foo.com", "max_candidates": 5},
        )

    candidate_id = discover.json()[0]["id"]

    reject = await client.post(
        f"/rss/candidates/{candidate_id}/reject",
        headers=headers,
        json={"reason": "Low quality source"},
    )
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"
    assert reject.json()["reason"] == "Low quality source"
