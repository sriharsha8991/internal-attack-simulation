"""Provider factory — builds an `LLMProvider` straight from the central `LlmConfig`.

To swap providers, change `llm.provider` in config/config.yaml. No code changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import LLMProvider, LLMProviderError

if TYPE_CHECKING:
    from ..config import LlmConfig


def get_provider(cfg: "LlmConfig") -> LLMProvider:
    name = cfg.provider.lower()
    if name == "gemini":
        from .gemini import build_from_env

        return build_from_env(
            model=cfg.model,
            classifier_model=cfg.classifier_model,
            api_key_env=cfg.api_key_env,
            temperature=cfg.temperature,
            max_grounded_calls_per_run=cfg.grounding.max_grounded_calls_per_run,
            thinking_level=cfg.thinking_level,
        )
    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider()
    if name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider()
    raise LLMProviderError(f"unknown llm.provider: {cfg.provider!r}")
