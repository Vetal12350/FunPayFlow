"""Read-only UI integration: no Windows GUI, network, credentials or worker start."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ui_integration_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


project = load("project_status")
cc = load("control_center")
SEN = "Vetal12350/SenReader"
FUNPAY = "Vetal12350/FunPayFlow"


def write_status(root, **data):
    base = {
        "version": 1, "phase": "claimed", "heartbeat": project.time.time(),
        "current_repo": SEN, "project_jobs": {
            f"{SEN}#1": {"phase": "pr_created", "pr_url": f"https://github.com/{SEN}/pull/7"},
            f"{FUNPAY}#1": {"phase": "blocked", "pr_url": f"https://github.com/{FUNPAY}/pull/8"},
        },
    }
    base.update(data)
    (root / "live_status.json").write_text(json.dumps(base), encoding="utf-8")


def test_projects_are_isolated_even_with_identical_issue_numbers(tmp_path):
    write_status(tmp_path)
    s = project.read_project_status(tmp_path)
    assert s["live"] and s["active_repo"] == SEN
    assert set(s["jobs"]) == {SEN, FUNPAY}
    sr = project.project_rows(s, SEN)
    fr = project.project_rows(s, FUNPAY)
    assert set(sr) == {1} and set(fr) == {1}
    assert sr[1]["link"] == f"https://github.com/{SEN}/pull/7"
    assert fr[1]["link"] == f"https://github.com/{FUNPAY}/pull/8"
    assert sr[1]["phase"] == "pr_created"


def test_link_validation_rejects_other_repos_and_injections():
    assert project.safe_project_link(SEN, f"https://github.com/{SEN}/issues/2")
    assert project.safe_project_link(SEN, f"https://github.com/{SEN}/pull/5")
    for url in [
        "javascript:alert(1)", "https://github.com/attacker/repo/pull/2",
        f"https://github.com/{SEN}/pull/0", f"https://github.com/{SEN}/pull/1?token=x",
        f"https://github.com/{SEN}/pull/1/", f"https://github.com/{SEN}/issues/%2e",
        f"https://github.com/{FUNPAY}/pull/12",
    ]:
        assert not project.safe_project_link(SEN, url)


def test_malicious_untrusted_metadata_is_stripped(tmp_path):
    write_status(tmp_path, project_jobs={
        f"{SEN}#3": {
            "phase": "pr_created",
            "pr_url": "https://github.com/attacker/repo/pull/5",
            "secret": "never_display_this",
        },
        "evil/repo#1": {"phase": "claimed"},
        f"{FUNPAY}#4": {"phase": "bad"},
    })
    s = project.read_project_status(tmp_path)
    assert s["jobs"][SEN][3] == {"phase": "pr_created"}
    assert s["jobs"][FUNPAY] == {}
    assert "never_display_this" not in str(s)


@pytest.mark.parametrize("kwargs", [
    {"heartbeat": 0}, {"heartbeat": 1e30}, {"phase": "evil"},
    {"version": 999}, {"project_jobs": ["invalid"]},
])
def test_stale_or_invalid_status_never_claimed_active(tmp_path, kwargs):
    write_status(tmp_path, **kwargs)
    s = project.read_project_status(tmp_path)
    assert not s["live"]


def test_universal_launch_requires_reviewed_installed_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "WINDOWS", True)
    monkeypatch.setattr(cc.sys, "version_info", (3, 13))
    local = tmp_path / "source" / "scripts"
    local.mkdir(parents=True)
    with pytest.raises(cc.Unavailable):
        cc.start_worker(tmp_path, universal=True)
    for file in ["codex_issue_worker.py", "worker_projects.py", "universal_codex_worker.py"]:
        (local / file).write_bytes((SCRIPTS / file).read_bytes())
    monkeypatch.setattr(cc.worker, "other_workers", lambda: False)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda args, **kwargs: calls.append((args,kwargs)) or "mocked")
    assert cc.start_worker(tmp_path, universal=True) == "mocked"
    assert calls[0][0][1].endswith("universal_codex_worker.py")
    assert calls[0][1]["shell"] is False
    # Any changed installed dependency must prevent launch.
    (local / "worker_projects.py").write_text("# altered")
    with pytest.raises(cc.Unavailable):
        cc.start_worker(tmp_path, universal=True)
    assert len(calls) == 1


def test_control_center_still_supports_legacy_start(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "WINDOWS", True)
    monkeypatch.setattr(cc.sys, "version_info", (3, 13))
    local = tmp_path / "source" / "scripts"
    local.mkdir(parents=True)
    (local / "codex_issue_worker.py").write_bytes((SCRIPTS / "codex_issue_worker.py").read_bytes())
    monkeypatch.setattr(cc.worker, "other_workers", lambda: False)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda args, **kwargs: calls.append(args) or "legacy")
    assert cc.start_worker(tmp_path) == "legacy"
    assert calls[0][1].endswith("codex_issue_worker.py")


def test_no_auto_launch_on_import():
    assert callable(cc.main)
    assert "customtkinter" not in cc.main.__globals__
    assert callable(project.read_project_status)


def test_senreader_queue_is_owner_scoped_and_read_only(monkeypatch, tmp_path):
    seen = []
    def fake_gh(argv):
        seen.append(argv)
        return [
            {"number": 8, "title": "[Codex] Implement alerts",
             "author": {"login": "Vetal12350"}, "labels": [{"name": "codex-local"}]},
            {"number": 9, "title": "[Codex] Untrusted",
             "author": {"login": "attacker"}, "labels": [{"name": "codex-local"}]},
            {"number": 10, "title": "Arbitrary untrusted title",
             "author": {"login": "Vetal12350"}, "labels": [{"name": "codex-local"}]},
        ]
    monkeypatch.setattr(cc, "gh_json", fake_gh)
    rows, online = cc.collect_senreader(tmp_path)
    assert online and set(rows) == {8}
    assert rows[8]["link"] == f"https://github.com/{SEN}/issues/8"
    assert seen == [cc.SENREADER_ISSUES]


def test_offline_senreader_queue_preserves_only_verified_local_status(monkeypatch, tmp_path):
    write_status(tmp_path)
    monkeypatch.setattr(cc, "gh_json", lambda _: (_ for _ in ()).throw(cc.Unavailable("offline")))
    rows, online = cc.collect_senreader(tmp_path)
    assert not online
    assert set(rows) == {1}
    assert rows[1]["pr_url"] == f"https://github.com/{SEN}/pull/7"
