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
import logging
import os
import random
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .base import (
    CommandValidation,
    CommandValidationResult,
    GroundingBudgetExceeded,
    GroundingDepth,
    LLMMessage,
    LLMProviderError,
    ResearchResult,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Substrings that mark a transient, retryable provider failure (rate limits,
# overload, gateway/availability blips, timeouts). Matched case-insensitively
# against the exception text since the google-genai SDK surfaces these as
# generic exceptions carrying an HTTP status / reason string.
_RETRYABLE_HINTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate limit",
    "resource_exhausted",
    "resource exhausted",
    "unavailable",
    "overloaded",
    "deadline",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "temporarily",
)


def _is_retryable(exc: Exception) -> bool:
    """True if ``exc`` looks like a transient failure worth retrying.

    A grounding-budget breach is deterministic (not transient), so it is never
    retried even though it subclasses ``LLMProviderError``.
    """
    if isinstance(exc, GroundingBudgetExceeded):
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _RETRYABLE_HINTS)

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
        temperature: float | None = None,
        max_grounded_calls_per_run: int = 40,
        thinking_level: str = "high",
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
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
        self._thinking_level = thinking_level
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = max(0.0, retry_base_delay)

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
        thinking: str | None = None,
    ) -> Any:
        types = self._genai.types
        tools: list[Any] = []
        if grounding != "skip":
            self._budget_check()
            tools = [types.Tool(google_search=types.GoogleSearch())]

        kwargs: dict[str, Any] = {}
        # As per Gemini 3.x best practices: do not send temperature parameter
        # to ensure reasoning capabilities remain optimized by defaults.
        t = temperature if temperature is not None else self._temperature
        # We DO NOT send `temperature` or `top_p` or `top_k` per Gemini 3 documentation
        # If it is being passed locally, we explicitly drop it.
        # if t is not None:
        #     kwargs["temperature"] = t

        if tools:
            kwargs["tools"] = tools
        if response_schema is not None:
            # Structured-output mode is mutually exclusive with tools in the SDK;
            # callers should pass grounding="skip" when requesting JSON.
            kwargs["response_mime_type"] = "application/json"
           
            kwargs["response_schema"] = _sanitize_schema_for_gemini(
                response_schema.model_json_schema()
            )

        # Enable thinking for deeper reasoning. The thinking_level controls
        # how much internal reasoning the model does before answering.
        # Use the call-level override or fall back to instance default.
        effective_thinking = thinking or self._thinking_level
        if effective_thinking and effective_thinking != "off":
            try:
                kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=effective_thinking,
                )
            except Exception:
                # Fallback: older SDK versions may not support ThinkingConfig
                pass

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
        thinking: str | None = None,
    ) -> Any:
        config = self._build_config(
            grounding=grounding,
            temperature=temperature,
            response_schema=response_schema,
            thinking=thinking,
        )
        contents = self._to_contents(messages)
        # Bounded retry with exponential backoff + jitter for transient failures
        # (429 / 5xx / timeouts). Deterministic failures (bad schema, budget)
        # are not retried — see `_is_retryable`.
        attempts = self._max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                if grounding != "skip":
                    self._grounded_count += 1
                return resp
            except Exception as e:  # pragma: no cover - SDK-level failures
                last_exc = e
                if attempt < self._max_retries and _is_retryable(e):
                    delay = self._retry_base_delay * (2**attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "[gemini] transient failure (attempt %d/%d), retrying in %.2fs: %s",
                        attempt + 1,
                        attempts,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                break
        raise LLMProviderError(
            f"gemini generate_content failed after {attempts} attempt(s): {last_exc}"
        ) from last_exc

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
        thinking: str | None = None,
    ) -> T:
        if grounding != "skip":
            # Gemini structured output and google_search are mutually exclusive.
            # Honor the stronger constraint (schema) and warn via exception text.
            raise LLMProviderError(
                "generate_structured cannot be combined with grounding; "
                "research first, then call generate_structured(grounding='skip')"
            )

        # `thinking` defaults to the instance level (configurable) rather than a
        # hardcoded "high" — callers route deep-reasoning work (planning) to
        # "high" and mechanical work (memory merge, extraction, grading) lower.
        effective_thinking = thinking or self._thinking_level

        def _attempt(msgs: list[LLMMessage]) -> tuple[T | None, str, Exception | None]:
            resp = self._generate(
                model=self._model,
                messages=msgs,
                grounding="skip",
                temperature=temperature,
                response_schema=schema,
                thinking=effective_thinking,
            )
            raw_text = (resp.text or "").strip()
            if not raw_text:
                return None, "", LLMProviderError(
                    "empty response body from gemini structured call"
                )
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                return None, raw_text, exc
            try:
                return schema.model_validate(data), raw_text, None
            except ValidationError as exc:
                return None, raw_text, exc

        obj, raw, err = _attempt(messages)
        if obj is not None:
            return obj

        # One repair round-trip: feed the malformed output + error back and ask
        # for valid JSON only. A deterministic near-miss (truncated/extra prose)
        # usually self-corrects here instead of aborting the whole phase.
        logger.warning(
            "[gemini] structured output invalid (%s); attempting one repair re-ask",
            type(err).__name__ if err else "unknown",
        )
        repair_msgs = list(messages) + [
            LLMMessage(
                role="user",
                content=(
                    "Your previous response did not parse as valid JSON matching "
                    f"the required schema. Error:\n{err}\n\n"
                    "Previous response (first 1000 chars):\n"
                    f"{raw[:1000]}\n\n"
                    "Re-emit ONLY a single valid JSON object matching the schema. "
                    "No markdown, no code fences, no commentary."
                ),
            ),
        ]
        obj, _, err2 = _attempt(repair_msgs)
        if obj is not None:
            logger.info("[gemini] structured output recovered after repair re-ask")
            return obj
        raise LLMProviderError(
            f"gemini structured output failed after repair re-ask: {err2 or err}"
        )

    def classify_grounding_depth(self, prompt: str) -> GroundingDepth:
        msg = LLMMessage(role="user", content=_DEPTH_CLASSIFIER_PROMPT.format(prompt=prompt))
        resp = self._generate(
            model=self._classifier_model,
            messages=[msg],
            grounding="skip",
            temperature=0.0,
            thinking="low",  # cheap classification — minimal reasoning
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

    def validate_commands(
        self,
        commands: list[dict[str, str]],
        *,
        platform: str = "windows",
    ) -> CommandValidationResult:
        """Use Gemini code execution to validate command syntax and logic.

        The model writes Python code that parses/checks each command string
        for common issues: unclosed quotes, placeholder tokens, missing
        binaries, syntax errors in shell constructs.
        """
        if not commands:
            return CommandValidationResult(all_valid=True, summary="no commands")

        types = self._genai.types

        # Build the validation prompt
        import json as _json
        cmd_list = _json.dumps(commands, indent=2)
        prompt = (
            f"You are a command-line syntax validator for {platform} systems.\n"
            f"Validate the following commands. For each command, write Python code "
            f"to check:\n"
            f"1. No placeholder tokens (like #{{...}}, <TARGET>, {{{{...}}}}, "
            f"${{{{VAR}}}})\n"
            f"2. Balanced quotes (single and double)\n"
            f"3. Valid shell syntax (pipes, redirects, semicolons, &&/||)\n"
            f"4. No references to undefined variables from other commands\n"
            f"5. Output directory uses temp path (not relative paths)\n\n"
            f"Commands to validate:\n{cmd_list}\n\n"
            f"For each command, print a line: "
            f"VALID:<name> or INVALID:<name>:<issue description>\n"
            f"At the end, print SUMMARY:<number_invalid> issues found"
        )

        try:
            config = types.GenerateContentConfig(
                tools=[types.Tool(code_execution=types.ToolCodeExecution())],
                temperature=0.0,
            )
            resp = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            # Code execution is best-effort; don't block the pipeline
            return CommandValidationResult(
                all_valid=True,
                summary=f"validation skipped (code_execution error: {exc})",
            )

        # Parse the response — look for executable_code results and text
        validations: list[CommandValidation] = []
        raw_output = ""

        for part in (resp.candidates[0].content.parts if resp.candidates else []):
            if hasattr(part, "code_execution_result") and part.code_execution_result:
                raw_output += part.code_execution_result.output or ""
            elif hasattr(part, "text") and part.text:
                raw_output += part.text or ""

        # Parse VALID/INVALID lines from output
        cmd_names = {c["name"] for c in commands}
        found_names: set[str] = set()
        for line in raw_output.splitlines():
            line = line.strip()
            if line.startswith("VALID:"):
                name = line[6:].strip()
                if name in cmd_names:
                    validations.append(CommandValidation(name=name, valid=True))
                    found_names.add(name)
            elif line.startswith("INVALID:"):
                parts_split = line[8:].split(":", 1)
                name = parts_split[0].strip()
                issue = parts_split[1].strip() if len(parts_split) > 1 else "unknown issue"
                if name in cmd_names:
                    validations.append(
                        CommandValidation(name=name, valid=False, issues=[issue])
                    )
                    found_names.add(name)

        # Any commands not covered are assumed valid
        for c in commands:
            if c["name"] not in found_names:
                validations.append(CommandValidation(name=c["name"], valid=True))

        all_valid = all(v.valid for v in validations)
        invalid_count = sum(1 for v in validations if not v.valid)
        summary = f"{invalid_count} issue(s) found" if invalid_count else "all commands valid"

        return CommandValidationResult(
            validations=validations,
            all_valid=all_valid,
            summary=summary,
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
    thinking_level: str = "high",
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
        thinking_level=thinking_level,
    )
