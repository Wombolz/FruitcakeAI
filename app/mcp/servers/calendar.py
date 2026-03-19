"""
FruitcakeAI v5 — Calendar MCP server (internal_python)

Tools: list_events, create_event, search_events
Providers: Google Calendar, Apple CalDAV (graceful when not configured)

To enable Google Calendar:
  pip install google-api-python-client google-auth
  GOOGLE_CALENDAR_ENABLED=true
  GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE=/path/to/sa.json
  GOOGLE_CALENDAR_DELEGATED_USER=you@gmail.com  # if using domain-wide delegation

To enable Apple CalDAV:
  pip install caldav icalendar
  APPLE_CALDAV_ENABLED=true
  APPLE_CALDAV_URL=https://caldav.icloud.com
  APPLE_CALDAV_USERNAME=you@icloud.com
  APPLE_CALDAV_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # from appleid.apple.com
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


# ── MCP interface ─────────────────────────────────────────────────────────────

def get_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "list_events",
            "description": (
                "List calendar events in a date range. "
                "Use when the user asks about their schedule, upcoming events, or what's on the calendar. "
                "Available calendars: Home (default), Work, Family, Calendar, 'Turkey Cookin'. "
                "If the user doesn't specify a calendar, use the default (Home). "
                "If the request sounds work-related use Work; family events use Family."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in ISO format (e.g. 2026-03-01). Defaults to today.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in ISO format. Defaults to 30 days from start_date.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar name: 'Home', 'Work', 'Family', 'Calendar', or 'Turkey Cookin'. Defaults to Home.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum events to return. Default: 20.",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "create_event",
            "description": (
                "Create a new calendar event. "
                "Available calendars: Home (default), Work, Family, Calendar, 'Turkey Cookin'. "
                "Infer the best calendar from context if the user doesn't specify. "
                "IMPORTANT: If the event date is described as a weekday pattern "
                "(e.g. 'first Thursday', 'last Friday of the month', 'every second Monday'), "
                "you MUST call compute_dates first to get the exact ISO dates. "
                "Never calculate weekday-of-month dates yourself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "start": {
                        "type": "string",
                        "description": "Start datetime in ISO format (e.g. 2026-03-15T14:00:00)",
                    },
                    "end": {
                        "type": "string",
                        "description": "End datetime in ISO format (e.g. 2026-03-15T15:00:00)",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar name: 'Home', 'Work', 'Family', 'Calendar', or 'Turkey Cookin'. Defaults to Home.",
                    },
                    "description": {"type": "string", "description": "Optional event description"},
                    "location": {"type": "string", "description": "Optional location"},
                },
                "required": ["title", "start", "end"],
            },
        },
        {
            "name": "search_events",
            "description": (
                "Search calendar events by keyword across a date range. "
                "Searches the default calendar (Home). For targeted searches, use list_events with a specific calendar_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for"},
                    "days_back": {
                        "type": "integer",
                        "description": "Days to look back from today. Default: 30.",
                        "default": 30,
                    },
                    "days_forward": {
                        "type": "integer",
                        "description": "Days to look forward from today. Default: 60.",
                        "default": 60,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "compute_dates",
            "description": (
                "Compute exact dates for a recurring pattern using Python — never guess dates yourself. "
                "Use this BEFORE creating multiple events to get the correct dates. "
                "Examples: 'first thursday of each month', 'every other monday', 'last friday of each month'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Day pattern. Format: '{ordinal} {weekday}' where ordinal is "
                            "first/second/third/fourth/last and weekday is monday–sunday. "
                            "E.g. 'first thursday', 'last friday', 'third wednesday'."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of occurrences to compute. Default: 6.",
                        "default": 6,
                    },
                    "start_from": {
                        "type": "string",
                        "description": "ISO date to start from (e.g. 2026-03-01). Defaults to today.",
                    },
                },
                "required": ["pattern"],
            },
        },
    ]


async def call_tool(tool_name: str, arguments: Dict[str, Any], user_context: Any) -> str:
    if tool_name == "list_events":
        return await _list_events(arguments, user_context)
    if tool_name == "create_event":
        return await _create_event(arguments, user_context)
    if tool_name == "search_events":
        return await _search_events(arguments, user_context)
    if tool_name == "compute_dates":
        return _compute_dates(arguments)
    return f"Unknown calendar tool: {tool_name}"


# ── Tool implementations ──────────────────────────────────────────────────────

async def _list_events(args: Dict[str, Any], user_context: Any) -> str:
    now = datetime.now(timezone.utc)
    start_raw = args.get("start_date")
    if start_raw:
        start_dt = _parse_dt(start_raw, default=now)
        if start_dt is None:
            return (
                f"Invalid start_date '{start_raw}'. Use ISO date/time, e.g. "
                "2026-03-06 or 2026-03-06T09:00:00."
            )
    else:
        start_dt = now
    end_str = args.get("end_date")
    if end_str:
        end_dt = _parse_dt(end_str, default=start_dt + timedelta(days=30))
        if end_dt is None:
            return (
                f"Invalid end_date '{end_str}'. Use ISO date/time, e.g. "
                "2026-03-20 or 2026-03-20T18:00:00."
            )
    else:
        end_dt = start_dt + timedelta(days=30)
    if end_dt <= start_dt:
        return (
            f"Invalid date range: end_date ({end_dt.isoformat()}) must be after "
            f"start_date ({start_dt.isoformat()})."
        )
    max_results = min(int(args.get("max_results", 20)), 100)
    calendar_id = args.get("calendar_id")

    provider = _get_provider()
    if provider is None:
        return _not_configured()

    try:
        events = await provider.list_events(
            calendar_id=calendar_id,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            max_results=max_results,
        )
        events = _dedupe_events(events)
    except Exception as e:
        log.error("list_events failed", error=str(e))
        return f"Calendar error: {e}"

    if not events:
        return f"No events found between {start_dt.date()} and {end_dt.date()}."

    lines = [f"Events from {start_dt.date()} to {end_dt.date()}:\n"]
    for ev in events:
        s = (ev.get("start") or "")[:16].replace("T", " ")
        e = (ev.get("end") or "")[:16].replace("T", " ")
        summary = ev.get("summary") or "Untitled"
        loc = f" @ {ev['location']}" if ev.get("location") else ""
        lines.append(f"• {s} – {e}: {summary}{loc}")
        if ev.get("description"):
            lines.append(f"  {ev['description'][:100]}")
    return "\n".join(lines)


async def _create_event(args: Dict[str, Any], user_context: Any) -> str:
    title = (args.get("title") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    if not title or not start or not end:
        return "Error: title, start, and end are required."

    provider = _get_provider()
    if provider is None:
        return _not_configured()

    try:
        result = await provider.create_event(
            calendar_id=args.get("calendar_id") or provider.default_calendar_id(),
            payload=_EventPayload(
                summary=title,
                start=start,
                end=end,
                description=args.get("description"),
                location=args.get("location"),
            ),
        )
        status = str(result.get("status") or "").strip().lower()
        if status and status != "created":
            if status == "calendar_not_found":
                requested = args.get("calendar_id") or provider.default_calendar_id()
                return f"Failed to create event: calendar '{requested}' not found."
            return f"Failed to create event: provider returned status '{status}'."
        display_start = start[:16].replace("T", " ")
        return f"Event created: '{title}' on {display_start} (id: {result.get('id', 'ok')})"
    except Exception as e:
        log.error("create_event failed", error=str(e))
        return f"Failed to create event: {e}"


async def _search_events(args: Dict[str, Any], user_context: Any) -> str:
    query = (args.get("query") or "").strip().lower()
    if not query:
        return "No search query provided."

    days_back = int(args.get("days_back", 30))
    days_forward = int(args.get("days_forward", 60))
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days_back)
    end_dt = now + timedelta(days=days_forward)

    provider = _get_provider()
    if provider is None:
        return _not_configured()

    try:
        events = await provider.list_events(
            calendar_id=None,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            max_results=200,
        )
        events = _dedupe_events(events)
    except Exception as e:
        return f"Calendar error: {e}"

    matches = [
        ev for ev in events
        if query in (ev.get("summary") or "").lower()
        or query in (ev.get("description") or "").lower()
        or query in (ev.get("location") or "").lower()
    ]

    if not matches:
        return f"No events matching '{query}' in the last {days_back} / next {days_forward} days."

    lines = [f"Events matching '{query}':\n"]
    for ev in matches:
        s = (ev.get("start") or "")[:16].replace("T", " ")
        lines.append(f"• {s}: {ev.get('summary') or 'Untitled'}")
    return "\n".join(lines)


def _compute_dates(args: Dict[str, Any]) -> str:
    """
    Compute exact dates for a recurring weekday pattern using Python.
    Pattern: '{ordinal} {weekday}' e.g. 'first thursday', 'last friday'.
    """
    import calendar as cal_mod

    pattern = (args.get("pattern") or "").strip().lower()
    count = int(args.get("count") or 6)
    start_str = args.get("start_from")

    ordinal_map = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}
    weekday_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    ordinal = next((v for k, v in ordinal_map.items() if k in pattern), None)
    weekday = next((v for k, v in weekday_map.items() if k in pattern), None)

    if ordinal is None or weekday is None:
        return (
            f"Could not parse pattern '{pattern}'. "
            "Use format: 'first thursday', 'last friday', 'second monday', etc."
        )

    start = datetime.fromisoformat(start_str).date() if start_str else datetime.now(timezone.utc).date()
    # Start from the 1st of start month
    current = start.replace(day=1)
    results = []

    while len(results) < count:
        # Find all matching weekdays in current month
        _, days_in_month = cal_mod.monthrange(current.year, current.month)
        matching = [
            current.replace(day=d)
            for d in range(1, days_in_month + 1)
            if current.replace(day=d).weekday() == weekday
        ]
        if matching:
            pick = matching[ordinal]  # ordinal=-1 gives last
            if pick >= start:
                results.append(pick)
        # Advance to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1, day=1)
        else:
            current = current.replace(month=current.month + 1, day=1)

    lines = [f"Computed dates for '{pattern}' ({count} occurrences):\n"]
    for i, d in enumerate(results, 1):
        lines.append(f"{i}. {d.strftime('%A, %B')} {d.day}, {d.year} ({d.isoformat()})")
    return "\n".join(lines)


# ── Provider factory ──────────────────────────────────────────────────────────

def _get_provider() -> Optional[Any]:
    if settings.google_calendar_enabled:
        try:
            return _GoogleProvider()
        except Exception as e:
            log.debug("Google Calendar unavailable", error=str(e))
    if settings.apple_caldav_enabled:
        try:
            return _AppleProvider()
        except Exception as e:
            log.debug("Apple CalDAV unavailable", error=str(e))
    return None


def _not_configured() -> str:
    return (
        "Calendar integration is not configured. "
        "To enable Google Calendar: set GOOGLE_CALENDAR_ENABLED=true and configure a service account. "
        "To enable Apple Calendar: set APPLE_CALDAV_ENABLED=true with your CalDAV URL and app password."
    )


def _parse_dt(s: str, default: datetime) -> Optional[datetime]:
    if not s:
        return default
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _calendar_matches(requested_calendar_id: str, provider_calendar_name: str, provider_calendar_url: str) -> bool:
    requested = (requested_calendar_id or "").strip()
    if not requested:
        return False
    req = requested.lower()
    name = (provider_calendar_name or "").strip().lower()
    url = (provider_calendar_url or "").strip().lower()
    return req == name or req == url


def _dedupe_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop duplicate events while preserving order."""
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for ev in events or []:
        key = (
            str(ev.get("id") or ""),
            str(ev.get("start") or ""),
            str(ev.get("end") or ""),
            str(ev.get("summary") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    return deduped


@dataclass
class _EventPayload:
    summary: str
    start: str
    end: str
    description: Optional[str] = None
    location: Optional[str] = None
    timezone: str = "UTC"


# ── Google Calendar provider ──────────────────────────────────────────────────

class _GoogleProvider:
    """Google Calendar API. Requires google-api-python-client google-auth."""

    def __init__(self):
        try:
            import googleapiclient  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "google-api-python-client not installed. "
                "Run: pip install google-api-python-client google-auth"
            )
        if not settings.google_calendar_enabled:
            raise RuntimeError("GOOGLE_CALENDAR_ENABLED=false")
        self._service = None
        self._lock = asyncio.Lock()

    def default_calendar_id(self) -> str:
        return settings.google_calendar_default_id or "primary"

    async def _get_service(self):
        async with self._lock:
            if self._service:
                return self._service
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            sa_file = settings.google_calendar_service_account_file
            if not sa_file:
                raise RuntimeError("GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE not set")

            scopes = ["https://www.googleapis.com/auth/calendar"]
            creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
            if settings.google_calendar_delegated_user:
                creds = creds.with_subject(settings.google_calendar_delegated_user)

            loop = asyncio.get_running_loop()
            self._service = await loop.run_in_executor(
                None,
                lambda: build("calendar", "v3", credentials=creds, cache_discovery=False),
            )
            return self._service

    async def list_events(
        self, calendar_id: Optional[str], start: str, end: str, max_results: int
    ) -> List[Dict[str, Any]]:
        service = await self._get_service()
        loop = asyncio.get_running_loop()
        effective_calendar_id = calendar_id or self.default_calendar_id()

        def _fetch():
            resp = (
                service.events()
                .list(
                    calendarId=effective_calendar_id,
                    timeMin=start,
                    timeMax=end,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            results = []
            for item in resp.get("items", []):
                s = item.get("start", {})
                e = item.get("end", {})
                results.append({
                    "id": item.get("id"),
                    "summary": item.get("summary"),
                    "description": item.get("description"),
                    "location": item.get("location"),
                    "start": s.get("dateTime") or s.get("date"),
                    "end": e.get("dateTime") or e.get("date"),
                })
            return results

        return await loop.run_in_executor(None, _fetch)

    async def create_event(self, calendar_id: str, payload: _EventPayload) -> Dict[str, Any]:
        service = await self._get_service()
        loop = asyncio.get_running_loop()
        body = {
            "summary": payload.summary,
            "description": payload.description,
            "location": payload.location,
            "start": {"dateTime": payload.start, "timeZone": payload.timezone},
            "end": {"dateTime": payload.end, "timeZone": payload.timezone},
        }

        def _create():
            return (
                service.events()
                .insert(calendarId=calendar_id, body=body, sendUpdates="all")
                .execute()
            )

        result = await loop.run_in_executor(None, _create)
        return {"id": result.get("id"), "status": result.get("status")}


# ── Apple CalDAV provider ─────────────────────────────────────────────────────

class _AppleProvider:
    """Apple Calendar via CalDAV. Requires caldav icalendar."""

    def __init__(self):
        try:
            import caldav  # noqa: F401
        except ImportError:
            raise RuntimeError("caldav not installed. Run: pip install caldav icalendar")
        if not settings.apple_caldav_enabled:
            raise RuntimeError("APPLE_CALDAV_ENABLED=false")
        if not settings.apple_caldav_url or not settings.apple_caldav_username:
            raise RuntimeError("APPLE_CALDAV_URL and APPLE_CALDAV_USERNAME required")

        import caldav
        self._client = caldav.DAVClient(
            settings.apple_caldav_url,
            username=settings.apple_caldav_username,
            password=settings.apple_caldav_app_password,
        )
        self._principal = None
        self._lock = asyncio.Lock()

    def default_calendar_id(self) -> str:
        return settings.apple_caldav_default_calendar or "home"

    async def _get_principal(self):
        async with self._lock:
            if self._principal:
                return self._principal
            loop = asyncio.get_running_loop()
            self._principal = await loop.run_in_executor(None, self._client.principal)
            return self._principal

    async def list_events(
        self, calendar_id: Optional[str], start: str, end: str, max_results: int
    ) -> List[Dict[str, Any]]:
        principal = await self._get_principal()
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        loop = asyncio.get_running_loop()
        requested_calendar = (calendar_id or "").strip()

        def _fetch():
            calendars = list(principal.calendars())
            available_names = [
                (getattr(cal, "name", None) or str(cal.url))
                for cal in calendars
            ]

            selected = []
            if requested_calendar:
                for cal in calendars:
                    name = getattr(cal, "name", None) or str(cal.url)
                    if _calendar_matches(requested_calendar, name, str(cal.url)):
                        selected.append(cal)
                if not selected:
                    raise RuntimeError(
                        f"Calendar '{requested_calendar}' not found. Available calendars: {', '.join(available_names)}"
                    )
            else:
                selected = calendars

            items = []
            for cal in selected:
                for ev in cal.date_search(start_dt, end_dt):
                    comp = ev.icalendar_component
                    for vevent in _collect_vevent_components(comp):
                        s = vevent.get("dtstart")
                        e = vevent.get("dtend")
                        items.append({
                            "id": str(vevent.get("uid", "")),
                            "summary": str(vevent.get("summary", "")),
                            "description": str(vevent.get("description", "")) if vevent.get("description") else None,
                            "location": str(vevent.get("location", "")) if vevent.get("location") else None,
                            "start": s.dt.isoformat() if s and getattr(s, "dt", None) else None,
                            "end": e.dt.isoformat() if e and getattr(e, "dt", None) else None,
                        })

            def _sort_key(item: Dict[str, Any]) -> str:
                return str(item.get("start") or "")

            items.sort(key=_sort_key)
            return items[:max_results]

        return await loop.run_in_executor(None, _fetch)

    async def create_event(self, calendar_id: str, payload: _EventPayload) -> Dict[str, Any]:
        import uuid
        from icalendar import Calendar as ICalendar, Event as ICalEvent

        principal = await self._get_principal()
        start_dt = datetime.fromisoformat(payload.start)
        end_dt = datetime.fromisoformat(payload.end)
        loop = asyncio.get_running_loop()

        def _create():
            for cal in principal.calendars():
                name = getattr(cal, "name", None) or str(cal.url)
                if _calendar_matches(calendar_id, name, str(cal.url)):
                    cal_obj = ICalendar()
                    cal_obj.add("prodid", "-//FruitcakeAI//")
                    cal_obj.add("version", "2.0")
                    ev = ICalEvent()
                    uid = str(uuid.uuid4())
                    ev.add("uid", uid)
                    ev.add("summary", payload.summary)
                    ev.add("dtstart", start_dt)
                    ev.add("dtend", end_dt)
                    if payload.description:
                        ev.add("description", payload.description)
                    if payload.location:
                        ev.add("location", payload.location)
                    cal_obj.add_component(ev)
                    cal.save_event(cal_obj.to_ical().decode("utf-8"))
                    return {"id": uid, "status": "created"}
            return {"id": "", "status": "calendar_not_found"}

        return await loop.run_in_executor(None, _create)


def _collect_vevent_components(component: Any) -> List[Any]:
    """Return VEVENT objects whether the source is VEVENT or VCALENDAR."""
    if component is None:
        return []

    if getattr(component, "name", None) == "VEVENT":
        return [component]

    vevents: List[Any] = []
    for sub in getattr(component, "subcomponents", []) or []:
        if getattr(sub, "name", None) == "VEVENT":
            vevents.append(sub)
    return vevents
