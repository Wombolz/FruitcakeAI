"""
FruitcakeAI v5 — Agent Registry Loader

Loads broad category definitions plus Fruitcake-specific presets from
config/agents.yaml.

Categories describe how an agent works.
Presets describe the assigned Fruitcake job built on top of a category.
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
class FruitcakeAgentCategory:
    category_id: str
    display_name: str
    when_to_use: str
    execution_mode: str = "task"
    background: bool = False
    memory_scope: str = "user"
    behavior_instructions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FruitcakeAgentPreset:
    preset_id: str
    category_id: str
    category_display_name: str
    display_name: str
    when_to_use: str
    execution_mode: str = "task"
    background: bool = False
    memory_scope: str = "user"
    persona_compatibility: Optional[str] = None
    required_context_sources: tuple[str, ...] = ()
    behavior_instructions: tuple[str, ...] = ()
    output_contract: tuple[str, ...] = ()
    hidden_from_picker: bool = False


@dataclass(frozen=True)
class FruitcakeAgentRegistry:
    categories: Dict[str, FruitcakeAgentCategory]
    presets: Dict[str, FruitcakeAgentPreset]


_registry: Optional[FruitcakeAgentRegistry] = None


def _normalize_category(name: str, raw: Dict[str, Any]) -> FruitcakeAgentCategory:
    return FruitcakeAgentCategory(
        category_id=name,
        display_name=str(raw.get("display_name") or name),
        when_to_use=str(raw.get("when_to_use") or "").strip(),
        execution_mode=str(raw.get("execution_mode") or "task").strip() or "task",
        background=bool(raw.get("background", False)),
        memory_scope=str(raw.get("memory_scope") or "user").strip() or "user",
        behavior_instructions=tuple(
            str(item).strip()
            for item in (raw.get("behavior_instructions") or [])
            if str(item).strip()
        ),
    )


def _normalize_preset(
    name: str,
    raw: Dict[str, Any],
    *,
    categories: Dict[str, FruitcakeAgentCategory],
) -> FruitcakeAgentPreset:
    category_id = str(raw.get("category") or "").strip()
    if not category_id:
        raise ValueError(f"Agent preset '{name}' is missing required category.")
    category = categories.get(category_id)
    if category is None:
        raise ValueError(f"Agent preset '{name}' references unknown category '{category_id}'.")

    preset_behavior = tuple(
        str(item).strip()
        for item in (raw.get("behavior_instructions") or [])
        if str(item).strip()
    )
    preset_output = tuple(
        str(item).strip()
        for item in (raw.get("output_contract") or [])
        if str(item).strip()
    )

    return FruitcakeAgentPreset(
        preset_id=name,
        category_id=category.category_id,
        category_display_name=category.display_name,
        display_name=str(raw.get("display_name") or name),
        when_to_use=str(raw.get("when_to_use") or category.when_to_use).strip(),
        execution_mode=str(raw.get("execution_mode") or category.execution_mode).strip() or category.execution_mode,
        background=bool(raw.get("background", category.background)),
        memory_scope=str(raw.get("memory_scope") or category.memory_scope).strip() or category.memory_scope,
        persona_compatibility=(str(raw.get("persona_compatibility") or "").strip() or None),
        required_context_sources=tuple(
            str(item).strip()
            for item in (raw.get("required_context_sources") or [])
            if str(item).strip()
        ),
        behavior_instructions=category.behavior_instructions + preset_behavior,
        output_contract=preset_output,
        hidden_from_picker=bool(raw.get("hidden_from_picker", False)),
    )


def _load_registry() -> FruitcakeAgentRegistry:
    global _registry
    if _registry is None:
        if not _CONFIG_PATH.exists():
            log.warning("agents.yaml not found — using empty registry", path=str(_CONFIG_PATH))
            _registry = FruitcakeAgentRegistry(categories={}, presets={})
            return _registry

        with open(_CONFIG_PATH) as f:
            raw = yaml.safe_load(f) or {}

        categories_raw = raw.get("categories", {}) or {}
        presets_raw = raw.get("presets", {}) or {}

        categories = {
            str(name): _normalize_category(str(name), data or {})
            for name, data in categories_raw.items()
        }
        presets = {
            str(name): _normalize_preset(str(name), data or {}, categories=categories)
            for name, data in presets_raw.items()
        }
        _registry = FruitcakeAgentRegistry(categories=categories, presets=presets)
        log.info(
            "Agent registry loaded",
            categories=list(categories.keys()),
            presets=list(presets.keys()),
        )
    return _registry


def get_agent_category(name: str) -> Optional[FruitcakeAgentCategory]:
    key = str(name or "").strip()
    if not key:
        return None
    return _load_registry().categories.get(key)


def list_agent_categories() -> Dict[str, FruitcakeAgentCategory]:
    return dict(_load_registry().categories)


def get_agent_preset(name: str) -> Optional[FruitcakeAgentPreset]:
    key = str(name or "").strip()
    if not key:
        return None
    return _load_registry().presets.get(key)


def list_agent_presets() -> Dict[str, FruitcakeAgentPreset]:
    return dict(_load_registry().presets)


def agent_preset_exists(name: str) -> bool:
    return get_agent_preset(name) is not None
