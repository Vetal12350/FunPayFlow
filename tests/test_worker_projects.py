"""Offline policy tests for an opt-in universal worker: zero network or processes."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "worker_projects.py"
spec = importlib.util.spec_from_file_location("worker_projects_tests", SCRIPT)
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)

PROFILES = project.PROFILES
allowed_path = project.allowed_path
choose_next_repo = project.choose_next_repo
migrate_funpay_ledger = project.migrate_funpay_ledger
profile_for = project.profile_for
task_key = project.task_key

FUNPAY = "Vetal12350/FunPayFlow"
SEN = "Vetal12350/SenReader"


def test_registry_only_contains_explicit_projects_and_is_readonly():
    assert set(PROFILES) == {FUNPAY, SEN}
    assert profile_for(SEN).toolchain == "flutter-github-ci"
    assert profile_for(FUNPAY).toolchain == "python-uv"
    with pytest.raises(TypeError):
        PROFILES["evil/repo"] = profile_for(SEN)


@pytest.mark.parametrize("repo", ["another/repo", "Vetal12350/Other", "Vetal12350/SenReader/extra", "", None])
def test_unknown_repo_fails_closed(repo):
    with pytest.raises(ValueError):
        profile_for(repo)


def test_profiles_have_distinct_remotes_and_no_extra_power_from_issue():
    assert profile_for(SEN).remote == "https://github.com/Vetal12350/SenReader.git"
    assert profile_for(FUNPAY).remote == "https://github.com/Vetal12350/FunPayFlow.git"
    assert profile_for(SEN).branch == "main"
    assert profile_for(FUNPAY).branch == "main"
    assert all(not p.startswith(".github/") for p in profile_for(SEN).allowed_prefixes)


@pytest.mark.parametrize("path", [
    "lib/main.dart", "lib/data/remanga_source.dart", "test/source_test.dart",
    "scripts/prepare_android.py", "README.md", "pubspec.yaml", "analysis_options.yaml",
])
def test_senreader_permits_expected_source_files(path):
    assert allowed_path(profile_for(SEN), path)


@pytest.mark.parametrize("path", [
    "", "/lib/main.dart", "../lib/main.dart", "lib/../.env", "lib//main.dart",
    "./lib/main.dart", "lib/.settings/foo.dart", "lib/secret.txt",
    "lib/credentials.json", "lib/api_token.dart", ".github/workflows/evil.yml",
    "pubspec.lock", "src/funpayflow/main.py", "lib\\main.dart",
    "lib/a\x00.dart", "lib/a\n.dart", "lib/a:b.dart",
])
def test_senreader_rejects_untrusted_paths(path):
    assert not allowed_path(profile_for(SEN), path)


def test_funpay_scope_is_preserved():
    p = profile_for(FUNPAY)
    assert allowed_path(p, "src/funpayflow/main.py")
    assert allowed_path(p, "tests/test_something.py")
    assert allowed_path(p, "scripts/control_center.py")
    assert allowed_path(p, "README.md")
    assert not allowed_path(p, "lib/main.dart")
    assert not allowed_path(p, ".github/workflows/ci.yml")
    assert not allowed_path(p, "src/funpayflow/.env")


def test_paths_cannot_cross_between_profiles():
    assert not allowed_path(profile_for(FUNPAY), "lib/core/models.dart")
    assert not allowed_path(profile_for(SEN), "src/funpayflow/runtime_events.py")


def test_issue_keys_are_project_scoped():
    assert task_key(FUNPAY, 42) == "Vetal12350/FunPayFlow#42"
    assert task_key(SEN, 42) == "Vetal12350/SenReader#42"
    assert task_key(FUNPAY, 42) != task_key(SEN, 42)
    for number in [0, -1, True, "42", 10**15]:
        with pytest.raises(ValueError):
            task_key(FUNPAY, number)


def test_migration_preserves_claimed_blocked_pr_created_exactly():
    old = {
        "89": {"status": "started"},
        "93": {"status": "blocked", "reason": "mocked"},
        "124": {"status": "pr_created", "url": "https://github.com/Vetal12350/FunPayFlow/pull/1"},
    }
    migrated = migrate_funpay_ledger(old)
    assert migrated == {task_key(FUNPAY, int(k)): v for k, v in old.items()}
    migrated[task_key(FUNPAY, 89)]["status"] = "blocked"
    assert old["89"]["status"] == "started"


@pytest.mark.parametrize("state", [None, [], {"bad": {"status": "started"}}, {"01": {}}, {"1": []}, {1: {}}])
def test_bad_migration_input_fails_closed(state):
    with pytest.raises(ValueError):
        migrate_funpay_ledger(state)


def test_fair_selection_and_no_work():
    pending = {FUNPAY: True, SEN: True}
    assert choose_next_repo(pending, None) == FUNPAY
    assert choose_next_repo(pending, FUNPAY) == SEN
    assert choose_next_repo(pending, SEN) == FUNPAY
    assert choose_next_repo({FUNPAY: False, SEN: True}, FUNPAY) == SEN
    assert choose_next_repo({FUNPAY: False, SEN: False}, None) is None


def test_queue_rejects_unknown_repo_and_non_boolean_values():
    with pytest.raises(ValueError):
        choose_next_repo({"attacker/repo": True}, None)
    with pytest.raises(ValueError):
        choose_next_repo({SEN: 1}, None)
    with pytest.raises(ValueError):
        choose_next_repo({}, "attacker/repo")
