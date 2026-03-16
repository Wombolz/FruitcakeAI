from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import Document
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


async def _user_id(client, token: str) -> int:
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    return int(me.json()["id"])


async def _seed_doc(owner_id: int, name: str = "notes.txt", scope: str = "personal") -> int:
    async with TestSessionLocal() as db:
        doc = Document(
            owner_id=owner_id,
            filename=name,
            original_filename=name,
            file_path=f"/tmp/{name}",
            file_size_bytes=128,
            mime_type="text/plain",
            scope=scope,
            processing_status="ready",
            title=name,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return int(doc.id)


@pytest.mark.asyncio
async def test_get_document_details_returns_metadata(client):
    token = await _token(client, "libdetailuser")
    user_id = await _user_id(client, token)
    doc_id = await _seed_doc(user_id)

    resp = await client.get(
        f"/library/documents/{doc_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == doc_id
    assert "filename" in data
    assert "processing_status" in data
    assert "file_size_bytes" in data


@pytest.mark.asyncio
async def test_get_document_details_rejects_unauthorized_user(client):
    owner_token = await _token(client, "libowner")
    other_token = await _token(client, "libother")
    owner_id = await _user_id(client, owner_token)
    doc_id = await _seed_doc(owner_id, "owner.txt", scope="personal")

    resp = await client.get(
        f"/library/documents/{doc_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_document_excerpts_filters_to_requested_doc(client):
    token = await _token(client, "libexcerptuser")
    user_id = await _user_id(client, token)
    doc_id = await _seed_doc(user_id, "excerpt.txt")

    fake_rag = SimpleNamespace(
        is_ready=True,
        query=AsyncMock(
            return_value=[
                {
                    "text": "match one",
                    "score": 0.91,
                    "metadata": {"document_id": str(doc_id), "filename": "excerpt.txt"},
                },
                {
                    "text": "other doc",
                    "score": 0.88,
                    "metadata": {"document_id": "9999", "filename": "other.txt"},
                },
            ]
        ),
    )

    with patch("app.api.library.get_rag_service", return_value=fake_rag):
        resp = await client.get(
            f"/library/documents/{doc_id}/excerpts",
            params={"q": "match", "top_k": 10},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["document_id"] == doc_id
    assert data["count"] == 1
    assert data["results"][0]["text"] == "match one"
