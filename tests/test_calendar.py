from __future__ import annotations

from datetime import datetime, timezone

from app.mcp.servers.calendar import (
    _calendar_matches,
    _collect_vevent_components,
    _dedupe_events,
    _parse_dt,
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
