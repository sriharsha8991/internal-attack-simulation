"""Plain-Python tool for retrieving payload metadata details.

This tool allows Master and Planner agents to dynamically search or retrieve details 
about pre-uploaded payloads directly on demand during planning or review.
"""

from __future__ import annotations

import logging
from typing import Any
from ..bootstrap import get_kali

logger = logging.getLogger(__name__)

def get_payload_by_id(payload_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Retrieve metadata for a specific payload using its UUID string.

    Returns the payload details dict or None if not found in the catalog.
    """
    from ..bootstrap import _state
    catalog = _state.get("payload_catalog")
    if not catalog or not catalog.entries:
        return None

    pid_str = str(payload_id).strip().lower()
    for entry in catalog.entries:
        if entry.payload_id and str(entry.payload_id).strip().lower() == pid_str:
            return {
                "payload_id": str(entry.payload_id),
                "name": entry.name,
                "platform": entry.platform or "any",
                "type": entry.type or "-",
                "risk": entry.risk_classification or "-",
                "description": entry.description or "",
                "category": entry.category or ""
            }
    return None


def search_payloads(query: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Search pre-uploaded payloads in the catalog matching a query string in name or description.

    Can also filter by platform matching the foothold if specified in query keywords.
    """
    from ..bootstrap import _state
    catalog = _state.get("payload_catalog")
    if not catalog or not catalog.entries:
        return []

    q = query.strip().lower()
    results = []
    
    # Optional foothold platform check
    foothold_platform = (state.get("foothold") or {}).get("platform", "").lower()

    for entry in catalog.entries:
        # Platform match gate: if foothold platform exists, avoid suggesting mismatched payloads
        if entry.platform and foothold_platform and entry.platform.lower() != foothold_platform:
            if entry.platform.lower() not in ("any", "all", "*"):
                continue

        name = (entry.name or "").lower()
        desc = (entry.description or "").lower()
        cat = (entry.category or "").lower()

        if q in name or q in desc or q in cat:
            results.append({
                "payload_id": str(entry.payload_id) if entry.payload_id else "?",
                "name": entry.name,
                "platform": entry.platform or "any",
                "type": entry.type or "-",
                "risk": entry.risk_classification or "-",
                "description": entry.description or "",
                "category": entry.category or ""
            })

    return results
