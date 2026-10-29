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
    return {"number": 88, "url": URL, "headRefName": "codex/issue-42", "headRefOid": SHA, "state": "OPEN", "mergedAt": None}


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
    stale = {"heartbeat": 100.0, "phase_started": 95.0, "phase": "codex_running"}
    msg = cc.worker_status_text(stale, True, 54100.0)
    assert "состояние неизвестно" in msg
    assert "Последний записанный этап" in cc.phase_status_text(stale, 54100.0)
    assert " с" not in cc.phase_status_text(stale, 54100.0)
    active = cc.worker_status_text(stale, True, 105.0)
    assert "heartbeat свежий" in active
    assert "10 с" in cc.phase_status_text(stale, 105.0)


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
    assert result["notices"] == [("blocked:42", "Задача #42: Worker заблокирован", cc.BASE + "/issues/42")]


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
    assert cc.stage({"phase": "codex_running"}) == "Codex работает"
    assert cc.stage({"phase": "pr_created"}) == "PR создан"
    row = {"phase": "pr_created", "pr": 88, "pr_state": "OPEN",
           "checks": dict.fromkeys(cc.REQUIRED, "waiting")}
    assert cc.stage(row) == "Ожидание проверок"
    row["checks"][next(iter(cc.REQUIRED))] = "in_progress"
    assert cc.stage(row) == "Проверки выполняются"
    row["checks"] = dict.fromkeys(cc.REQUIRED, "success")
    assert cc.stage(row) == "Ожидает проверки кода"
    row["checks"][next(iter(cc.REQUIRED))] = "failure"
    assert cc.stage(row) == "Ошибка проверок"
    assert cc.stage({"phase": "blocked"}) == "Worker заблокирован"


def test_notification_write_failure_cannot_replay_or_popup(monkeypatch, tmp_path):
    notices = cc.Notifications(tmp_path)
    def fail(*args):
        raise OSError("synthetic")
    monkeypatch.setattr(cc.worker, "state_write", fail)
    assert not notices.claim("pr:88")
    assert not notices.available
    assert not notices.claim("pr:89")


@pytest.mark.parametrize("metadata,expected", [
    ({"state": "OPEN", "mergedAt": None}, "OPEN"),
    ({"state": "CLOSED", "mergedAt": None}, "CLOSED"),
    ({"state": "MERGED", "mergedAt": "2026-10-20T09:10:00Z"}, "MERGED"),
    ({}, "UNKNOWN"), ({"state": "MERGED", "mergedAt": None}, "UNKNOWN"),
    ({"state": "OPEN"}, "UNKNOWN"),
    ({"state": {}, "mergedAt": None}, "UNKNOWN"),
    ({"state": "OPEN", "mergedAt": "2026-10-20T09:10:00Z"}, "UNKNOWN"),
    ({"state": "MERGED", "mergedAt": "2026-99-99T09:10:00Z"}, "UNKNOWN"),
    ({"state": "MERGED", "mergedAt": "synthetic-private-marker"}, "UNKNOWN"),
    ({"state": "synthetic-private-marker", "mergedAt": {}}, "UNKNOWN"),
])
def test_pr_lifecycle_exact_metadata(metadata, expected):
    assert cc.pr_state(metadata) == expected


@pytest.mark.parametrize("state,label", [
    ("MERGED", "Слито в main"), ("CLOSED", "Закрыто без merge"),
    ("OPEN", "Ожидает проверки кода"), ("UNKNOWN", "Неизвестно"),
])
def test_verified_lifecycle_overrides_historical_phase(monkeypatch, tmp_path, state, label):
    fake_live(tmp_path)
    github(monkeypatch)
    original = cc.gh_json
    def query(args):
        if args == cc.PRS:
            return [{**pr(), "state": state,
                     "mergedAt": "2026-10-20T09:10:00Z" if state == "MERGED" else None}]
        return original(args)
    monkeypatch.setattr(cc, "gh_json", query)
    result = cc.collect(tmp_path)
    row = result["rows"][42]
    assert cc.stage(row) == label
    if state in {"MERGED", "CLOSED", "UNKNOWN"}:
        assert row["audit"] == label
    assert set(row["checks"].values()) == {"success"}
    assert cc.task_text(row, result["live"], 9999999999) == label
    assert "synthetic-private-marker" not in json.dumps(result)


def test_historical_task_and_fresh_current_task():
    live = {"heartbeat": 100, "phase_started": 95, "phase": "codex_running", "issue": 42}
    row = {"phase": "codex_running", "issue": 42}
    assert cc.task_text(row, live, 105) == "Codex работает"
    assert "не подтверждено" in cc.task_text(row, live, 131)
    assert "не подтверждено" in cc.task_text({**row, "issue": 41}, live, 105)
    assert cc.featured_task({42: row}, live, 105)[0] == "Текущая задача"
    assert cc.featured_task({42: row}, live, 131)[0] == "Последняя задача"
    merged = {**row, "pr": 88, "pr_state": "MERGED"}
    assert cc.featured_task({42: merged}, live, 105)[0] == "Последняя задача"
    assert cc.featured_task({}, None, 105) == ("Задач пока нет", None)


def test_worker_launch_observation_and_historical_notice():
    live = {"heartbeat": 100, "phase_started": 95, "phase": "waiting"}
    assert cc.worker_active(live, 105)
    assert not cc.worker_active(live, 131)
    assert not cc.worker_active(live, 99)
    assert not cc.worker_active(None, 105)
    notice = "Сначала проверьте уже запущенный Worker"
    assert cc.launch_notice(notice, live, 131) == notice
    assert "Ранее" in cc.launch_notice(notice, live, 105)
    assert "Сейчас Worker активен" in cc.launch_notice(notice, live, 105)
    assert cc.launch_notice("", live, 105) == ""


def test_import_does_not_spawn_or_import_tk(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not name.startswith("tkinter")
        assert not name.startswith("customtkinter")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location("headless_monitor", SCRIPT)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_pr_listing_fixed_fields_allowlist(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="[]")
    monkeypatch.setattr(cc.subprocess, "run", run)
    cc.gh_json(cc.PRS)
    assert cc.PRS[-1] == "number,url,headRefName,headRefOid,state,mergedAt"
    for args in (cc.PRS + ["--web"], cc.PRS[:-1] + ["body"],
                 ["gh", "api", "--method=GET", "--", f"repos/{cc.REPO}/pulls/88"]):
        with pytest.raises(cc.Unavailable):
            cc.gh_json(args)
    assert calls == [cc.PRS]


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_terminal_github_task_never_shows_live_phase_elapsed(state):
    live = {"heartbeat": 100, "phase_started": 95, "phase": "codex_running", "issue": 79}
    row = {"issue": 79, "phase": "codex_running", "pr": 80, "pr_state": state}
    text = cc.phase_status_text(live, 105, {79: row})
    assert "сохранён в истории" in text
    assert "10 с" not in text
    assert "Codex работает" not in text


def test_unreadable_checks_do_not_imply_waiting_or_review():
    row = {"issue": 79, "phase": "pr_created", "pr": 80, "pr_state": "OPEN"}
    assert cc.stage(row) == "Неизвестно"
    assert cc.stage({**row, "phase": "blocked"}) == "Worker заблокирован"


def test_smoke_79_80_merged_four_green(monkeypatch, tmp_path):
    status = cc.worker.LiveStatus(tmp_path)
    url = cc.BASE + "/pull/80"
    status.publish("pr_created", 79, url)
    github(monkeypatch)
    original = cc.gh_json
    def query(args):
        if args == cc.PRS:
            return [{**pr(), "number": 80, "url": url, "headRefName": "codex/issue-79",
                     "state": "MERGED", "mergedAt": "2026-10-20T09:10:00Z"}]
        if "/actions/runs?" in args[-1]:
            data = original(args)
            for run in data["workflow_runs"]:
                run["pull_requests"] = [{"number": 80}]
            return data
        return original(args)
    monkeypatch.setattr(cc, "gh_json", query)
    result = cc.collect(tmp_path)
    row = result["rows"][79]
    assert cc.stage(row) == row["audit"] == "Слито в main"
    assert set(row["checks"].values()) == {"success"}
    assert "pending" not in row["audit"]


@pytest.mark.parametrize("field,value", [
    ("url", "https://github.com/Attacker/Fork/pull/88"),
    ("headRefName", "codex/issue-999"),
    ("headRefOid", "not-a-sha"),
])
def test_lifecycle_requires_trusted_pr_identity(monkeypatch, tmp_path, field, value):
    fake_live(tmp_path)
    github(monkeypatch)
    original = cc.gh_json
    def query(args):
        if args == cc.PRS:
            return [{**pr(), "state": "MERGED", "mergedAt": "2026-10-20T09:10:00Z",
                     field: value}]
        return original(args)
    monkeypatch.setattr(cc, "gh_json", query)
    result = cc.collect(tmp_path)
    row = result["rows"][42]
    assert "pr_state" not in row
    assert cc.stage(row) == "Неизвестно"
    assert not any(key.startswith("green:") for key, _, _ in result["notices"])


@pytest.mark.parametrize("key", ["pr:88", "green:88:" + SHA, "failure:88:" + SHA, "blocked:42"])
def test_sound_once_per_event_across_restart(tmp_path, key):
    calls = []
    app = SimpleNamespace(bell=lambda: calls.append("bell"))
    assert cc.sound_notification(app, cc.Notifications(tmp_path), key)
    assert not cc.sound_notification(app, cc.Notifications(tmp_path), key)
    assert calls == ["bell"]


def test_audio_failure_is_quiet_and_never_replayed(tmp_path):
    def bell():
        raise RuntimeError("No desktop audio")
    app = SimpleNamespace(bell=bell)
    assert cc.sound_notification(app, cc.Notifications(tmp_path), "pr:88")
    assert not cc.sound_notification(app, cc.Notifications(tmp_path), "pr:88")


def test_sound_requires_main_thread(tmp_path):
    import threading
    errors = []
    store = cc.Notifications(tmp_path)
    def run():
        try:
            cc.sound_notification(SimpleNamespace(), store, "pr:88")
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert errors == ["Notifications require the UI main thread"]
    assert not store.seen


def test_missing_optional_dependency_does_not_access_state(monkeypatch, capsys):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "customtkinter":
            raise ImportError("optional")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(cc.worker, "home", lambda: pytest.fail("State accessed"))
    assert cc.main() == 1
    assert "setup_control_center_ui.ps1" in capsys.readouterr().err


def test_wrong_optional_version_does_not_access_state(monkeypatch, capsys):
    import sys
    monkeypatch.setitem(sys.modules, "customtkinter", SimpleNamespace(__version__="5.2.2"))
    monkeypatch.setattr(cc.worker, "home", lambda: pytest.fail("State accessed"))
    assert cc.main() == 1
    assert "CustomTkinter==6.0.0" in capsys.readouterr().err


def test_fresh_waiting_health_is_distinct_from_running():
    live = {"heartbeat": 100, "phase_started": 95, "phase": "waiting"}
    assert "ожидает задачи" in cc.worker_status_text(live, True, 105)
    assert "состояние неизвестно" in cc.worker_status_text(live, True, 131)


def test_ctk_ui_refresh_is_sound_only_and_worker_stays_disabled(monkeypatch, tmp_path):
    """Exercise startup, queue consumption, refresh and close with a headless CTk fake."""
    import queue
    import sys
    widgets = []
    bells = []
    callbacks = []
    protocols = {}
    monkeypatch.setattr(cc.vps, "check_once", lambda: pytest.fail("Passive VPS check"))
    monkeypatch.setattr(cc.vps.config_api, "local_directory", lambda: pytest.fail("Passive VPS config read"))
    class Widget:
        def __init__(self, *args, **kwargs):
            self.options = kwargs
            widgets.append(self)
        def configure(self, **kwargs):
            self.options.update(kwargs)
        def grid(self, **kwargs):
            pass
        pack = grid
        grid_remove = grid
        set = grid
        grid_columnconfigure = lambda *args, **kwargs: None
        grid_rowconfigure = grid_columnconfigure
        title = grid_columnconfigure
        geometry = grid_columnconfigure
        minsize = grid_columnconfigure
        update_idletasks = grid_columnconfigure
        winfo_width = lambda self: 1150
        winfo_screenwidth = lambda self: 1366
        winfo_screenheight = lambda self: 768
        bind = grid_columnconfigure
        destroy = grid_columnconfigure
        def protocol(self, name, callback):
            protocols[name] = callback
        def after(self, delay, callback):
            callbacks.append(callback)
        def bell(self):
            bells.append("bell")
        def mainloop(self):
            callbacks.pop(0)()  # Duplicate refresh cannot ring again.
            protocols["WM_DELETE_WINDOW"]()
    fake = SimpleNamespace(__version__="6.0.0", CTk=Widget, CTkFrame=Widget,
                           CTkButton=Widget, CTkLabel=Widget, CTkScrollableFrame=Widget,
                           CTkOptionMenu=Widget, set_appearance_mode=lambda value: None,
                           set_default_color_theme=lambda value: None)
    monkeypatch.setitem(sys.modules, "customtkinter", fake)
    live = {"heartbeat": 100, "phase_started": 95, "phase": "pr_created", "issue": 42,
            "jobs": {42: {"phase": "pr_created", "pr_url": URL}}, "events": []}
    row = {"issue": 42, **live["jobs"][42], "pr": 88, "pr_state": "OPEN", "link": URL,
           "checks": dict.fromkeys(cc.REQUIRED, "success")}
    result = {"live": live, "rows": {42: row}, "online": True,
              "notices": [("pr:88", "PR создан", URL), ("green:88:" + SHA, "Все четыре успешны", URL)]}
    queues = [queue.Queue(), queue.Queue(), queue.Queue()]
    queues[0].put(result)
    queues[1].put(live)
    monkeypatch.setattr(cc.queue, "Queue", lambda **kwargs: queues.pop(0))
    monkeypatch.setattr(cc.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(cc.worker, "home", lambda: tmp_path)
    monkeypatch.setattr(cc.time, "time", lambda: 105)
    monkeypatch.setattr(cc.webbrowser, "open", lambda *args: pytest.fail("Automatic browser"))
    assert cc.main() == 0
    assert bells == ["bell", "bell"]
    assert any(w.options.get("text") == "Worker запущен" and w.options.get("state") == "disabled" for w in widgets)
    assert any("PR создан" in w.options.get("text", "") for w in widgets)
    assert not cc.Notifications(tmp_path).claim("pr:88")


def test_launcher_and_explicit_setup_contract():
    launcher = SCRIPT.with_name("start_control_center.bat").read_text(encoding="utf-8")
    setup = SCRIPT.with_name("setup_control_center_ui.ps1").read_text(encoding="utf-8")
    assert "control-center-venv\\Scripts\\python.exe" in launcher
    assert 'if not exist "%UI_PYTHON%"' in launcher
    assert '"%UI_PYTHON%" "%~dp0control_center.py"' in launcher
    assert "echo Run explicitly:" in launcher
    assert "pip" not in launcher and "py -3.13" not in launcher
    assert "CustomTkinter==6.0.0" in setup
    assert "https://pypi.org/simple" in setup and "--isolated" in setup
    assert "-m venv $uiVenv" in setup and "-I -m pip" in setup
    assert "include-system-site-packages = false" in setup
    for forbidden in ("RunAs", "ExecutionPolicy", "Invoke-WebRequest", "uv sync", "--user", "--extra-index-url"):
        assert forbidden not in setup


def test_presentation_contains_no_popups_or_focus_steals():
    import ast
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden = {"Toplevel", "CTkToplevel", "messagebox", "showinfo", "showerror",
                 "focus_force", "attributes", "grab_set"}
    assert not any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree))
    assert SCRIPT.read_text(encoding="utf-8").count("app.bell()") == 1


def test_timeline_contains_only_observed_events():
    live = {"events": [{"issue": 42, "phase": "claimed", "timestamp": 100},
                       {"issue": 99, "phase": "codex_running", "timestamp": 101}]}
    text = cc.observed_timeline(live, 42)
    assert cc.PHASE_TEXT["claimed"] in text
    assert cc.PHASE_TEXT["codex_running"] not in text
    assert "%" not in text
