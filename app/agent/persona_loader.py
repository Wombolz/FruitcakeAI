"""
FruitcakeAI v5 — Persona Loader
Loads and caches persona definitions from config/personas.yaml.

The cache is populated on first access and held for the process lifetime.
A server restart is required to pick up persona config changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import structlog
import yaml

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "personas.yaml"

_personas: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _personas
    if _personas is None:
        if not _CONFIG_PATH.exists():
            log.warning("personas.yaml not found — using empty persona set", path=str(_CONFIG_PATH))
            _personas = {}
        else:
            with open(_CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
            _personas = raw.get("personas", {})
            log.info("Personas loaded", names=list(_personas.keys()))
    return _personas


def get_persona(name: str) -> Dict[str, Any]:
    """Return the persona config dict for *name*, or {} if not found."""
    return _load().get(name, {})


def list_personas() -> Dict[str, Any]:
    """Return all persona definitions keyed by name."""
    return dict(_load())


def persona_exists(name: str) -> bool:
    return name in _load()
