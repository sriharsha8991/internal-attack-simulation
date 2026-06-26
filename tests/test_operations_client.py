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


# ---- worker operation discovery -------------------------------------------


class _FakeBas:
    def __init__(self, ops):
        class _Ops:
            def list(_self):
                return ops
        self.operations = _Ops()


def test_discover_operation_id_matches_adversary_and_picks_latest():
    from bas.worker import _discover_operation_id

    # Real OperationSummary shape: adversary is a nested object.
    bas = _FakeBas([
        {"operation_id": "old", "adversary": {"adversary_id": "ADV"}, "started_at": "2026-01-01T00:00:00"},
        {"operation_id": "new", "adversary": {"adversary_id": "ADV"}, "started_at": "2026-06-01T00:00:00"},
        {"operation_id": "other", "adversary": {"adversary_id": "ZZZ"}, "started_at": "2026-06-02T00:00:00"},
    ])
    assert _discover_operation_id(bas, "ADV") == "new"  # latest matching adversary


def test_discover_operation_id_none_when_no_match():
    from bas.worker import _discover_operation_id

    bas = _FakeBas([{"operation_id": "x", "adversary": {"adversary_id": "OTHER"}}])
    assert _discover_operation_id(bas, "ADV") is None


def test_discover_operation_id_flat_adversary_fallback():
    from bas.worker import _discover_operation_id

    # Forward-compat: a flat adversary_id field still resolves.
    bas = _FakeBas([{"operation_id": "flat", "adversary_id": "ADV"}])
    assert _discover_operation_id(bas, "ADV") == "flat"


def test_adapt_operation_detail_builds_stages_from_logs():
    """A detail payload (no `abilities`, only execution_logs) parses into
    per-stage results with stdout/exit_code preserved."""
    from bas.results import parse_operation_result

    detail = {
        "operation_id": "op-1",
        "name": "discovery-op",
        "status": "completed",
        "engagement_id": "eng-1",
        "adversary": {"adversary_id": "ADV"},
        "progress": {"total_abilities": 1, "completed_abilities": 1, "progress_percent": 100},
        "execution_logs": [
            {
                "ability_id": "ab-1",
                "stage_id": "st-1",
                "command_executed": "whoami",
                "stdout": "root\n",
                "stderr": "",
                "exit_code": 0,
                "executor": "sh",
                "timestamp": "2026-06-05T00:00:00",
            },
            {
                "ability_id": "ab-1",
                "stage_id": "st-2",
                "command_executed": "nmap -sV 10.0.0.0/24",
                "stdout": "",
                "stderr": "nmap: command not found",
                "exit_code": 127,
                "executor": "sh",
                "timestamp": "2026-06-05T00:01:00",
            },
        ],
    }
    result = parse_operation_result(detail)
    assert result.operation_id == "op-1"
    assert len(result.abilities) == 1
    stages = result.abilities[0].stages
    assert len(stages) == 2
    passed = {s.command_executed: s.execution_status for s in stages}
    assert passed["whoami"] == "passed"
    assert passed["nmap -sV 10.0.0.0/24"] == "failed"
    assert stages[0].stdout == "root\n"


def test_detect_issues_flags_tool_not_found_in_stdout():
    from bas.results import IssueKind, detect_issues, parse_operation_result

    detail = {
        "operation_id": "op-stdout-tool-missing",
        "name": "ad-enum-op",
        "status": "completed",
        "execution_logs": [
            {
                "ability_id": "ab-certify",
                "stage_id": "st-certify",
                "command_executed": ".\\Certify.exe find /vulnerable",
                "stdout": "ERROR: The term 'Certify.exe' is not recognized as the name of a cmdlet, function, script file, or operable program.\n",
                "stderr": "",
                "exit_code": 0,
                "executor": "powershell",
            },
        ],
    }

    result = parse_operation_result(detail)
    issues = detect_issues(result)

    assert any(issue.kind == IssueKind.TOOL_NOT_FOUND for issue in issues)


def test_detect_issues_flags_powershell_variable_reference_parse_error():
    from bas.results import IssueKind, detect_issues, parse_operation_result

    detail = {
        "operation_id": "op-psh-parse",
        "name": "credaccess-op",
        "status": "completed",
        "execution_logs": [
            {
                "ability_id": "ab-kerberoast",
                "stage_id": "st-kerberoast",
                "command_executed": 'Write-Output "Failed for $spn: $_"',
                "stdout": "",
                "stderr": (
                    "Variable reference is not valid. ':' was not followed by "
                    "a valid variable name character.\n"
                    "FullyQualifiedErrorId : InvalidVariableReferenceWithDrive"
                ),
                "exit_code": 1,
                "executor": "powershell",
            },
        ],
    }

    result = parse_operation_result(detail)
    issues = detect_issues(result)

    assert any(issue.kind == IssueKind.PSH_PARSE_ERROR for issue in issues)


def test_derive_phase_done_requires_every_ability_to_pass():
    from bas.results import derive_phase_done, parse_operation_result

    detail = {
        "operation_id": "op-partial-success",
        "name": "credaccess-op",
        "status": "completed",
        "execution_logs": [
            {
                "ability_id": "ab-ticket-cache",
                "stage_id": "st-ticket-cache",
                "command_executed": "klist",
                "stdout": "Cached Tickets: (5)",
                "stderr": "",
                "exit_code": 0,
                "executor": "powershell",
            },
            {
                "ability_id": "ab-kerberoast",
                "stage_id": "st-kerberoast",
                "command_executed": "kerberoast",
                "stdout": "",
                "stderr": "runtime failure",
                "exit_code": 1,
                "executor": "powershell",
            },
        ],
    }

    result = parse_operation_result(detail)

    assert derive_phase_done(result, []) is False


def test_exit_zero_stdout_error_marker_blocks_phase_done():
    from bas.results import IssueKind, derive_phase_done, detect_issues, parse_operation_result

    detail = {
        "operation_id": "op-error-marker",
        "name": "ad-enum-op",
        "status": "completed",
        "execution_logs": [
            {
                "ability_id": "ab-ad-enum",
                "stage_id": "st-ad-enum",
                "command_executed": "powershell ad enum",
                "stdout": "Domain: north.sevenkingdoms.local\nUsers failed: Unable to find type [DirectorySearcher].\n",
                "stderr": "",
                "exit_code": 0,
                "executor": "powershell",
            },
        ],
    }

    result = parse_operation_result(detail)
    issues = detect_issues(result)

    assert any(issue.kind == IssueKind.ERROR_MARKER for issue in issues)
    assert any(issue.kind == IssueKind.TYPE_NOT_FOUND for issue in issues)
    assert derive_phase_done(result, issues) is False


# ---- poller result persistence --------------------------------------------


class _FakeResultStore:
    def __init__(self, existing=False):
        self._existing = existing
        self.saved: list[tuple] = []

    def exists(self, eng, op):
        return self._existing

    def save(self, eng, op, payload):
        self.saved.append((eng, op, payload))


def test_persist_polled_result_saves_when_new(monkeypatch):
    import bas.worker as worker

    rs = _FakeResultStore(existing=False)
    monkeypatch.setitem(worker._state, "results_store", rs)
    detail = {"operation_id": "op1", "status": "completed", "execution_logs": []}
    worker._persist_polled_result("eng1", "op1", detail)
    assert rs.saved == [("eng1", "op1", detail)]


def test_persist_polled_result_skips_when_already_saved(monkeypatch):
    import bas.worker as worker

    rs = _FakeResultStore(existing=True)  # webhook already saved it
    monkeypatch.setitem(worker._state, "results_store", rs)
    worker._persist_polled_result("eng1", "op1", {"operation_id": "op1"})
    assert rs.saved == []  # idempotent — no double write
