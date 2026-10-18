"""Offline evidence for the design-only probe; this is not a runtime hook.

The two-call rehearsal below is test code only. No Account is constructed and
no main/Telegram module is imported (those imports can load private settings).
"""

import ast
import inspect
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from requests.exceptions import Timeout
from FunPayAPI.account import Account as ClientAccount

from funpayflow import node_form
from funpayflow.node_form import NodeFormError
from test_node_form import fixture, service


def rehearse(service):
    """Candidate fixed-key reporting contract, deliberately confined to tests."""
    report = {}
    for node in (976, 986):
        try:
            service.get(node, force=True)
        except NodeFormError as error:
            report[str(node)] = error.diagnostic
        else:
            report[str(node)] = {"reason": "OK"}
    return report


def test_two_forced_gets_reuse_exact_account_and_lock(service, monkeypatch):
    account, lock = service.client.account, service.client._account_lock
    calls = []

    def method(*args, **kwargs):
        assert service.client.account is account
        assert service.client._account_lock is lock
        # Another thread cannot enter while NodeFormService owns the RLock.
        acquired = []

        def contender():
            entered = lock.acquire(blocking=False)
            acquired.append(entered)
            if entered:
                lock.release()

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and acquired == [False]
        calls.append((args, kwargs))
        name = "cyberpunk" if len(calls) == 1 else "elden_ring"
        return SimpleNamespace(status_code=200, content=fixture(name).encode(),
                               url="https://funpay.com/" + args[1])

    monkeypatch.setattr(account, "method", method)
    assert rehearse(service) == {"976": {"reason": "OK"}, "986": {"reason": "OK"}}
    assert calls == [
        (("get", f"lots/offerEdit?node={node}", {"accept-language": "ru"}, {}),
         {"raise_not_200": True}) for node in (976, 986)
    ]
    # The service retains its ordinary cache, including technical metadata.
    # A production probe would need an explicit policy for this side effect.
    assert len(service.client._node_form_cache) == 2


def test_failure_report_is_sanitized_and_state_is_unchanged(service, monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('unchanged')")
    before = path.read_bytes()
    lines = []
    monkeypatch.setattr(node_form.logger, "warning", lines.append)
    service.client.account.failure = Timeout("SYNTHETIC_PRIVATE transport detail")

    report = rehearse(service)

    assert report == {str(node): {"reason": "REQUEST_TRANSPORT"} for node in (976, 986)}
    assert len(service.client.account.calls) == 2  # no retries after either failure
    assert lines == ['NodeForm failure {"reason": "REQUEST_TRANSPORT"}'] * 2
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report) + "".join(lines)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM sentinel").fetchall() == [("unchanged",)]
    # Failure releases the shared lock and does not poison a later normal GET.
    service.client.account.failure = None
    assert service.get(976)["node_id"] == 976


def test_installed_transport_has_no_per_call_redirect_or_timeout_control(monkeypatch):
    """Verify the actual supported API, without constructing an Account."""
    parameters = inspect.signature(ClientAccount.method).parameters
    assert "timeout" not in parameters and "allow_redirects" not in parameters
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("FunPayAPI.account.requests.get", get)
    account = SimpleNamespace(golden_key="SYNTHETIC", phpsessid=None,
                              user_agent=None, requests_timeout=10, proxy={})
    ClientAccount.method(account, "get", "lots/offerEdit?node=976", {}, {},
                         raise_not_200=True)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 10
    assert "allow_redirects" not in calls[0][1]  # requests GET defaults to True


def test_no_diagnostic_is_wired_into_boot_or_supervision():
    """Design-only fallback stays OFF; do not import the runtime to check it."""
    source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for item in ast.walk(tree):
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            assert "node_form" not in ast.unparse(item)
    for name in ("main", "_supervise_tasks", "_application_loop"):
        function = next(item for item in tree.body
                        if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
        assert "NodeFormService" not in ast.unparse(function)
        assert "rehearse" not in ast.unparse(function)
