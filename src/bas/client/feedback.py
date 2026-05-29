"""AI operation feedback — POST /ai/operation-feedback.

Sends command corrections for existing stage IDs so the backend can patch
and re-execute without creating new abilities or adversaries.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from .transport import HttpTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AIStageChange(BaseModel):
    """One corrected stage to include in the feedback payload."""

    operation_id: str
    ability_id: str
    stage_id: str
    suggested_command_template: str
    reason: str
    confidence: float = 0.9
    apply_immediately: bool = True


class AIFeedbackPayload(BaseModel):
    """Top-level payload for ``POST /ai/operation-feedback``."""

    source: str = "operation-analyzer"
    loop_status: str = "continue"
    changes: list[AIStageChange] = Field(default_factory=list)
    engagement_id: str | None = None
    engagement_status: str | None = None


# ---------------------------------------------------------------------------
# Resource client
# ---------------------------------------------------------------------------


class FeedbackApi:
    """Send AI-generated command corrections to the BAS backend."""

    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    def _post_feedback(self, payload: AIFeedbackPayload) -> None:
        """POST the feedback payload to the backend. Shared by send() and finalize()."""
        self._t.post_json(
            "/ai/operation-feedback",
            json=payload.model_dump(mode="json"),
        )

    def send(
        self,
        operation_id: str,
        changes: list[AIStageChange],
        *,
        loop_status: str = "continue",
        engagement_id: str | None = None,
    ) -> None:
        """POST /ai/operation-feedback with the given stage changes.

        Does nothing in dry-run mode.
        """
        if not changes:
            logger.info("[feedback] no changes to send — skipping")
            return

        payload = AIFeedbackPayload(
            loop_status=loop_status,
            changes=changes,
            engagement_id=engagement_id,
        )

        if self._dry:
            logger.info(
                "[feedback] DRY-RUN: would POST %d changes for op=%s",
                len(changes),
                operation_id,
            )
            return

        self._post_feedback(payload)
        logger.info(
            "[feedback] sent %d stage corrections for op=%s",
            len(changes),
            operation_id,
        )

    def finalize(
        self,
        engagement_id: str,
        engagement_status: str,
    ) -> None:
        """Notify the backend that the engagement is complete.

        Sends a terminal feedback with ``loop_status='finalize'`` and no
        changes. Does nothing in dry-run mode.
        """
        if self._dry:
            logger.info(
                "[feedback] DRY-RUN: would finalize engagement=%s status=%s",
                engagement_id,
                engagement_status,
            )
            return

        payload = AIFeedbackPayload(
            source="operation-analyzer",
            loop_status="finalize",
            changes=[],
            engagement_id=engagement_id,
            engagement_status=engagement_status,
        )
        self._post_feedback(payload)
        logger.info(
            "[feedback] finalized engagement=%s status=%s",
            engagement_id,
            engagement_status,
        )
