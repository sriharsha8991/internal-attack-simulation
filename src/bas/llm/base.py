"""Shared types and the LLMProvider Protocol.

Every provider implementation MUST:
  - implement all four methods (`chat`, `generate_structured`,
    `classify_grounding_depth`, `research`),
  - increment its internal counter on every grounded call and raise
    `GroundingBudgetExceeded` once the per-run cap is hit (fail-closed),
  - expose `provider_id` like `"gemini:gemini-3.5-flash"` so artefacts can
    record provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

GroundingDepth = Literal["skip", "light", "deep"]
"""How much external evidence to fetch before answering.

    skip    no web search at all (deterministic, cached knowledge)
    light   one grounded round-trip per prompt (default)
    deep    multi-step grounded research (citations required)
"""


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class ResearchResult:
    text: str
    citations: list[str] = field(default_factory=list)
    depth: GroundingDepth = "skip"
    queries: list[str] = field(default_factory=list)


class LLMProviderError(RuntimeError):
    """Wraps any provider-side failure (network, quota, schema)."""


class GroundingBudgetExceeded(LLMProviderError):
    """Raised when a grounded call would exceed `max_grounded_calls_per_run`.

    The orchestrator catches this and either falls back to ungrounded answers
    or aborts the run, depending on policy.
    """


T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMProvider(Protocol):
    """Provider-agnostic surface used by every agent."""

    provider_id: str

    # ---- raw chat -----------------------------------------------------------

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        grounding: GroundingDepth = "skip",
        temperature: float | None = None,
    ) -> str: ...

    # ---- structured output --------------------------------------------------

    def generate_structured(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        grounding: GroundingDepth = "skip",
        temperature: float | None = None,
    ) -> T: ...

    # ---- grounding routing --------------------------------------------------

    def classify_grounding_depth(self, prompt: str) -> GroundingDepth:
        """Cheap pre-classifier that decides whether to spend a grounded call."""
        ...

    def research(
        self,
        query: str,
        *,
        depth: GroundingDepth = "light",
    ) -> ResearchResult: ...

    # ---- bookkeeping --------------------------------------------------------

    @property
    def grounded_calls_made(self) -> int: ...
