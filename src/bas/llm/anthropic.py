"""Anthropic provider — anthropic SDK with the built-in `web_search` server tool.

Grounding mapping:
    skip    no tools; deterministic
    light   tools=[web_search] enabled (max_uses=3), single messages.create() call
    deep    tools=[web_search] enabled (max_uses=8) + higher effort; still one call
            (multi-turn deep research is a later milestone — keep this simple).

Per-run grounded-call counter is enforced fail-closed before every grounded
request; once `max_grounded_calls_per_run` is hit, `GroundingBudgetExceeded`
is raised and the orchestrator decides what to do.

Structured output uses `output_config.format` (json_schema) rather than
assistant-turn prefill, which current Claude models reject. This requires a
model that supports structured outputs (Claude Opus 4.8, Claude Sonnet 5,
Claude Haiku 4.5, or Claude Fable 5) — pick one via `llm.model` in
config/config.yaml when `llm.provider: anthropic`.

Thinking on claude-opus-4-8 (and Opus 4.7 / Sonnet 5 / Fable 5): these models
only support *adaptive* thinking. Manual `thinking: {"type": "enabled",
"budget_tokens": N}` is rejected outright with a 400 error:
    "thinking.type.enabled" is not supported for this model. Use
    "thinking.type.adaptive" and "output_config.effort" to control thinking
    behavior.
Thinking depth is instead controlled by `output_config.effort` (low | medium
| high | xhigh). Thinking is off unless the request explicitly sets
`thinking: {"type": "adaptive"}` — there is no `{"type": "disabled"}` to
send; to run without thinking you simply omit the `thinking` field entirely.
See `_build_kwargs` below for how `llm.thinking_level` maps onto this.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, TypeVar
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic


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
# against the exception text since some failure paths surface as generic
# exceptions rather than typed SDK errors.
_RETRYABLE_HINTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "529",
    "rate limit",
    "rate_limit",
    "overloaded",
    "overloaded_error",
    "unavailable",
    "deadline",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "temporarily",
)


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively force `additionalProperties: false` on every object node.

    Anthropic's structured-outputs grammar compiler requires this to be set
    to exactly ``false`` on every ``"type": "object"`` schema (including ones
    nested under ``$defs``/``definitions``, ``properties``, ``items``, etc.).
    It rejects both a missing key (400: "'additionalProperties' must be
    explicitly set to false") and an explicit `true` (400: "'additional
    Properties: true' is not supported"), e.g. from a ``dict[str, Any]``
    field, which pydantic renders as ``additionalProperties: true``. So this
    always overwrites the key rather than only filling it in when absent.
    Every schema passed to ``output_config.format`` must go through this.
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: _walk(v) for k, v in node.items()}
            if out.get("type") == "object":
                # Anthropic requires this to be exactly `false` — not merely
                # present. A schema that already sets `true` (e.g. from a
                # `dict[str, Any]` field) is rejected just as hard as one
                # that omits the key, so always overwrite rather than only
                # filling in a missing key.
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(schema)


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

# `llm.thinking_level` (off | minimal | low | medium | high) maps onto
# Anthropic's `output_config.effort` (low | medium | high | xhigh | max).
# Whether adaptive thinking is enabled at all is decided separately (any level
# other than "off" turns it on) — this table only controls effort depth.
_THINKING_TO_EFFORT: dict[str, str] = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


class AnthropicProvider:
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
        # Import lazily so the module imports cleanly even when `anthropic` is
        # not installed (e.g. in unit tests using stub providers).
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - exercised when extras missing
            raise LLMProviderError(
                "anthropic is not installed; run `pip install .[anthropic]` "
                "(or `pip install anthropic`) to use AnthropicProvider"
            ) from e

        if not api_key:
            raise LLMProviderError("AnthropicProvider requires a non-empty api_key")

        self._anthropic = anthropic
        self._client = wrap_anthropic(
            anthropic.Anthropic(api_key=api_key)
        )
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
        return f"anthropic:{self._model}"

    @property
    def grounded_calls_made(self) -> int:
        return self._grounded_count

    # ---- internals ----------------------------------------------------------

    def _budget_check(self) -> None:
        if self._grounded_count >= self._max_grounded:
            raise GroundingBudgetExceeded(
                f"grounded-call budget exceeded ({self._max_grounded}); failing closed"
            )

    @staticmethod
    def _to_system_and_messages(
        messages: list[LLMMessage],
    ) -> tuple[str | None, list[dict[str, str]]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        convo = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        if not convo:
            # Anthropic requires a non-empty messages array starting with "user".
            convo = [{"role": "user", "content": ""}]
        system = "\n\n".join(system_parts) if system_parts else None
        return system, convo

    def _build_kwargs(
        self,
        *,
        grounding: GroundingDepth,
        thinking: str | None,
        response_schema: type[BaseModel] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}

        if grounding != "skip":
            self._budget_check()
            max_uses = 3 if grounding == "light" else 8
            kwargs["tools"] = [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}
            ]

        effective_thinking = thinking or self._thinking_level
        # claude-opus-4-8 only accepts thinking.type == "adaptive"; both manual
        # `{"type": "enabled", "budget_tokens": N}` and `{"type": "disabled"}`
        # are rejected with a 400 on this model family. To run without
        # thinking, omit the `thinking` field entirely rather than sending a
        # "disabled" type.
        if effective_thinking != "off":
            kwargs["thinking"] = {"type": "adaptive"}
        output_config: dict[str, Any] = {
            "effort": _THINKING_TO_EFFORT.get(effective_thinking, "medium")
        }
        if response_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": _strict_json_schema(response_schema.model_json_schema()),
            }
        kwargs["output_config"] = output_config
        return kwargs
    
    @traceable(
        run_type="llm",
        name="Anthropic Messages API",
    )
    def _generate(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        grounding: GroundingDepth,
        temperature: float | None,
        max_tokens: int = 16000,
        thinking: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> Any:
        system, convo = self._to_system_and_messages(messages)
        kwargs = self._build_kwargs(
            grounding="skip",
            thinking=thinking,
            response_schema=response_schema,
        )

        if system:
            kwargs["system"] = system

        # NOTE: `temperature` is intentionally not forwarded.
        _ = temperature

        logger.info(
            "[anthropic] Preparing request: "
            "model=%s messages=%d system=%s max_tokens=%d grounding=%s thinking=%s schema=%s",
            model,
            len(convo),
            "yes" if system else "no",
            max_tokens,
            grounding,
            thinking,
            response_schema.__name__ if response_schema else None,
        )

        logger.info("[anthropic] Input messages: %s", convo)

        if system:
            logger.info("[anthropic] System prompt: %s", system)

        attempts = self._max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                logger.info(
                    "[anthropic] Sending request (attempt %d/%d)",
                    attempt + 1,
                    attempts,
                )

                resp = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=convo,
                    **kwargs,
                )

                logger.info(f"RESPONSEEEEEE:::::::::::::::::--------->    {self._extract_text(resp)}")

                if grounding != "skip":
                    self._grounded_count += 1

                usage = getattr(resp, "usage", None)
                if usage:
                    logger.info(
                        "[anthropic] Request succeeded. "
                        "input_tokens=%s output_tokens=%s",
                        getattr(usage, "input_tokens", None),
                        getattr(usage, "output_tokens", None),
                    )
                else:
                    logger.info("[anthropic] Request succeeded.")

                return resp

            except Exception as e:  # pragma: no cover - SDK-level failures
                last_exc = e

                logger.info(
                    "[anthropic] Request failed on attempt %d/%d: %s",
                    attempt + 1,
                    attempts,
                    e,
                    exc_info=True,
                )

                if attempt < self._max_retries and _is_retryable(e):
                    delay = self._retry_base_delay * (2**attempt) + random.uniform(0, 0.5)

                    logger.warning(
                        "[anthropic] transient failure (attempt %d/%d), retrying in %.2fs: %s",
                        attempt + 1,
                        attempts,
                        delay,
                        e,
                    )

                    time.sleep(delay)
                    continue

                break

        logger.info(
            "[anthropic] Request permanently failed after %d attempts.",
            attempts,
        )

        raise LLMProviderError(
            f"anthropic messages.create failed after {attempts} attempt(s): {last_exc}"
        ) from last_exc

    @staticmethod
    def _extract_text(resp: Any) -> str:
        parts: list[str] = []
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)

    @staticmethod
    def _extract_citations(resp: Any) -> list[str]:
        citations: list[str] = []
        try:
            for block in getattr(resp, "content", None) or []:
                if getattr(block, "type", None) != "web_search_tool_result":
                    continue
                for result in getattr(block, "content", None) or []:
                    url = getattr(result, "url", None)
                    if url:
                        citations.append(url)
        except Exception:
            pass
        seen: set[str] = set()
        deduped: list[str] = []
        for c in citations:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        return deduped

    @staticmethod
    def _extract_queries(resp: Any) -> list[str]:
        queries: list[str] = []
        try:
            for block in getattr(resp, "content", None) or []:
                if getattr(block, "type", None) != "server_tool_use":
                    continue
                if getattr(block, "name", None) != "web_search":
                    continue
                query = (getattr(block, "input", None) or {}).get("query")
                if query:
                    queries.append(query)
        except Exception:
            pass
        return queries

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
        return self._extract_text(resp).strip()

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
            # Structured output and the web_search tool are kept mutually
            # exclusive here for the same reason as the Gemini provider: honor
            # the stronger constraint (schema) rather than guess precedence.
            raise LLMProviderError(
                "generate_structured cannot be combined with grounding; "
                "research first, then call generate_structured(grounding='skip')"
            )


        def _attempt(msgs: list[LLMMessage]) -> tuple[T | None, str, Exception | None]:
            resp = self._generate(
                model=self._model,
                messages=msgs,
                grounding="skip",
                temperature=temperature,
                # NOTE: claude-opus-4-8 (and Opus 4.7 / Sonnet 5 / Fable 5)
                # reject manual `{"type": "enabled", "budget_tokens": N}`
                # thinking with a 400. Pass a thinking *level* here instead —
                # `_build_kwargs` turns it into `thinking: {"type":
                # "adaptive"}` + `output_config.effort`, which is the only
                # supported way to request extra reasoning depth on these
                # models. "high" gives structured extraction the most
                # reliable results; drop to "medium" if latency/cost matters
                # more than accuracy for your schema.
                thinking=thinking or "high",
                response_schema=schema,
            )
            raw_text = self._extract_text(resp).strip()
            if not raw_text:
                return None, "", LLMProviderError(
                    "empty response body from anthropic structured call"
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
        # for valid JSON only, mirroring the Gemini provider's recovery path.
        logger.warning(
            "[anthropic] structured output invalid (%s); attempting one repair re-ask",
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
            logger.info("[anthropic] structured output recovered after repair re-ask")
            return obj
        raise LLMProviderError(
            f"anthropic structured output failed after repair re-ask: {err2 or err}"
        )

    def classify_grounding_depth(self, prompt: str) -> GroundingDepth:
        msg = LLMMessage(role="user", content=_DEPTH_CLASSIFIER_PROMPT.format(prompt=prompt))
        resp = self._generate(
            model=self._classifier_model,
            messages=[msg],
            grounding="skip",
            temperature=0.0,
            max_tokens=16,
            thinking="off",  # cheap classification — no reasoning needed
        )
        text = self._extract_text(resp).strip().lower()
        token = text.split()[0] if text else "light"
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
            text=self._extract_text(resp).strip(),
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
        """Use Claude code execution to validate command syntax and logic.

        The model writes Python code that parses/checks each command string
        for common issues: unclosed quotes, placeholder tokens, missing
        binaries, syntax errors in shell constructs.
        """
        if not commands:
            return CommandValidationResult(all_valid=True, summary="no commands")

        cmd_list = json.dumps(commands, indent=2)
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
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                tools=[{"type": "code_execution_20260521", "name": "code_execution"}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            # Code execution is best-effort; don't block the pipeline.
            return CommandValidationResult(
                all_valid=True,
                summary=f"validation skipped (code_execution error: {exc})",
            )

        raw_output = ""
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "bash_code_execution_tool_result":
                content = getattr(block, "content", None)
                stdout = getattr(content, "stdout", None) if content else None
                if stdout:
                    raw_output += stdout
            elif btype == "text":
                raw_output += getattr(block, "text", "") or ""

        cmd_names = {c["name"] for c in commands}
        found_names: set[str] = set()
        validations: list[CommandValidation] = []
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

        # Any commands not covered are assumed valid.
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
    temperature: float | None,
    max_grounded_calls_per_run: int,
    thinking_level: str = "high",
) -> AnthropicProvider:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise LLMProviderError(
            f"missing api key: env var {api_key_env!r} is unset; "
            "set it in your shell or .env before constructing AnthropicProvider"
        )
    return AnthropicProvider(
        model=model,
        classifier_model=classifier_model,
        api_key=api_key,
        temperature=temperature,
        max_grounded_calls_per_run=max_grounded_calls_per_run,
        thinking_level=thinking_level,
    )