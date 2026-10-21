"""Account policy ceilings for selectable personalities. No model decisions."""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.persona_loader import get_persona, persona_exists


class PersonaPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PersonaPolicy:
    library_scopes: tuple[str, ...]
    calendar_access: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    content_filter: str


def _strings(values) -> list[str]:
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, str) or not value or value != value.strip() for value in values
    ):
        raise PersonaPolicyError("Persona access policy must contain lists of names.")
    return list(dict.fromkeys(values))


def _narrow(values: list[str], definition: dict, field: str) -> list[str]:
    if field not in definition:
        return values
    allowed = set(_strings(definition[field]))
    return [value for value in values if value in allowed]


def resolve_persona_policy(user, selected_name: str) -> PersonaPolicy:
    # user.persona is currently administrator-managed. Keep its legacy policy as
    # the baseline; future user-owned personality defaults must not replace it.
    baseline_name = user.persona or "family_assistant"
    for name in (baseline_name, selected_name):
        if not persona_exists(name):
            raise PersonaPolicyError(f"Unknown persona '{name}'.")
    baseline, selected = get_persona(baseline_name), get_persona(selected_name)
    definitions = [baseline]
    if user.role == "restricted":
        if not persona_exists("restricted_assistant"):
            raise PersonaPolicyError("Restricted account policy is unavailable.")
        definitions.append(get_persona("restricted_assistant"))
    definitions.append(selected)
    library = _strings(user.library_scopes)
    calendars = _strings(user.calendar_access)
    blocked: list[str] = []
    strict = user.role == "restricted"
    for definition in definitions:
        library = _narrow(library, definition, "library_scopes")
        calendars = _narrow(calendars, definition, "calendar_access")
        blocked.extend(_strings(definition.get("blocked_tools", [])))
        strict |= definition.get("content_filter") == "strict"
    if not library:
        blocked.extend(["search_library", "summarize_document", "list_library_documents"])
    if not calendars:
        blocked.extend(["list_events", "search_events", "create_event", "delete_event"])
    return PersonaPolicy(tuple(library), tuple(calendars), tuple(dict.fromkeys(blocked)), "strict" if strict else "")
