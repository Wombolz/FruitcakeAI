from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class PiModelProfile:
    """Operator-selected model connection for an isolated Pi process."""

    provider: str
    model: str
    base_url: str
    api_key: str = field(default="local", repr=False)
    context_window: int = 32768
    max_tokens: int = 4096
    reasoning: bool = False

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("Coding model endpoint must be an HTTP(S) URL")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("Coding model endpoint cannot contain credentials, query, or fragment")
        if not self.provider or not self.model or self.context_window <= 0 or self.max_tokens <= 0:
            raise ValueError("Coding model profile requires a provider, model, and positive token limits")

    def models_document(self) -> dict:
        return {
            "providers": {
                self.provider: {
                    "baseUrl": self.base_url,
                    "api": "openai-completions",
                    "apiKey": "$FRUITCAKE_PI_MODEL_KEY",
                    "models": [{
                        "id": self.model,
                        "name": self.model,
                        "reasoning": self.reasoning,
                        "input": ["text"],
                        "contextWindow": self.context_window,
                        "maxTokens": self.max_tokens,
                    }],
                }
            }
        }
