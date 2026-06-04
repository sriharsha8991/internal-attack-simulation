"""Campaign memory — the single owner of the agent's evolving fact store.

``state["memory"]`` is a plain dict (so LangGraph can checkpoint/serialise it),
but every *mutation* of it — deep-merging confirmed facts, appending phase
narratives, and the speculative ``pending.*`` key lifecycle — used to live as
duplicated inline logic across the push and analyse nodes in ``graph.py``.

``CampaignMemory`` is a thin wrapper over that dict that centralises those
operations so the graph nodes stay pure routing. Construct it from state,
mutate, then write ``.data`` back into the returned node delta::

    mem = CampaignMemory.from_state(state)
    mem.clear_pending().merge_facts(update.facts).add_narrative(
        phase=phase, text=update.narrative, ts=_now()
    )
    return {"memory": mem.data, ...}

``project_for_prompt`` produces a *reduced* view for LLM prompts (drops internal
``pending.*`` bookkeeping and caps the narrative tail) so per-call token count
stops growing linearly with campaign length. The canonical on-disk store keeps
the full history — only what the model *sees* is trimmed.
"""

from __future__ import annotations

from typing import Any

# Default number of most-recent narratives included in a prompt projection.
DEFAULT_PROMPT_NARRATIVES = 6

_PENDING_PREFIX = "pending."


def _merge_lists(base: list, delta: list) -> list:
    """Union two lists preserving order, de-duping where items are comparable."""
    out = list(base)
    for item in delta:
        # `in` works for both hashable and unhashable (dict/list) items via ==.
        if item not in out:
            out.append(item)
    return out


def _deep_merge(base: dict, delta: dict) -> dict:
    """Recursively merge ``delta`` into a copy of ``base``.

    Memory is a nested structure (e.g. ``{"network": {"cidr": ..., "live_hosts":
    [...]}}``). A shallow top-level merge lets a later phase that emits only
    ``{"network": {"cidr": ...}}`` clobber the entire prior ``network`` sub-dict,
    silently dropping ``live_hosts``. Deep-merge preserves sibling sub-keys:

      * nested dicts merge key-by-key (recursively),
      * lists are unioned (append new items, preserve order),
      * scalars — and any type mismatch — overwrite.
    """
    out = dict(base)
    for k, v in delta.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = _deep_merge(cur, v)
        elif isinstance(cur, list) and isinstance(v, list):
            out[k] = _merge_lists(cur, v)
        else:
            out[k] = v
    return out


def project_for_prompt(
    memory: dict[str, Any],
    *,
    max_narratives: int | None = DEFAULT_PROMPT_NARRATIVES,
    drop_pending: bool = True,
) -> dict[str, Any]:
    """Return a reduced copy of ``memory`` suitable for an LLM prompt.

    Strips speculative ``pending.*`` keys (internal bookkeeping with no signal
    for planning) and caps the ``narratives`` list to its most-recent tail. All
    structured facts are preserved verbatim. The input is never mutated.
    """
    out: dict[str, Any] = {}
    for k, v in memory.items():
        if drop_pending and k.startswith(_PENDING_PREFIX):
            continue
        if (
            k == "narratives"
            and isinstance(v, list)
            and max_narratives is not None
            and len(v) > max_narratives
        ):
            out[k] = v[-max_narratives:]
        else:
            out[k] = v
    return out


class CampaignMemory:
    """Mutable wrapper over the ``state["memory"]`` dict.

    All mutators return ``self`` so calls can be chained. ``.data`` exposes the
    underlying dict for writing back into a node's state delta.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(data or {})

    @classmethod
    def from_state(cls, state: Any) -> "CampaignMemory":
        """Build from a SessionState (or any mapping with a ``memory`` key)."""
        return cls(state.get("memory") or {})

    # ---- access -------------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        """The underlying dict — write this back into the node delta."""
        return self._data

    def project_for_prompt(
        self,
        *,
        max_narratives: int | None = DEFAULT_PROMPT_NARRATIVES,
        drop_pending: bool = True,
    ) -> dict[str, Any]:
        return project_for_prompt(
            self._data, max_narratives=max_narratives, drop_pending=drop_pending
        )

    # ---- mutation -----------------------------------------------------------

    def merge_facts(self, facts: dict[str, Any] | None) -> "CampaignMemory":
        """Deep-merge a fact delta, preserving nested sibling sub-keys."""
        if facts:
            self._data = _deep_merge(self._data, facts)
        return self

    def add_narrative(
        self, *, phase: str, text: str | None, ts: str
    ) -> "CampaignMemory":
        """Append a phase narrative entry. No-op when ``text`` is empty."""
        if not text:
            return self
        narratives = list(self._data.get("narratives") or [])
        narratives.append({"phase": phase, "ts": ts, "text": text})
        self._data["narratives"] = narratives
        return self

    def add_pending(self, *, phase: str, ability: str) -> "CampaignMemory":
        """Record a speculative ``pending.<phase>.<ability>`` marker."""
        self._data[f"{_PENDING_PREFIX}{phase}.{ability}"] = "awaiting_results"
        return self

    def clear_pending(self) -> "CampaignMemory":
        """Remove every ``pending.*`` key (they were never confirmed)."""
        for k in [k for k in self._data if k.startswith(_PENDING_PREFIX)]:
            del self._data[k]
        return self

    def pending_keys(self) -> list[str]:
        return [k for k in self._data if k.startswith(_PENDING_PREFIX)]
