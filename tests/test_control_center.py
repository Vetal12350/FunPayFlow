"""Offline monitor model tests: no Tk window, gh, Codex, or production endpoints."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "control_center.py"
spec = importlib.util.spec_from_file_location("control_center_tests", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
URL = cc.BASE + "/pull/88"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external process")
    monkeypatch.setattr(cc.subprocess, "run", forbidden)
    monkeypatch.setattr(cc.subprocess, "Popen", forbidden)


def pr():
    return {"number": 88, "url": URL, "headRefName": "codex/issue-42", "headRefOid": SHA}


def fake_live(tmp_path, phase="pr_created"):
    status = cc.worker.LiveStatus(tmp_path)
    status.publish(phase, 42, URL if phase == "pr_created" else "")
    return status


def github(monkeypatch, statuses=None, runs=None):
    if statuses is None:
        statuses = dict.fromkeys(cc.REQUIRED, "success")
    if runs is None:
        runs = [dict(id=i, run_attempt=1, path=path, head_sha=SHA,
                     event="pull_request", pull_requests=[{"number": 88}])
                for i, path in enumerate(sorted(set(cc.REQUIRED.values())), 1)]
    def query(args):
        if args == cc.ISSUES:
            return [{"number": 42, "title": "[Codex] synthetic", "author": {"login": cc.worker.OWNER},
                     "labels": [{"name": cc.worker.LABEL}]}]
        if args == cc.PRS:
            return [pr()]
        endpoint = args[-1]
        if "/actions/runs?" in endpoint:
            return {"total_count": len(runs), "workflow_runs": runs}
        run_id = int(endpoint.split("/runs/")[1].split("/")[0])
        run = next(r for r in runs if r["id"] == run_id)
        jobs = [{"name": name, "status": "completed", "conclusion": state}
                for name, state in statuses.items() if cc.REQUIRED[name] == run["path"]]
        return {"total_count": len(jobs), "jobs": jobs}
    monkeypatch.setattr(cc, "gh_json", query)


def test_import_has_no_tk_side_effects():
    assert callable(cc.main)
    assert "tkinter" not in cc.main.__globals__


def test_not_queued_empty_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "gh_json", lambda args: [])
    result = cc.collect(tmp_path)
    assert result["online"] and result["rows"] == {} and result["notices"] == []


def test_actual_github_queue_without_local_job(monkeypatch, tmp_path):
    github(monkeypatch)
    result = cc.collect(tmp_path)
    assert result["rows"][42]["phase"] == "Queued"
    assert "pr" not in result["rows"][42]  # Branch name does not authenticate a worker PR.


@pytest.mark.parametrize("phase", ["claimed", "clone", "dependencies", "codex_running", "branch_pr", "blocked"])
def test_observed_phases_only(tmp_path, phase, monkeypatch):
    fake_live(tmp_path, phase)
    monkeypatch.setattr(cc, "gh_json", lambda args: [])
    result = cc.collect(tmp_path)
    assert result["rows"][42]["phase"] == phase
    assert result["live"]["events"][-1]["phase"] == phase
    assert cc.health(result["live"], result["live"]["heartbeat"] + 1) == "heartbeat observed"


def test_stale_future_stopped_and_missing_heartbeat(tmp_path):
    status = fake_live(tmp_path)
    live = cc.read_live(tmp_path)
    assert "stale" in cc.health(live, live["heartbeat"] + 31)
    assert "stale" in cc.health(live, live["heartbeat"] - 1)
    assert "unknown" in cc.health(None, 100)
    status.publish("stopped")
    live = cc.read_live(tmp_path)
    assert cc.health(live, live["heartbeat"]) == "stopped"


def test_github_offline_retains_local_status(monkeypatch, tmp_path):
    fake_live(tmp_path, "codex_running")
    def offline(args):
        raise cc.Unavailable("offline")
    monkeypatch.setattr(cc, "gh_json", offline)
    result = cc.collect(tmp_path)
    assert not result["online"]
    assert result["rows"][42]["phase"] == "codex_running"


def test_four_exact_jobs_green_still_pending_audit(monkeypatch, tmp_path):
    fake_live(tmp_path)
    github(monkeypatch)
    result = cc.collect(tmp_path)
    assert set(result["rows"][42]["checks"]) == set(cc.REQUIRED)
    assert result["rows"][42]["audit"] == "pending human/ChatGPT review"
    assert any(key.startswith("green:") for key, _, _ in result["notices"])


@pytest.mark.parametrize("name", list(cc.REQUIRED))
@pytest.mark.parametrize("state", ["failure", "skipped", "neutral", "cancelled"])
def test_each_required_check_must_succeed(monkeypatch, tmp_path, name, state):
    fake_live(tmp_path)
    states = dict.fromkeys(cc.REQUIRED, "success")
    states[name] = state
    github(monkeypatch, states)
    result = cc.collect(tmp_path)
    assert not any(key.startswith("green:") for key, _, _ in result["notices"])
    if state in cc.FAILURES:
        assert result["rows"][42]["audit"] == "blocked (checks)"
        assert any(key.startswith("failure:") for key, _, _ in result["notices"])


def test_missing_qa_waits_and_wrong_sha_or_pr_never_green(monkeypatch, tmp_path):
    fake_live(tmp_path)
    states = dict.fromkeys(list(cc.REQUIRED)[:2], "success")
    github(monkeypatch, states)
    assert cc.collect(tmp_path)["rows"][42]["checks"]["Owner journeys and regression QA"] == "waiting"
    for field, value in [("head_sha", "b" * 40), ("pull_requests", [{"number": 99}]), ("event", "push")]:
        runs = [dict(id=1, run_attempt=1, path=".github/workflows/ci.yml", head_sha=SHA,
                     event="pull_request", pull_requests=[{"number": 88}])]
        runs[0][field] = value
        github(monkeypatch, runs=runs)
        assert set(cc.checks_for(pr()).values()) == {"waiting"}


def test_merged_pr_workflow_with_empty_associations_uses_exact_sha_branch_and_repo(monkeypatch, tmp_path):
    """GitHub can clear pull_requests on completed runs after PR merge."""
    fake_live(tmp_path)
    paths = sorted(set(cc.REQUIRED.values()))
    runs = [
        dict(id=i, run_attempt=1, path=path, head_sha=SHA, event="pull_request",
             pull_requests=[], head_branch="codex/issue-42",
             repository={"full_name": cc.REPO},
             head_repository={"full_name": cc.REPO})
        for i, path in enumerate(paths, 1)
    ]
    github(monkeypatch, runs=runs)
    result = cc.collect(tmp_path)
    assert set(result["rows"][42]["checks"].values()) == {"success"}
    assert result["rows"][42]["audit"] == "pending human/ChatGPT review"


@pytest.mark.parametrize("bad_field,bad_value", [
    ("head_branch", "codex/issue-999"),
    ("repository", {"full_name": "Attacker/Fork"}),
    ("head_repository", {"full_name": "Attacker/Fork"}),
    ("head_sha", "b" * 40),
    ("event", "push"),
    ("pull_requests", [{"number": 999}]),
])
def test_empty_association_fallback_rejects_wrong_identity(monkeypatch, bad_field, bad_value):
    run = dict(id=1, run_attempt=1, path=".github/workflows/ci.yml",
               head_sha=SHA, event="pull_request", pull_requests=[],
               head_branch="codex/issue-42",
               repository={"full_name": cc.REPO},
               head_repository={"full_name": cc.REPO})
    run[bad_field] = bad_value
    github(monkeypatch, runs=[run])
    assert set(cc.checks_for(pr()).values()) == {"waiting"}


def test_missing_workflow_association_does_not_count_as_success(monkeypatch):
    run = dict(id=1, run_attempt=1, path=".github/workflows/ci.yml",
               head_sha=SHA, event="pull_request", head_branch="codex/issue-42",
               repository={"full_name": cc.REPO},
               head_repository={"full_name": cc.REPO})
    github(monkeypatch, runs=[run])
    with pytest.raises(cc.Unavailable, match="association"):
        cc.checks_for(pr())


def test_stale_worker_does_not_display_elapsed_running_time():
    stale = {"heartbeat": 100.0, "phase_started": 95.0, "phase": "waiting"}
    msg = cc.worker_status_text(stale, True, 54100.0)
    assert "stale" in msg and "last recorded phase: waiting" in msg
    assert "phase elapsed" not in msg
    active = cc.worker_status_text(stale, True, 105.0)
    assert "heartbeat observed" in active and "phase elapsed 10s" in active


def test_latest_run_and_exact_job_names(monkeypatch):
    runs = [dict(id=n, run_attempt=1, path=".github/workflows/ci.yml", head_sha=SHA,
                 event="pull_request", pull_requests=[{"number": 88}]) for n in (1, 2)]
    github(monkeypatch, {"offline-tests (ubuntu-latest) extra": "success"}, runs)
    # Test wrapper cannot invent a mapping for a lookalike job name.
    original = cc.gh_json
    def query(args):
        if "/attempts/" in args[-1]:
            assert "/runs/2/" in args[-1]
            return {"total_count": 1, "jobs": [{"name": "offline-tests (ubuntu-latest) extra",
                                                 "status": "completed", "conclusion": "success"}]}
        return original(args)
    monkeypatch.setattr(cc, "gh_json", query)
    assert set(cc.checks_for(pr()).values()) == {"waiting"}


@pytest.mark.parametrize("data", [None, {}, {"total_count": 101, "jobs": []},
                                    {"total_count": 2, "jobs": []}])
def test_truncated_or_malformed_pages_fail_closed(data):
    with pytest.raises(cc.Unavailable):
        cc.bounded_page(data, "jobs")


def test_notifications_persist_and_bad_storage_fails_closed(tmp_path):
    notices = cc.Notifications(tmp_path)
    assert notices.claim("pr:88")
    assert not notices.claim("pr:88")
    assert not cc.Notifications(tmp_path).claim("pr:88")
    (tmp_path / "monitor_notifications.json").write_text("invalid")
    assert not cc.Notifications(tmp_path).claim("pr:89")


def test_status_sanitizes_payload_and_notifications(monkeypatch, tmp_path):
    fake_live(tmp_path, "blocked")
    path = tmp_path / "live_status.json"
    value = json.loads(path.read_text())
    value["task_body"] = "synthetic-private-marker"
    value["jobs"]["42"]["raw_log"] = "synthetic-private-marker"
    value["events"].append({"phase": "synthetic-private-marker", "issue": 42, "timestamp": 100})
    path.write_text(json.dumps(value))
    monkeypatch.setattr(cc, "gh_json", lambda args: [])
    result = cc.collect(tmp_path)
    assert "synthetic-private-marker" not in json.dumps(result)
    assert result["notices"] == [("blocked:42", "Issue #42: worker blocked", cc.BASE + "/issues/42")]


@pytest.mark.parametrize("text", ["[]", "{", '{"version": 99}', "x" * 65537], ids=["list", "truncated", "version", "oversized"])
def test_bad_telemetry_unavailable(tmp_path, text):
    (tmp_path / "live_status.json").write_text(text)
    assert cc.read_live(tmp_path) is None


def test_read_only_gh_allowlist_and_timeouts(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="[]")
    monkeypatch.setattr(cc.subprocess, "run", run)
    assert cc.gh_json(cc.ISSUES) == []
    cc.api(f"runs?head_sha={SHA}&event=pull_request&per_page=100")
    assert all(not kw["shell"] and kw["timeout"] == 20 for _, kw in calls)
    for args in (["gh", "issue", "create"], ["gh", "api", "--method=POST", "--", "anything"],
                 ["gh", "api", "--method=GET", "--", "repos/evil/actions/runs"]):
        with pytest.raises(cc.Unavailable):
            cc.gh_json(args)
    assert len(calls) == 2


def test_gh_timeout_sanitized(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic-private-marker", 20)
    monkeypatch.setattr(cc.subprocess, "run", run)
    with pytest.raises(cc.Unavailable, match="GitHub unavailable"):
        cc.gh_json(cc.ISSUES)


def test_launch_existing_worker_and_uncertainty_never_spawns(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "WINDOWS", True)
    installed = tmp_path / "source" / "scripts" / "codex_issue_worker.py"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(Path(cc.worker.__file__).read_bytes())
    for mode in ("existing", "unknown"):
        def inventory():
            if mode == "unknown":
                raise cc.worker.WorkerError("inventory unavailable")
            return True
        monkeypatch.setattr(cc.worker, "other_workers", inventory)
        with pytest.raises(cc.Unavailable):
            cc.start_worker(tmp_path)


def test_launch_only_matching_installed_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "WINDOWS", True)
    installed = tmp_path / "source" / "scripts" / "codex_issue_worker.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("# older worker")
    with pytest.raises(cc.Unavailable, match="trusted source update"):
        cc.start_worker(tmp_path)
    installed.write_bytes(Path(cc.worker.__file__).read_bytes())
    monkeypatch.setattr(cc.worker, "other_workers", lambda: False)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or "child")
    assert cc.start_worker(tmp_path) == "child"
    assert calls[0][0][1:] == [str(installed), "--watch", "--interval", "90"]
    assert calls[0][1]["shell"] is False


def test_safe_links_only():
    assert cc.safe_link(URL)
    for value in ("javascript:alert(1)", cc.BASE + "/pull/88?token=x", "https://evil.test", None):
        assert not cc.safe_link(value)


@pytest.mark.parametrize("value, expected", [
    ("https://github.com/Vetal12350/FunPayFlow/issues/42", True),
    ("http://github.com/Vetal12350/FunPayFlow/issues/42", False),
    ("https://github.com.evil.test/Vetal12350/FunPayFlow/issues/42", False),
    ("https://user@github.com/Vetal12350/FunPayFlow/issues/42", False),
    ("https://github.com/Vetal12350/OtherRepo/issues/42", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/42?token=x", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/42#comment", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/%2e%2e/42", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/42/", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/4 2", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/4\t2", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/4\n2", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/42\n", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/0", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/-1", False),
    ("https://github.com/Vetal12350/FunPayFlow/issues/01", False),
])
def test_safe_issue_links_strict_allowlist(value, expected):
    assert cc.safe_link(value) is expected


def test_stage_comes_only_from_observed_checks():
    assert cc.stage({"phase": "codex_running"}) == "Codex running"
    assert cc.stage({"phase": "pr_created"}) == "PR created"
    states = dict.fromkeys(cc.REQUIRED, "waiting")
    assert cc.stage({"phase": "pr_created", "checks": states}) == "CI / QA waiting"
    states[next(iter(states))] = "in_progress"
    assert cc.stage({"phase": "pr_created", "checks": states}) == "CI / QA running"
    states = dict.fromkeys(cc.REQUIRED, "success")
    assert cc.stage({"phase": "pr_created", "checks": states}) == "Audit pending"
    states[next(iter(states))] = "failure"
    assert cc.stage({"phase": "pr_created", "checks": states}) == "Audit blocked"
    assert cc.stage({"phase": "blocked", "checks": states}) == "Worker blocked"


def test_notification_write_failure_cannot_replay_or_popup(monkeypatch, tmp_path):
    notices = cc.Notifications(tmp_path)
    def fail(*args):
        raise OSError("synthetic")
    monkeypatch.setattr(cc.worker, "state_write", fail)
    assert not notices.claim("pr:88")
    assert not notices.available
    assert not notices.claim("pr:89")
