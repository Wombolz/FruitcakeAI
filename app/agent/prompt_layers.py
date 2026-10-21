"""Source-labelled prompt composition; not an authorization mechanism."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


PromptSource = Literal["persona_identity", "session_context", "persona_behavior",
                       "runtime_contract", "access_restrictions"]


@dataclass(frozen=True)
class PromptLayer:
    source: PromptSource
    lines: tuple[str, ...]

    @property
    def content(self) -> str:
        return "\n".join(self.lines)


def render_prompt_layers(layers: Iterable[PromptLayer]) -> str:
    # Preserve legacy separators and ordering while retaining source provenance.
    return "\n".join(line for layer in layers for line in layer.lines)
