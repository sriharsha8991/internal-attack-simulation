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

from .utils import _now

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


def project_for_prompt(mem: dict[str, Any]) -> dict[str, Any]:
    """Return a reduced copy of ``memory`` suitable for an LLM prompt.

    Strips speculative ``pending.*`` keys (internal bookkeeping with no signal
    for planning) and caps the ``narratives`` list to its most-recent tail. All
    structured facts are preserved verbatim. The input is never mutated.
    """
    out: dict[str, Any] = {}
    for k, v in mem.items():
        if k.startswith(_PENDING_PREFIX):
            continue
        if (
            k == "narratives"
            and isinstance(v, list)
            and DEFAULT_PROMPT_NARRATIVES is not None
            and len(v) > DEFAULT_PROMPT_NARRATIVES
        ):
            out[k] = v[-DEFAULT_PROMPT_NARRATIVES:]
        else:
            out[k] = v
    return out


def persist_memory(state: dict[str, Any], memory: dict[str, Any], *, label: str = "") -> None:
    """Write the agent's memory and campaign progress to disk as JSON.

    File: ``<results_dir>/../memory.json`` (i.e. ``engagements/<id>/memory.json``).
    Each write is atomic (tmp + rename) and overwrites the previous snapshot.

    The snapshot includes everything the agent needs to resume:
    - ``memory``: structured facts, narratives, pending keys
    - ``campaign_progress``: completed/available/current phases, phase history
    """
    import json
    import os
    import logging
    from pathlib import Path
    logger = logging.getLogger(__name__)

    results_dir = state.get("results_dir")
    if not results_dir:
        return
    engagement_dir = Path(results_dir).parent
    target = engagement_dir / "memory.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        # Build compact phase history (drop verbose key_commands)
        phase_history = []
        for rec in (state.get("phase_history") or []):
            phase_history.append({
                "phase": rec.get("phase"),
                "objective": rec.get("objective"),
                "outcome": rec.get("outcome"),
                "skills_used": rec.get("skills_used"),
                "techniques_used": rec.get("techniques_used"),
                "ability_names": (rec.get("ability_names") or [])[:10],
                "execution_outcome": rec.get("execution_outcome"),
                "memory_delta_keys": rec.get("memory_delta_keys"),
            })

        completed = list(state.get("completed_phases") or [])
        available = list(state.get("available_phases") or [])
        remaining = [p for p in available if p not in completed]

        snapshot = {
            "run_id": state.get("run_id", ""),
            "updated_at": _now(),
            "label": label,
            "campaign_progress": {
                "current_phase": state.get("current_phase", ""),
                "completed_phases": completed,
                "available_phases": available,
                "remaining_phases": remaining,
                "iteration": state.get("iteration", 0),
                "phase_history": phase_history,
            },
            "memory": memory,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        logger.debug(
            "[memory] persisted %d keys to %s (%s) — completed=%s remaining=%s",
            len(memory), target, label, completed, remaining,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[memory] failed to persist memory to %s", target, exc_info=True)


def load_memory_from_disk(results_dir: str | None) -> dict[str, Any]:
    """Reload the full memory snapshot from ``memory.json``.

    Returns the complete snapshot dict with keys:
    - ``memory``: the agent's structured facts/narratives
    - ``campaign_progress``: completed_phases, phase_history, etc.

    Returns empty dict if file not found or unreadable.
    """
    import json
    import logging
    from pathlib import Path
    logger = logging.getLogger(__name__)

    if not results_dir:
        return {}
    mem_file = Path(results_dir).parent / "memory.json"
    if not mem_file.is_file():
        return {}
    try:
        with mem_file.open("r", encoding="utf-8") as f:
            snapshot = json.load(f)
        progress = snapshot.get("campaign_progress", {})
        mem = snapshot.get("memory", {})
        logger.info(
            "[memory] reloaded from %s (label=%s) — "
            "memory_keys=%d completed=%s current=%s remaining=%s",
            mem_file,
            snapshot.get("label", ""),
            len(mem),
            progress.get("completed_phases", []),
            progress.get("current_phase", ""),
            progress.get("remaining_phases", []),
        )
        return snapshot
    except Exception:  # noqa: BLE001
        logger.warning("[memory] failed to reload from %s", mem_file, exc_info=True)
        return {}


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
