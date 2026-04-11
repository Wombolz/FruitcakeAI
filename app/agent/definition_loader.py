"""
FruitcakeAI v5 — Agent Definition Loader
Loads built-in Fruitcake agent definitions from config/agents.yaml.

This is intentionally separate from personas:
- agent definitions describe specialist operating contracts
- personas remain user/assistant identity + tone + access defaults
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
import yaml

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "agents.yaml"


@dataclass(frozen=True)
class FruitcakeAgentDefinition:
    agent_type: str
    display_name: str
    when_to_use: str
    execution_mode: str = "task"
    background: bool = False
    memory_scope: str = "user"
    persona_compatibility: Optional[str] = None
    required_context_sources: tuple[str, ...] = ()
    behavior_instructions: tuple[str, ...] = ()
    output_contract: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    hidden_from_picker: bool = False


_agents: Optional[Dict[str, FruitcakeAgentDefinition]] = None


def _normalize(name: str, raw: Dict[str, Any]) -> FruitcakeAgentDefinition:
    return FruitcakeAgentDefinition(
        agent_type=name,
        display_name=str(raw.get("display_name") or name),
        when_to_use=str(raw.get("when_to_use") or "").strip(),
        execution_mode=str(raw.get("execution_mode") or "task").strip() or "task",
        background=bool(raw.get("background", False)),
        memory_scope=str(raw.get("memory_scope") or "user").strip() or "user",
        persona_compatibility=(str(raw.get("persona_compatibility") or "").strip() or None),
        required_context_sources=tuple(
            str(item).strip()
            for item in (raw.get("required_context_sources") or [])
            if str(item).strip()
        ),
        behavior_instructions=tuple(
            str(item).strip()
            for item in (raw.get("behavior_instructions") or [])
            if str(item).strip()
        ),
        output_contract=tuple(
            str(item).strip()
            for item in (raw.get("output_contract") or [])
            if str(item).strip()
        ),
        aliases=tuple(
            str(item).strip()
            for item in (raw.get("aliases") or [])
            if str(item).strip()
        ),
        hidden_from_picker=bool(raw.get("hidden_from_picker", False)),
    )


def _load() -> Dict[str, FruitcakeAgentDefinition]:
    global _agents
    if _agents is None:
        if not _CONFIG_PATH.exists():
            log.warning("agents.yaml not found — using empty agent definition set", path=str(_CONFIG_PATH))
            _agents = {}
        else:
            with open(_CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
            agents_raw = raw.get("agents", {}) or {}
            canonical_agents = {
                str(name): _normalize(str(name), data or {})
                for name, data in agents_raw.items()
            }
            loaded_agents: Dict[str, FruitcakeAgentDefinition] = dict(canonical_agents)
            for definition in canonical_agents.values():
                for alias in definition.aliases:
                    if alias and alias not in loaded_agents:
                        loaded_agents[alias] = definition
            _agents = loaded_agents
            log.info("Agent definitions loaded", names=list(_agents.keys()))
    return _agents


def get_agent_definition(name: str) -> Optional[FruitcakeAgentDefinition]:
    key = str(name or "").strip()
    if not key:
        return None
    return _load().get(key)


def list_agent_definitions() -> Dict[str, FruitcakeAgentDefinition]:
    return {
        name: definition
        for name, definition in _load().items()
        if definition.agent_type == name
    }


def agent_definition_exists(name: str) -> bool:
    return get_agent_definition(name) is not None
