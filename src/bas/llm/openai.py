"""OpenAI provider stub (M2+). Raises on instantiation."""

from __future__ import annotations

from .base import LLMProviderError


class OpenAIProvider:
    def __init__(self, *_: object, **__: object) -> None:
        raise LLMProviderError(
            "OpenAIProvider is not implemented yet (M2). "
            "Switch llm.provider to 'gemini' in config/config.yaml for M1."
        )
