from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=32)
def load_profile_spec_text(name: str) -> str:
    spec_name = (name or "").strip().lower()
    if not spec_name:
        raise ValueError("Profile spec name is required")

    path = Path(__file__).resolve().parent / "specs" / f"{spec_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Profile spec file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
