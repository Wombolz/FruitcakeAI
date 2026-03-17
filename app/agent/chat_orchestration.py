from __future__ import annotations

from typing import Any


def build_orchestrated_chat_history(
    history: list[dict[str, Any]],
    *,
    enabled: bool,
    max_steps: int = 3,
) -> list[dict[str, Any]]:
    """
    Add a per-turn orchestration overlay for complex chat requests.

    The overlay is injected as a temporary system message right before the
    latest user message so chat stays non-task UX while still benefiting from
    a short internal plan + tool-grounded synthesis.
    """
    if not enabled:
        return list(history)

    steps = min(max(2, int(max_steps)), 3)
    overlay = {
        "role": "system",
        "content": (
            "Complex chat execution policy for this turn:\n"
            f"1. Create an internal micro-plan with {steps} steps maximum.\n"
            "2. Use tools only when they materially improve factual quality.\n"
            "3. Return one final answer in normal chat style.\n"
            "Rules:\n"
            "- Keep planning internal; do not expose chain-of-thought.\n"
            "- Do not create tasks or mention task orchestration.\n"
            "- If using links, provide direct URLs only.\n"
            "- If evidence is insufficient, say what is missing clearly."
        ),
    }

    copied = list(history)
    if copied and copied[-1].get("role") == "user":
        return copied[:-1] + [overlay, copied[-1]]
    return copied + [overlay]
