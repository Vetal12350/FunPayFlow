"""Offline multi-repo orchestration tests. No real GitHub, Codex, or VPS calls."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "universal_codex_worker_test_module", SCRIPTS / "universal_codex_worker.py",
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

FUNPAY = "Vetal12350/FunPayFlow"
SEN = "Vetal12350/SenReader"


def task(number, title="[Codex] test title", owner="Vetal12350", label="codex-local"):
    return {
        "number": number, "title": title,
        "body": "Implement an offline regression check.",
        "author": {"login": owner},
        "labels": [{"name": label}],
    }


def test_queue_per_repo_and_owner_validation(monkeypatch):
    observed = []
    def fake_query(argv):
        observed.append(argv)
        return [task(11), task(12), task(13, owner="attacker"),
                task(14, label="wrong"), task(15, title="not authorized")]
    monkeypatch.setattr(worker, "github_json", fake_query)
    state = {worker.task_key(SEN, 11): {"status": "blocked"}}
    result = worker.read_queue(worker.profile_for(SEN), state)
    assert [i["number"] for i in result] == [12]
    assert observed[0][4] == SEN
    assert "--label" in observed[0]


def test_queue_never_processes_other_projects_state(monkeypatch):
    monkeypatch.setattr(worker, "github_json", lambda argv: [task(11)])
    old = {worker.task_key(FUNPAY, 11): {"status": "started"}}
    assert [t["number"] for t in worker.read_queue(worker.profile_for(SEN), old)] == [11]


def test_queue_fails_closed_on_unexpected_response(monkeypatch):
    monkeypatch.setattr(worker, "github_json", lambda argv: {"invalid": []})
    with pytest.raises(worker.legacy.WorkerError):
        worker.read_queue(worker.profile_for(SEN), {})


def test_namespaced_migration_keeps_claimed_and_blocked(tmp_path):
    original = {
        "89": {"status": "started"},
        "124": {"status": "blocked", "reason": "do not retry"},
    }
    worker.legacy.state_write(tmp_path / "state.json", original)
    next_state = worker.load_state(tmp_path)
    assert next_state[worker.task_key(FUNPAY, 89)]["status"] == "started"
    assert next_state[worker.task_key(FUNPAY, 124)]["status"] == "blocked"
    assert (tmp_path / worker.LEGACY_BACKUP).exists()
    assert worker.legacy.state_read(tmp_path / worker.LEGACY_BACKUP) == original
    assert worker.load_state(tmp_path) == next_state
    assert "89" not in worker.load_state(tmp_path)


def test_corrupt_state_never_overwritten(tmp_path):
    raw = {"bad": {"status": "started"}}
    worker.legacy.state_write(tmp_path / "state.json", raw)
    with pytest.raises(worker.legacy.WorkerError):
        worker.load_state(tmp_path)
    assert worker.legacy.state_read(tmp_path / "state.json") == raw
    assert not (tmp_path / worker.LEGACY_BACKUP).exists()


def test_mixed_state_detects_collision():
    raw = {
        "89": {"status": "started"},
        worker.task_key(FUNPAY, 89): {"status": "pr_created"},
    }
    with pytest.raises(worker.legacy.WorkerError, match="Duplicate"):
        worker.normalize_state(raw)


def test_status_keeps_funpay_legacy_compatibility_without_leaking_senreader_issue(tmp_path):
    status = worker.MultiStatus(tmp_path)
    status.publish("claimed", FUNPAY, 13)
    status.publish("pr_created", FUNPAY, 13,
                   "https://github.com/Vetal12350/FunPayFlow/pull/13")
    status.publish("claimed", SEN, 13)
    status.publish("pr_created", SEN, 13,
                   "https://github.com/Vetal12350/SenReader/pull/17")
    data = worker.legacy.state_read(tmp_path / "live_status.json")
    assert data["version"] == 1
    assert data["issue"] is None
    assert data["current_repo"] == SEN
    assert set(data["jobs"]) == {"13"}
    assert data["jobs"]["13"]["pr_url"].endswith("/FunPayFlow/pull/13")
    assert data["project_jobs"][worker.task_key(SEN, 13)]["pr_url"].endswith("/SenReader/pull/17")
    assert all("token" not in str(value).lower() for value in data.values())


def test_status_disallows_cross_repo_and_untrusted_pr_urls(tmp_path):
    status = worker.MultiStatus(tmp_path)
    status.publish("pr_created", SEN, 7,
                   "https://github.com/Vetal12350/FunPayFlow/pull/4")
    data = worker.legacy.state_read(tmp_path / "live_status.json")
    assert "pr_url" not in data["project_jobs"][worker.task_key(SEN, 7)]
    with pytest.raises(worker.legacy.WorkerError):
        status.publish("claimed", "attacker/repo", 1)


def test_prompt_selects_correct_sandbox_policy():
    sen = worker.prompt_for(worker.profile_for(SEN), task(12))
    assert "Flutter Android" in sen
    assert "No" not in sen.split("TASK TITLE:", 1)[0] or "no" in sen.lower()
    assert "FunPay or production VPS" in sen
    assert "Do not commit, push" in sen
    funpay = worker.prompt_for(worker.profile_for(FUNPAY), task(12))
    assert funpay == worker.legacy.prompt_for(task(12))
    assert "FunPayFlow SOURCE CHECKOUT" in funpay


def test_senreader_does_not_run_uv_and_has_different_checkout(monkeypatch, tmp_path):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="", returncode=0)
    monkeypatch.setattr(worker.legacy, "run", fake_run)
    monkeypatch.setattr(worker.legacy, "git", lambda *a, **kw: "")
    monkeypatch.setattr(worker, "run_codex", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "checked_changes", lambda *a, **kw: ["lib/main.dart"])
    monkeypatch.setattr(worker, "stage_and_pr",
                        lambda checkout, item, profile: f"https://github.com/{profile.repo}/pull/77")
    profile = worker.profile_for(SEN)
    url = worker.execute_task(profile, task(7), tmp_path)
    assert url.endswith("/SenReader/pull/77")
    assert any("SenReader.git" in " ".join(command) for command in calls)
    assert all(command[0] != "uv" for command in calls)
    assert (tmp_path / "jobs" / "senreader").exists()


def test_process_once_is_idempotent_across_two_repos(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(worker, "read_queue",
                        lambda profile, state: [
                            item for item in [task(30)] if worker.task_key(profile.repo, 30) not in state
                        ])
    monkeypatch.setattr(worker, "execute_task",
                        lambda profile, item, root, status: (
                            calls.append(profile.repo) or
                            f"https://github.com/{profile.repo}/pull/30"
                        ))
    monkeypatch.setattr(worker.legacy, "run", lambda *args, **kw: SimpleNamespace(stdout=""))
    assert worker.process_once(tmp_path)
    assert worker.process_once(tmp_path)
    assert not worker.process_once(tmp_path)
    assert calls == [FUNPAY, SEN]
    st = worker.load_state(tmp_path)
    assert st[worker.task_key(FUNPAY, 30)]["status"] == "pr_created"
    assert st[worker.task_key(SEN, 30)]["status"] == "pr_created"


def test_claim_is_durable_before_codex_failure_and_never_retried(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(worker, "read_queue", lambda profile, state: [
        task(9) if worker.task_key(profile.repo, 9) not in state else None
    ] if profile.repo == SEN and worker.task_key(profile.repo, 9) not in state else [])
    def blocked(profile, item, root, status):
        calls.append(worker.load_state(root)[worker.task_key(SEN, 9)]["status"])
        raise worker.legacy.WorkerError("mock failure")
    monkeypatch.setattr(worker, "execute_task", blocked)
    monkeypatch.setattr(worker.legacy, "run", lambda *args, **kw: SimpleNamespace(stdout=""))
    assert worker.process_once(tmp_path)
    assert calls == ["started"]
    assert worker.load_state(tmp_path)[worker.task_key(SEN, 9)]["status"] == "blocked"
    assert not worker.process_once(tmp_path)


@pytest.mark.parametrize("path", [
    "lib/bad_token.txt", "lib/.secret", ".github/workflows/android-apk.yml",
    "src/funpayflow/main.py", "lib/../../secrets",
])
def test_senreader_source_policy_is_strict(path):
    assert not worker.allowed_path(worker.profile_for(SEN), path)


def test_checked_changes_rejects_symlink_and_unapproved_path(monkeypatch, tmp_path):
    monkeypatch.setattr(worker.legacy, "run", lambda *args, **kw: SimpleNamespace(
        stdout="?? .github/workflows/evil.yml\x00"))
    with pytest.raises(worker.legacy.WorkerError):
        worker.checked_changes(tmp_path, worker.profile_for(SEN))
    monkeypatch.setattr(worker.legacy, "run", lambda *args, **kw: SimpleNamespace(
        stdout="?? lib/target.dart\x00"))
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "target.dart").symlink_to(tmp_path / "outside.dart")
    with pytest.raises(worker.legacy.WorkerError):
        worker.checked_changes(tmp_path, worker.profile_for(SEN))


def test_stage_refuses_bad_git_index_mode(monkeypatch, tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.dart").write_text("main() {}")
    monkeypatch.setattr(worker, "checked_changes", lambda *args: ["lib/main.dart"])
    monkeypatch.setattr(worker.legacy, "git", lambda _cwd, *args, **kwargs:
                        "120000 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 0\tlib/main.dart"
                        if args[:2] == ("ls-files", "--stage") else
                        "lib/main.dart" if args[:3] == ("diff", "--cached", "--name-only") else "")
    with pytest.raises(worker.legacy.WorkerError, match="index mode"):
        worker.stage_and_pr(tmp_path, task(11), worker.profile_for(SEN))


def test_main_check_mode_never_starts_worker(monkeypatch):
    monkeypatch.setattr(worker.legacy, "check_tools", lambda: None)
    monkeypatch.setattr(worker.legacy, "home", lambda: pytest.fail("should not create dirs"))
    assert worker.main(["--check"]) == 0
