"""LLM provider abstraction.

Public surface:
    from bas.llm import get_provider, LLMProvider, GroundingDepth, ResearchResult

Implementations:
    gemini      — Google google-genai SDK with `google_search` grounding tool (M1)
    anthropic   — stub (M2+)
    openai      — stub (M2+)
"""

from .base import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    GroundingBudgetExceeded,
    GroundingDepth,
    ResearchResult,
)
from .factory import get_provider

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "GroundingBudgetExceeded",
    "GroundingDepth",
    "ResearchResult",
    "get_provider",
]
