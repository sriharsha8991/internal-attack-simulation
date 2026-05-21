"""Gemini provider — google-genai SDK with the built-in `google_search` tool.

Grounding mapping:
    skip    no tools; deterministic
    light   tools=[google_search] enabled, single generate_content() call
    deep    tools=[google_search] + a higher reasoning budget; still one call
            (multi-turn deep research is M2 — keep M1 simple).

Per-run grounded-call counter is enforced fail-closed before every grounded
request; once `max_grounded_calls_per_run` is hit, `GroundingBudgetExceeded`
is raised and the orchestrator decides what to do.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .base import (
    GroundingBudgetExceeded,
    GroundingDepth,
    LLMMessage,
    LLMProviderError,
    ResearchResult,
)

T = TypeVar("T", bound=BaseModel)

# Heuristic prompt for the classifier model for web evidence grounding.
_DEPTH_CLASSIFIER_PROMPT = (
    "Classify how much fresh web evidence is needed to answer the following request. "
    "Reply with exactly one lowercase token: skip, light, or deep.\n"
    "  skip  = answer from general knowledge alone (commands, syntax, well-known facts)\n"
    "  light = a single web lookup would substantially improve correctness\n"
    "  deep  = multiple authoritative sources required (CVE details, vendor-specific TTPs)\n\n"
    "Request:\n{prompt}\n\nAnswer:"
)


# Keys in a JSON Schema that the Gemini Developer API rejects. They're either
# Pydantic decoration (`title`, `$defs`) or Vertex-Enterprise-only features
# (`additionalProperties`).
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"additionalProperties", "$defs", "definitions", "title", "default", "examples"}
)


def _sanitize_schema_for_gemini(node: Any) -> Any:
    """Recursively strip keys Gemini Developer API rejects and inline ``$ref`` jumps.

    Pydantic emits ``$defs`` + ``$ref`` for nested models; the SDK can handle
    refs, so we keep those but copy ``$defs`` to a private slot used during
    inlining, then drop the top-level copy on the way back up.
    """
    if isinstance(node, dict):
        # First pass: harvest definitions so we can inline refs.
        defs = node.get("$defs") or node.get("definitions") or {}

        def _resolve(sub: Any) -> Any:
            if isinstance(sub, dict):
                # Inline `{"$ref": "#/$defs/Name"}` -> the actual subschema.
                ref = sub.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/"):
                    parts = ref.lstrip("#/").split("/")
                    target: Any = {"$defs": defs, "definitions": defs}
                    for p in parts:
                        target = target.get(p, {}) if isinstance(target, dict) else {}
                    return _resolve(target)
                cleaned: dict[str, Any] = {}
                for k, v in sub.items():
                    if k in _GEMINI_UNSUPPORTED_SCHEMA_KEYS:
                        continue
                    cleaned[k] = _resolve(v)
                # An object schema with no properties confuses Gemini; replace
                # with a permissive `type=object` so the LLM can emit `{}`.
                if cleaned.get("type") == "object" and "properties" not in cleaned:
                    cleaned["properties"] = {}
                return cleaned
            if isinstance(sub, list):
                return [_resolve(x) for x in sub]
            return sub

        return _resolve(node)
    if isinstance(node, list):
        return [_sanitize_schema_for_gemini(x) for x in node]
    return node


class GeminiProvider:
    """Implements the `LLMProvider` Protocol."""

    def __init__(
        self,
        *,
        model: str,
        classifier_model: str,
        api_key: str,
        temperature: float = 0.2,
        max_grounded_calls_per_run: int = 40,
    ) -> None:
        # Import lazily so the module imports cleanly even when google-genai
        # is not installed (e.g. in unit tests using stub providers).
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - exercised when extras missing
            raise LLMProviderError(
                "google-genai is not installed; add it to the env to use GeminiProvider"
            ) from e

        if not api_key:
            raise LLMProviderError("GeminiProvider requires a non-empty api_key")

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._classifier_model = classifier_model
        self._temperature = temperature
        self._max_grounded = max_grounded_calls_per_run
        self._grounded_count = 0

    # ---- introspection ------------------------------------------------------

    @property
    def provider_id(self) -> str:
        return f"gemini:{self._model}"

    @property
    def grounded_calls_made(self) -> int:
        return self._grounded_count

    # ---- internals ----------------------------------------------------------

    def _budget_check(self) -> None:
        if self._grounded_count >= self._max_grounded:
            raise GroundingBudgetExceeded(
                f"grounded-call budget exceeded ({self._max_grounded}); failing closed"
            )

    def _build_config(
        self,
        *,
        grounding: GroundingDepth,
        temperature: float | None,
        response_schema: type[BaseModel] | None = None,
    ) -> Any:
        types = self._genai.types
        tools: list[Any] = []
        if grounding != "skip":
            self._budget_check()
            tools = [types.Tool(google_search=types.GoogleSearch())]

        kwargs: dict[str, Any] = {
            "temperature": self._temperature if temperature is None else temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if response_schema is not None:
            # Structured-output mode is mutually exclusive with tools in the SDK;
            # callers should pass grounding="skip" when requesting JSON.
            kwargs["response_mime_type"] = "application/json"
           
            kwargs["response_schema"] = _sanitize_schema_for_gemini(
                response_schema.model_json_schema()
            )
        return types.GenerateContentConfig(**kwargs)

    def _to_contents(self, messages: list[LLMMessage]) -> list[Any]:
        types = self._genai.types
        out: list[Any] = []
        for m in messages:
            role = "user" if m.role in ("user", "system") else "model"
            out.append(types.Content(role=role, parts=[types.Part.from_text(text=m.content)]))
        return out

    def _generate(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        grounding: GroundingDepth,
        temperature: float | None,
        response_schema: type[BaseModel] | None = None,
    ) -> Any:
        config = self._build_config(
            grounding=grounding,
            temperature=temperature,
            response_schema=response_schema,
        )
        try:
            resp = self._client.models.generate_content(
                model=model,
                contents=self._to_contents(messages),
                config=config,
            )
        except Exception as e:  # pragma: no cover - SDK-level failures
            raise LLMProviderError(f"gemini generate_content failed: {e}") from e
        if grounding != "skip":
            self._grounded_count += 1
        return resp

    @staticmethod
    def _extract_citations(resp: Any) -> list[str]:
        citations: list[str] = []
        try:
            for cand in getattr(resp, "candidates", None) or []:
                meta = getattr(cand, "grounding_metadata", None)
                if not meta:
                    continue
                for chunk in getattr(meta, "grounding_chunks", None) or []:
                    web = getattr(chunk, "web", None)
                    uri = getattr(web, "uri", None) if web else None
                    if uri:
                        citations.append(uri)
        except Exception:
            pass
        # de-dup while preserving order
        seen: set[str] = set()
        deduped = []
        for c in citations:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        return deduped

    @staticmethod
    def _extract_queries(resp: Any) -> list[str]:
        try:
            for cand in getattr(resp, "candidates", None) or []:
                meta = getattr(cand, "grounding_metadata", None)
                if meta and getattr(meta, "web_search_queries", None):
                    return list(meta.web_search_queries)
        except Exception:
            pass
        return []

    # ---- Protocol methods ---------------------------------------------------

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        grounding: GroundingDepth = "skip",
        temperature: float | None = None,
    ) -> str:
        resp = self._generate(
            model=self._model,
            messages=messages,
            grounding=grounding,
            temperature=temperature,
        )
        return (resp.text or "").strip()

    def generate_structured(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        grounding: GroundingDepth = "skip",
        temperature: float | None = None,
    ) -> T:
        if grounding != "skip":
            # Gemini structured output and google_search are mutually exclusive.
            # Honor the stronger constraint (schema) and warn via exception text.
            raise LLMProviderError(
                "generate_structured cannot be combined with grounding; "
                "research first, then call generate_structured(grounding='skip')"
            )
        resp = self._generate(
            model=self._model,
            messages=messages,
            grounding="skip",
            temperature=temperature,
            response_schema=schema,
        )
        raw = (resp.text or "").strip()
        if not raw:
            raise LLMProviderError("empty response body from gemini structured call")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMProviderError(f"gemini returned non-JSON despite schema: {raw[:300]}") from e
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            raise LLMProviderError(f"gemini output failed schema validation: {e}") from e

    def classify_grounding_depth(self, prompt: str) -> GroundingDepth:
        msg = LLMMessage(role="user", content=_DEPTH_CLASSIFIER_PROMPT.format(prompt=prompt))
        resp = self._generate(
            model=self._classifier_model,
            messages=[msg],
            grounding="skip",
            temperature=0.0,
        )
        token = (resp.text or "").strip().lower().split()[0] if resp.text else "light"
        if token in {"skip", "light", "deep"}:
            return token  # type: ignore[return-value]
        return "light"  # safe default

    def research(
        self,
        query: str,
        *,
        depth: GroundingDepth = "light",
    ) -> ResearchResult:
        if depth == "skip":
            text = self.chat([LLMMessage(role="user", content=query)], grounding="skip")
            return ResearchResult(text=text, depth="skip")
        resp = self._generate(
            model=self._model,
            messages=[LLMMessage(role="user", content=query)],
            grounding=depth,
            temperature=None,
        )
        return ResearchResult(
            text=(resp.text or "").strip(),
            citations=self._extract_citations(resp),
            depth=depth,
            queries=self._extract_queries(resp),
        )


# ---------------------------------------------------------------------------
# Convenience factory used by the top-level llm factory.
# ---------------------------------------------------------------------------


def build_from_env(
    *,
    model: str,
    classifier_model: str,
    api_key_env: str,
    temperature: float,
    max_grounded_calls_per_run: int,
) -> GeminiProvider:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise LLMProviderError(
            f"missing api key: env var {api_key_env!r} is unset; "
            "set it in your shell or .env before constructing GeminiProvider"
        )
    return GeminiProvider(
        model=model,
        classifier_model=classifier_model,
        api_key=api_key,
        temperature=temperature,
        max_grounded_calls_per_run=max_grounded_calls_per_run,
    )
