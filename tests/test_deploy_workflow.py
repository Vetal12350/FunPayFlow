"""Offline: release builder <-> deploy agent contract and deploy.yml invariants."""
import hashlib
import importlib.util
import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
SHA = "e" * 40


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


builder = load("build_deploy_release", "scripts/build_deploy_release.py")


def wheels(tmp_path, names=("funpayflow-1.0-py3-none-any.whl",
                            "aiogram-3.0-py3-none-any.whl")):
    d = tmp_path / "wheels"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"PK" + n.encode())
    return d


def test_archive_is_deterministic(tmp_path):
    d = wheels(tmp_path)
    a = builder.assemble(SHA, d, tmp_path / "a.tar")
    b = builder.assemble(SHA, d, tmp_path / "b.tar")
    assert a == b == hashlib.sha256((tmp_path / "a.tar").read_bytes()).hexdigest()


@pytest.mark.parametrize("sha,names", [
    ("E" * 40, ("funpayflow-1.0-py3-none-any.whl",)),
    (SHA, ("aiogram-3.0-py3-none-any.whl",)),
    (SHA, ("funpayflow-1.0-py3-none-any.whl", "evil.sh")),
])
def test_builder_rejects_bad_input(tmp_path, sha, names):
    with pytest.raises(ValueError):
        builder.assemble(sha, wheels(tmp_path, names), tmp_path / "r.tar")


@pytest.mark.skipif(os.name == "nt", reason="deploy agent is Linux-only")
def test_agent_accepts_builder_archive(tmp_path):
    agent = load("deploy_agent_wf", "scripts/deploy_agent.py")
    builder.assemble(SHA, wheels(tmp_path), tmp_path / "r.tar")
    release = tmp_path / "release"
    release.mkdir()
    manifest = agent.extract_release(tmp_path / "r.tar", release)
    assert manifest["sha"] == SHA
    assert sorted(p.name for p in (release / "wheels").iterdir()) == [
        "aiogram-3.0-py3-none-any.whl", "funpayflow-1.0-py3-none-any.whl"]


def job(name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \w+:\n|\Z)", WORKFLOW, re.S | re.M)
    assert m, name
    return m.group(1)


def test_manual_trigger_only_and_serialized():
    on = WORKFLOW.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in on
    for trigger in ("push:", "pull_request", "schedule:", "workflow_run"):
        assert trigger not in on
    assert "group: production-deploy" in WORKFLOW
    assert "cancel-in-progress: false" in WORKFLOW


def test_production_jobs_need_environment_approval():
    assert "environment: production" in job("deploy")
    assert "environment: production" in job("rollback")
    assert "environment:" not in job("verify")
    assert "secrets." not in job("verify")
    assert "needs: verify" in job("deploy")


def test_inputs_never_interpolated_into_shell():
    for block in re.findall(r"run: \|\n(.*?)(?=\n      - |\n  \w|\Z)", WORKFLOW, re.S):
        assert "${{" not in block
    assert "${{" not in "".join(re.findall(r"run: '.*'", WORKFLOW))


def test_ssh_is_strict_and_deploy_is_sent_once():
    deploy = job("deploy")
    assert deploy.count('"deploy $SHA $DIGEST"') == 1
    assert "StrictHostKeyChecking=yes" in deploy and "BatchMode=yes" in deploy
    assert "StrictHostKeyChecking=no" not in WORKFLOW
    assert '"status $SHA"' in deploy
    assert "if: always()" in deploy and 'rm -rf "$RUNNER_TEMP/ssh"' in deploy


def test_verify_requires_main_head_and_green_ci():
    verify = job("verify")
    assert "git rev-parse origin/main" in verify
    assert 'startswith("offline-tests")' in verify
    assert "persist-credentials: false" in verify
    assert "uv run --no-sync pytest -q" in verify
