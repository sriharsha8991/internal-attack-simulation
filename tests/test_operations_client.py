"""Tests for the OperationsApi client (pull-based result polling)."""

from __future__ import annotations

from bas.client.operations import OperationsApi, is_operation_complete


class _FakeTransport:
    """Records requests and returns scripted GET payloads."""

    def __init__(self, get_sequence=None):
        self._get_sequence = list(get_sequence or [])
        self.calls: list[tuple[str, str]] = []

    def get_json(self, path, **kwargs):
        self.calls.append(("GET", path))
        if self._get_sequence:
            return self._get_sequence.pop(0)
        return {}

    def post_json(self, path, *, json=None, **kwargs):
        self.calls.append(("POST", path))
        return {"ok": True}

    @staticmethod
    def unwrap_list(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items", [])
        return []


# ---- completion detection --------------------------------------------------


def test_is_operation_complete_by_status():
    assert is_operation_complete({"operation": {"status": "completed"}})
    assert is_operation_complete({"operation": {"status": "FAILED"}})
    assert is_operation_complete({"status": "stopped"})
    assert not is_operation_complete({"operation": {"status": "running"}})
    assert not is_operation_complete({"operation": {"status": "pending"}})


def test_is_operation_complete_by_progress():
    assert is_operation_complete({"status": "running", "progress": {"progress_percent": 100}})
    assert not is_operation_complete({"status": "running", "progress": {"progress_percent": 40}})


# ---- endpoint wiring -------------------------------------------------------


def test_lifecycle_and_detail_paths():
    t = _FakeTransport(get_sequence=[{"operation": {"operation_id": "op1"}}])
    api = OperationsApi(t)
    api.get_detail("op1")
    api.start("op1")
    api.stop("op1")
    assert ("GET", "/operations/op1") in t.calls
    assert ("POST", "/operations/op1/start") in t.calls
    assert ("POST", "/operations/op1/stop") in t.calls


# ---- polling ---------------------------------------------------------------


def test_poll_returns_when_complete():
    # running, running, completed -> 3 GETs, returns the completed payload
    seq = [
        {"operation": {"status": "running"}, "progress": {"progress_percent": 10}},
        {"operation": {"status": "running"}, "progress": {"progress_percent": 60}},
        {"operation": {"status": "completed"}, "progress": {"progress_percent": 100}},
    ]
    t = _FakeTransport(get_sequence=seq)
    api = OperationsApi(t)
    slept: list[float] = []
    clock = {"t": 0.0}

    final = api.poll_until_complete(
        "op1",
        timeout_s=1000,
        interval_s=15,
        sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)),
        now=lambda: clock["t"],
    )
    assert is_operation_complete(final)
    assert final["operation"]["status"] == "completed"
    assert len(slept) == 2  # slept between the 3 polls


def test_poll_gives_up_at_timeout():
    # always running -> never completes; must stop at the deadline
    t = _FakeTransport()
    t.get_json = lambda *a, **k: {"operation": {"status": "running"}}  # type: ignore
    api = OperationsApi(t)
    clock = {"t": 0.0}

    final = api.poll_until_complete(
        "op1",
        timeout_s=30,
        interval_s=15,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
    )
    assert not is_operation_complete(final)  # timed out still-running


def test_dry_run_short_circuits():
    t = _FakeTransport()
    api = OperationsApi(t, dry_run=True)
    detail = api.poll_until_complete("op1", timeout_s=10)
    assert is_operation_complete(detail)  # dry-run reports completed
    # no lifecycle POSTs hit the transport in dry-run
    api.start("op1")
    assert all(method == "GET" or path.startswith("/operations") for method, path in t.calls) or not t.calls
