from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.servers.calendar import (
    _calendar_matches,
    _collect_vevent_components,
    _create_event,
    _delete_event,
    _dedupe_events,
    _format_event_timestamp,
    _list_events,
    _parse_dt,
    _search_events,
)


def test_calendar_matches_is_case_insensitive_for_name_and_url():
    assert _calendar_matches("home", "Home", "https://example.com/cal/home")
    assert _calendar_matches("HOME", "Home", "https://example.com/cal/home")
    assert _calendar_matches(
        "https://EXAMPLE.com/cal/home",
        "Home",
        "https://example.com/cal/home",
    )


def test_parse_dt_returns_default_when_empty():
    default = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    assert _parse_dt("", default=default) == default
    assert _parse_dt(None, default=default) == default


def test_parse_dt_returns_none_when_invalid():
    default = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    assert _parse_dt("2026-02-29", default=default) is None


class _FakeComp:
    def __init__(self, name, subcomponents=None):
        self.name = name
        self.subcomponents = subcomponents or []


def test_collect_vevent_components_handles_direct_vevent():
    comp = _FakeComp("VEVENT", [_FakeComp("VALARM")])
    out = _collect_vevent_components(comp)
    assert len(out) == 1
    assert out[0].name == "VEVENT"


def test_collect_vevent_components_handles_vcalendar_children():
    vevent = _FakeComp("VEVENT")
    comp = _FakeComp("VCALENDAR", [_FakeComp("VTIMEZONE"), vevent, _FakeComp("VALARM")])
    out = _collect_vevent_components(comp)
    assert out == [vevent]


def test_dedupe_events_removes_exact_duplicates_preserving_order():
    events = [
        {"id": "a", "start": "2026-03-07T15:00:00", "end": "2026-03-07T16:00:00", "summary": "Fruitcake Approval Test"},
        {"id": "a", "start": "2026-03-07T15:00:00", "end": "2026-03-07T16:00:00", "summary": "Fruitcake Approval Test"},
        {"id": "b", "start": "2026-03-09T14:00:00-04:00", "end": "2026-03-09T15:00:00-04:00", "summary": "Therapy"},
    ]
    out = _dedupe_events(events)
    assert len(out) == 2
    assert out[0]["id"] == "a"
    assert out[1]["id"] == "b"

@pytest.mark.asyncio
async def test_create_event_returns_failure_when_provider_calendar_not_found():
    provider = AsyncMock()
    provider.default_calendar_id.return_value = "home"
    provider.create_event.return_value = {"id": "", "status": "calendar_not_found"}

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _create_event(
            {
                "title": "Lunch with Rod",
                "start": "2026-03-26T12:00:00",
                "end": "2026-03-26T13:00:00",
                "calendar_id": "mcp",
            },
            user_context=None,
        )

    assert out == "Failed to create event: calendar 'mcp' not found."


@pytest.mark.asyncio
async def test_delete_event_requires_explicit_confirmation():
    out = await _delete_event({"event_id": "evt_123", "confirm": False}, user_context=None)
    assert out == "Deletion requires explicit confirmation. Ask the user to confirm before deleting."


@pytest.mark.asyncio
async def test_delete_event_returns_failure_when_provider_event_not_found():
    provider = AsyncMock()
    provider.default_calendar_id.return_value = "home"
    provider.delete_event.return_value = {"id": "evt_123", "status": "event_not_found"}

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _delete_event(
            {
                "event_id": "evt_123",
                "confirm": True,
                "calendar_id": "home",
                "start": "2026-03-26T12:00:00+00:00",
            },
            user_context=None,
        )

    assert out == "Failed to delete event: event 'evt_123' not found."


@pytest.mark.asyncio
async def test_delete_event_returns_failure_when_provider_cannot_verify_deletion():
    provider = AsyncMock()
    provider.default_calendar_id.return_value = "home"
    provider.delete_event.return_value = {"id": "evt_123", "status": "delete_unverified"}

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _delete_event(
            {
                "event_id": "evt_123",
                "confirm": True,
                "calendar_id": "home",
                "start": "2026-03-26T12:00:00+00:00",
            },
            user_context=None,
        )

    assert out == (
        "Failed to verify deletion for event 'evt_123'. "
        "The calendar provider did not confirm that the event was removed."
    )


@pytest.mark.asyncio
async def test_delete_event_returns_success_message():
    provider = AsyncMock()
    provider.default_calendar_id.return_value = "home"
    provider.delete_event.return_value = {
        "id": "evt_123",
        "status": "deleted",
        "summary": "Lunch with Rod",
        "start": "2026-03-26T12:00:00+00:00",
    }

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _delete_event(
            {
                "event_id": "evt_123",
                "confirm": True,
                "calendar_id": "home",
                "start": "2026-03-26T12:00:00+00:00",
            },
            user_context=None,
        )

    assert out.startswith("Event deleted: 'Lunch with Rod' (evt_123)")


@pytest.mark.asyncio
async def test_delete_event_returns_failure_when_provider_requires_start_timestamp():
    provider = AsyncMock()
    provider.default_calendar_id.return_value = "home"
    provider.delete_event.return_value = {"id": "evt_123", "status": "missing_start"}

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _delete_event(
            {
                "event_id": "evt_123",
                "confirm": True,
                "calendar_id": "home",
            },
            user_context=None,
        )

    assert out == (
        "Failed to delete event: event 'evt_123' needs a start timestamp for bounded lookup. "
        "List the event again and retry the delete with the exact event details."
    )


@pytest.mark.asyncio
async def test_list_events_includes_event_ids():
    provider = AsyncMock()
    provider.list_events.return_value = [
        {
            "id": "evt_123",
            "summary": "Lunch with Rod",
            "start": "2026-03-26T12:00:00+00:00",
            "end": "2026-03-26T13:00:00+00:00",
            "location": None,
            "description": None,
        }
    ]

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _list_events({"start_date": "2026-03-26", "end_date": "2026-03-27"}, user_context=None)

    assert "[evt_123]" in out


@pytest.mark.asyncio
async def test_search_events_includes_event_ids():
    provider = AsyncMock()
    provider.list_events.return_value = [
        {
            "id": "evt_123",
            "summary": "Lunch with Rod",
            "description": "",
            "location": "",
            "start": "2026-03-26T12:00:00+00:00",
            "end": "2026-03-26T13:00:00+00:00",
        }
    ]

    with patch("app.mcp.servers.calendar._get_provider", return_value=provider):
        out = await _search_events({"query": "rod"}, user_context=None)

    assert "[evt_123]" in out

def test_format_event_timestamp_derives_correct_weekday_from_iso_date():
    assert _format_event_timestamp("2026-03-19T11:00:00+00:00") == "Thursday, 2026-03-19 11:00"
    assert _format_event_timestamp("2026-03-20T13:00:00+00:00") == "Friday, 2026-03-20 13:00"
    assert _format_event_timestamp("2026-03-21T09:00:00+00:00") == "Saturday, 2026-03-21 09:00"
    assert _format_event_timestamp("2026-03-22T14:00:00+00:00") == "Sunday, 2026-03-22 14:00"
