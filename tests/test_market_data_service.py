from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models import Document, Secret, User
from app.market_data_service import get_daily_market_data
from app.secrets_service import encrypt_secret_value
from tests.conftest import TestSessionLocal


async def _seed_user_with_alphavantage_secret() -> int:
    async with TestSessionLocal() as db:
        user = User(
            username="marketuser",
            email="marketuser@example.com",
            hashed_password="x",
            role="parent",
            persona="family_assistant",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        secret = Secret(
            user_id=user.id,
            name="alphavantage_api_key",
            provider="alphavantage",
            ciphertext=encrypt_secret_value("alpha-key"),
            is_active=True,
        )
        db.add(secret)
        await db.commit()
        return int(user.id)


@pytest.mark.asyncio
async def test_get_daily_market_data_renders_csv_and_saves_document():
    user_id = await _seed_user_with_alphavantage_secret()
    payload = {
        "Meta Data": {"2. Symbol": "SPY"},
        "Time Series (Daily)": {
            "2026-03-27": {
                "1. open": "552.10",
                "2. high": "555.25",
                "3. low": "549.80",
                "4. close": "553.42",
                "5. volume": "1000000",
            },
            "2026-03-26": {
                "1. open": "548.00",
                "2. high": "553.00",
                "3. low": "547.50",
                "4. close": "552.00",
                "5. volume": "900000",
            },
        },
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            result = await get_daily_market_data(
                db,
                user_id=user_id,
                symbol="SPY",
                days=2,
                provider="alphavantage",
                save_to_library=True,
                output_format="csv",
            )
            await db.commit()

        assert result["symbol"] == "SPY"
        assert result["output_format"] == "csv"
        assert "date,open,high,low,close,volume" in result["rendered"]
        assert result["saved_document_id"] is not None
        assert result["saved_document_name"].endswith(".csv")

        doc = await db.get(Document, int(result["saved_document_id"]))
        assert doc is not None
        assert doc.original_filename == result["saved_document_name"]
        assert doc.scope == "personal"
        assert doc.processing_status == "processing"


@pytest.mark.asyncio
async def test_get_daily_market_data_renders_table_without_saving():
    user_id = await _seed_user_with_alphavantage_secret()
    payload = {
        "Meta Data": {"2. Symbol": "KO"},
        "Time Series (Daily)": {
            "2026-03-27": {
                "1. open": "75.19",
                "2. high": "75.75",
                "3. low": "74.65",
                "4. close": "74.69",
                "5. volume": "11202133",
            },
        },
    }

    async with TestSessionLocal() as db:
        with patch("app.api_service.fetch_json", new=AsyncMock(return_value=payload)):
            result = await get_daily_market_data(
                db,
                user_id=user_id,
                symbol="KO",
                days=1,
                provider="alphavantage",
                save_to_library=False,
                output_format="table",
            )

        assert "Alpha Vantage daily market data for KO:" in result["rendered"]
        assert result["saved_document_id"] is None
        docs = (await db.execute(select(Document).where(Document.owner_id == user_id))).scalars().all()
        assert docs == []
