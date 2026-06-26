"""Two-tier timeout-scanner behaviour (soft = recoverable, hard = abandon).

Verifies that an overdue-but-within-hard-cap engagement is left PARKED (so a
late /results still resumes it), while one past the hard cap is force-resumed.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import bas.worker as worker


class _FakeStore:
    def __init__(self, record: dict) -> None:
        self._rec = record
        self.saved: list[dict] = []

    def list_all(self):
        return [dict(self._rec)]

    def get(self, _id):
        return dict(self._rec)

    def save(self, rec):
        self._rec = dict(rec)
        self.saved.append(dict(rec))


class _FakeCompiled:
    def __init__(self) -> None:
        self.invoked = 0

    def invoke(self, *_a, **_k):
        self.invoked += 1
        return {}


def _setup(monkeypatch, *, elapsed_s: int):
    awaiting_since = (
        datetime.now(timezone.utc) - timedelta(seconds=elapsed_s)
    ).isoformat()
    record = {
        "run_id": "eng1",
        "status": "awaiting_results",
        "awaiting_since": awaiting_since,
    }
    store = _FakeStore(record)
    compiled = _FakeCompiled()
    monkeypatch.setitem(worker._state, "store", store)
    monkeypatch.setattr(worker, "_get_compiled_graph", lambda: compiled)
    monkeypatch.setattr(worker, "_get_engagement_lock", lambda _id: threading.Lock())
    worker._overdue_warned.clear()
    return store, compiled


def test_overdue_within_hard_cap_stays_parked(monkeypatch):
    # elapsed past soft (6000) but under hard (12000): must NOT force-resume.
    store, compiled = _setup(monkeypatch, elapsed_s=8000)
    worker._expire_stale_engagements(6000, 12000)
    assert compiled.invoked == 0
    assert store._rec["status"] == "awaiting_results"  # still parked
    # warned exactly once (idempotent within the same wait)
    worker._expire_stale_engagements(6000, 12000)
    assert compiled.invoked == 0


def test_past_hard_cap_force_resumes(monkeypatch):
    store, compiled = _setup(monkeypatch, elapsed_s=13000)
    worker._expire_stale_engagements(6000, 12000)
    assert compiled.invoked == 1
    assert store._rec["status"] == "completed"


def test_hard_cap_disabled_never_abandons(monkeypatch):
    # hard=0 disables forced abandonment regardless of how long it's been.
    store, compiled = _setup(monkeypatch, elapsed_s=999_999)
    worker._expire_stale_engagements(6000, 0)
    assert compiled.invoked == 0
    assert store._rec["status"] == "awaiting_results"


def test_within_soft_no_warn_no_resume(monkeypatch):
    store, compiled = _setup(monkeypatch, elapsed_s=100)
    worker._expire_stale_engagements(6000, 12000)
    assert compiled.invoked == 0
    assert not worker._overdue_warned  # not even overdue yet
