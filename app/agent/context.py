"""
FruitcakeAI v5 — UserContext
Builds the agent system prompt from the current user's role, persona, and scopes.

The LLM sees this context on every request — it's the primary access control mechanism.
Persona config (blocked_tools, tone, scopes) is loaded from config/personas.yaml.
Current date/time is injected on every call so the LLM can answer date-dependent
questions (age calculations, scheduling) correctly without a tool round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.db.models import User


@dataclass
class UserContext:
    user_id: int
    username: str
    role: str                          # admin | parent | restricted | guest
    persona: str = "family_assistant"
    persona_description: str = ""
    persona_tone: str = "helpful"
    behavior_instructions: List[str] = field(default_factory=list)
    library_scopes: List[str] = field(default_factory=lambda: ["family_docs"])
    calendar_access: List[str] = field(default_factory=list)
    blocked_tools: List[str] = field(default_factory=list)
    allowed_tool_cap: List[str] = field(default_factory=list)
    skill_prompt_additions: List[str] = field(default_factory=list)
    skill_granted_tools: List[str] = field(default_factory=list)
    active_skill_slugs: List[str] = field(default_factory=list)
    skill_selection_mode: str = ""
    skill_injection_details: List[dict] = field(default_factory=list)
    content_filter: str = ""           # "" | "strict"
    session_id: Optional[int] = None   # set by chat.py for audit logging
    task_id: Optional[int] = None      # set by task runner for task-scoped tool state
    timezone: Optional[str] = None     # IANA tz string, e.g. "America/Chicago"

    @classmethod
    def from_user(
        cls,
        user: "User",
        persona_name: Optional[str] = None,
    ) -> "UserContext":
        """
        Build a UserContext from a SQLAlchemy User model.

        persona_name overrides user.persona (used for mid-session /persona switches).
        Blocked tools, tone, and scopes are loaded from config/personas.yaml.
        """
        from app.agent.persona_loader import get_persona

        name = persona_name or user.persona or "family_assistant"
        pc = get_persona(name)

        # Persona-defined scopes take precedence; fall back to user record scopes.
        library_scopes = pc.get("library_scopes") or user.library_scopes
        calendar_access = pc.get("calendar_access") or user.calendar_access

        return cls(
            user_id=user.id,
            username=user.username,
            role=user.role,
            persona=name,
            persona_description=pc.get("description", ""),
            persona_tone=pc.get("tone", "helpful"),
            behavior_instructions=[str(item).strip() for item in (pc.get("behavior_instructions") or []) if str(item).strip()],
            library_scopes=library_scopes,
            calendar_access=calendar_access,
            blocked_tools=pc.get("blocked_tools", []),
            content_filter=pc.get("content_filter", ""),
            timezone=getattr(user, "active_hours_tz", None) or None,
        )

    def _current_time_str(self) -> str:
        """Return a human-readable current date/time string, localized if a timezone is set."""
        now_utc = datetime.now(_tz.utc)
        if self.timezone:
            try:
                from zoneinfo import ZoneInfo
                now_local = now_utc.astimezone(ZoneInfo(self.timezone))
                return now_local.strftime("%A, %B %d, %Y %I:%M %p %Z")
            except Exception:
                pass
        return now_utc.strftime("%A, %B %d, %Y %H:%M UTC")

    def to_system_prompt(self) -> str:
        lines = [
            "You are FruitcakeAI, a private, local-first AI assistant for a family household.",
            "",
            f"Current date and time: {self._current_time_str()}",
            f"Current user: {self.username} (role: {self.role})",
            f"Active persona: {self.persona}",
        ]

        if self.persona_description:
            lines.append(f"Persona: {self.persona_description}")
        if self.persona_tone:
            lines.append(f"Tone: be {self.persona_tone}")
        if self.behavior_instructions:
            lines += ["", "Behavior guidance:"]
            lines.extend(f"- {instruction}" for instruction in self.behavior_instructions)

        lines += [
            "",
            "Rules:",
            "- If a sandboxed tool is available for the requested action, use it and report the tool result rather than refusing from general model policy.",
            "- Do not claim a tool is unavailable when it is present in the current tool list.",
            "- If you just created a task in this conversation and the user asks to change it or run it, prefer updating or running that existing task instead of creating a second similar task.",
            "- For a new task request, use propose_task_draft first and let the user review the draft in the task editor before anything is persisted. Use create_task only after that draft has been reviewed and the user has clearly confirmed the details, unless they explicitly ask you to save immediately.",
            "- When a high-confidence task recipe fits, prefer structured task details over copying the user's wording verbatim: choose a clean title, capture key parameters like topic or location separately, and save the stronger normalized task shape.",
            "- Never delete a calendar event unless the user explicitly confirms the deletion in the conversation first. List or search the event, identify the exact event id, ask for confirmation if it has not already been given, then use delete_event.",
            "- When you learn a stable user fact, durable preference, recurring household procedure, or meaningful near-term event that should persist beyond this turn, use create_memory once with a clear self-contained statement.",
            "- Do not create memories for trivial one-off chatter, temporary reasoning steps, or facts already captured in the visible memory context.",
            "- Always cite the source document name when using search_library results.",
            f"- Only surface documents within the user's permitted scopes: {self.library_scopes}.",
            "- Be helpful and privacy-conscious.",
            "- If you don't find relevant information in the library, say so clearly.",
            "- For shell requests, use shell_exec when it is available for local workspace commands or explicit shell-policy tests. Let the shell tool enforce what is blocked, timed out, or refused, then report that tool result clearly.",
        ]

        if self.content_filter == "strict":
            lines += [
                "",
                "Content restrictions (STRICT — restricted-access user):",
                "- Avoid all adult topics, violence, or inappropriate content.",
                "- Keep explanations simple and age-appropriate.",
                "- Gently redirect if asked about blocked topics.",
            ]

        if self.blocked_tools:
            lines += [
                "",
                f"The following tools are NOT available in this persona and must not be used: "
                f"{', '.join(self.blocked_tools)}.",
            ]

        if self.skill_prompt_additions:
            lines += ["", "Active skills:"]
            for addition in self.skill_prompt_additions:
                lines += ["---", addition.strip()]
            lines.append("---")
        if self.skill_granted_tools:
            lines += [
                "",
                "Relevant tool guidance from active skills:",
                f"- Prefer these tools when they are available and appropriate: {', '.join(self.skill_granted_tools)}.",
            ]

        return "\n".join(lines)
