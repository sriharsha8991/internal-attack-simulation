"""Payload catalog — prompt-context shim over ``GET /payloads``.

The BAS backend stores pre-uploaded payload binaries (e.g. SharpHound.exe) and
exposes them via ``GET /payloads``. Each ability stage may reference one by
setting ``AbilityStageCreate.payload_id``; the on-target BAS agent then
downloads via ``/payloads/{id}/download`` and substitutes the local path at
execution time.

This module fetches the catalog once per engagement and renders it into:
  * a per-phase planner-prompt block — so the specialist LLM can attach a real
    ``payload_id`` to a stage when the listed binary is the right tool;
  * a per-phase master-prompt summary — so the master router can mention
    pre-uploaded payloads in ``PhaseBriefing.constraints``.

Filtering pairs ``PayloadCategory`` (matches the ``Phase`` enum exactly) with
foothold platform compatibility so a Linux foothold never sees Windows-only
binaries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import PayloadMetadata

logger = logging.getLogger(__name__)


def _platform_matches(entry_platform: str | None, foothold_platform: str | None) -> bool:
    """Return True if the payload's declared platform is usable on the foothold.

    A missing/blank ``entry_platform`` or the literal ``"any"`` is treated as
    universally compatible. When the foothold platform is unknown we accept
    everything (no foothold filter possible).
    """
    if not entry_platform:
        return True
    ep = entry_platform.strip().lower()
    if ep in ("", "any", "all", "*"):
        return True
    if not foothold_platform:
        return True
    return ep == foothold_platform.strip().lower()


@dataclass
class PayloadCatalog:
    """In-memory view of the backend's payload metadata, scoped to an engagement."""

    entries: list[PayloadMetadata] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def fetch(cls, bas) -> "PayloadCatalog":
        """Fetch the catalog from the backend; on any failure return empty.

        Never raises — agent code expects the catalog to always exist, and an
        empty catalog simply degrades to "the planner has no payload context",
        which is the pre-existing behaviour.
        """
        try:
            entries = bas.payloads.list()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("[payloads] catalog fetch failed: %s — proceeding empty", exc)
            return cls(entries=[])
        logger.info("[payloads] catalog loaded — %d entries", len(entries))
        return cls(entries=list(entries))

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def for_phase(
        self, phase: str | None, platform: str | None = None
    ) -> list[PayloadMetadata]:
        """Return entries whose ``category`` matches the phase (if specified), AND whose platform
        is compatible with the foothold (when known). If category is missing, we include it
        so the LLM can decide based on the name/type."""
        out: list[PayloadMetadata] = []
        if not phase:
            ph = ""
        else:
            ph = phase.strip().lower()

        for e in self.entries:
            if not _platform_matches(e.platform, platform):
                continue
            cat = (e.category or "").strip().lower()
            # If the backend explicitly categorised it, enforce the filter.
            # If category is null/empty, allow it to pass through to the LLM.
            if cat and ph and cat != ph:
                continue
            out.append(e)
        return out

    def known_ids(self) -> set[str]:
        """Set of payload_id strings, used for push-time validation."""
        return {str(e.payload_id) for e in self.entries if e.payload_id is not None}

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def render_planner_block(self, phase: str | None, platform: str | None) -> str:
        """Render the planner-prompt block for one phase + platform.

        Empty string when no payloads match — silence is the strongest
        anti-hallucination signal (the LLM never sees a "0 payloads" header
        that might tempt it to invent one).
        """
        matches = self.for_phase(phase, platform)
        if not matches:
            return ""

        header_phase = (phase or "?").strip().lower()
        header_platform = (platform or "unknown").strip().lower()

        rows = [
            "| payload_id | name | platform | type | risk | description |",
            "|---|---|---|---|---|---|",
        ]
        for e in matches:
            rows.append(
                "| {pid} | {name} | {plat} | {typ} | {risk} | {desc} |".format(
                    pid=str(e.payload_id) if e.payload_id else "?",
                    name=(e.name or "").replace("|", "/"),
                    plat=(e.platform or "any"),
                    typ=(e.type or "-"),
                    risk=(e.risk_classification or "-"),
                    desc=(e.description or "").replace("|", "/").replace("\n", " ")[:160],
                )
            )

        return (
            "\n\n--- AVAILABLE PAYLOADS (pre-uploaded binaries for this phase + platform) ---\n"
            f"Phase: {header_phase}   Foothold platform: {header_platform}\n\n"
            + "\n".join(rows)
            + "\n\n"
            "RULES — read carefully:\n"
            "1. Set `payload_id` on a stage ONLY when that stage actually executes the listed binary. "
            "Recon, setup, and cleanup stages leave `payload_id: null`.\n"
            "2. NEVER invent a payload_id. Copy a UUID verbatim from the table above, "
            "or leave the field null.\n"
            "3. The on-target agent downloads the binary and substitutes its local path "
            "automatically. Write `command_template` AS IF the binary is already on PATH — "
            "do NOT hardcode paths like `C:\\Temp\\foo.exe` and do NOT use placeholders "
            "like `{payload_path}`. Example: "
            "`SharpHound.exe -CollectionMethod Default -OutputDirectory %TEMP%`.\n"
            "4. If no listed payload matches the stage's intent, prefer a native LOLBin — "
            "do not attach a payload \"just because it's there\".\n"
            "5. Foothold/payload platform mismatch = do not use (already filtered, "
            "but double-check)."
        )

    def render_master_summary(self) -> str:
        """One-line-per-phase summary for the master router prompt.

        Empty string when the entire catalog is empty.
        """
        if not self.entries:
            return ""

        by_phase: dict[str, list[PayloadMetadata]] = {}
        for e in self.entries:
            key = (e.category or "").strip().lower()
            if not key:
                key = "uncategorized/all"
            by_phase.setdefault(key, []).append(e)

        if not by_phase:
            return ""

        lines = ["\n\n--- PAYLOADS AVAILABLE (pre-uploaded binaries by phase) ---"]
        for phase in sorted(by_phase.keys()):
            items = ", ".join(
                f"{e.name} ({e.platform or 'any'})" for e in by_phase[phase]
            )
            lines.append(f"- {phase}: {items}")
        lines.append(
            "When briefing the planner for a phase that has payloads listed, "
            "prefer mentioning them in `constraints` / `open_questions` so the "
            "planner uses the pre-uploaded binary instead of fetching from the internet."
        )
        return "\n".join(lines)
