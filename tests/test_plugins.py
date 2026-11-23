"""Offline: optional local plugin loader and its gated API."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import FunPayAPI
import pytest

from funpayflow import plugins, runtime_control

EVENTS = FunPayAPI.enums.EventTypes


def client():
    account = SimpleNamespace(id=42, send_message=Mock())
    return SimpleNamespace(account=account, _account_lock=threading.RLock(),
                           runner_stop_requested=lambda: False)


def write(directory, rel, text):
    path = directory / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(autouse=True)
def runtime():
    runtime_control.begin_runtime_cycle()
    yield
    runtime_control.begin_runtime_cycle()


def flush(host):
    host._executor.submit(lambda: None).result(timeout=5)


def test_without_directory_nothing_happens():
    host = plugins.PluginHost()
    host.load(None)
    host.bind(client(), None, lambda: False)
    host.dispatch(SimpleNamespace(type=EVENTS.NEW_ORDER, order=object()))
    assert host.loaded == [] and host._executor is None


def test_relative_or_missing_directory_is_ignored(tmp_path):
    host = plugins.PluginHost()
    host.load("relative/plugins")
    assert host.loaded == []
    host = plugins.PluginHost()
    host.load(str(tmp_path / "missing"))
    assert host.loaded == []


def test_loads_packages_and_modules_skips_bad_ones_and_isolates_errors(tmp_path):
    write(tmp_path, "alpha/__init__.py", "from .helper import VALUE\n"
          "def setup(api):\n    api.on('order', lambda e: SEEN.append((VALUE, e)))\nSEEN = []\n")
    write(tmp_path, "alpha/helper.py", "VALUE = 'pkg'\n")
    write(tmp_path, "beta.py", "def setup(api):\n    api.on('message', lambda e: (MSGS.append(e), 1 / 0))\nMSGS = []\n")
    write(tmp_path, "broken.py", "def setup(api):\n    raise RuntimeError('SECRET-DETAIL')\n")
    write(tmp_path, "Bad-Name.py", "raise SystemExit\n")
    write(tmp_path, "notes.txt", "ignored")
    host = plugins.PluginHost()
    host.load(str(tmp_path))
    assert host.loaded == ["alpha", "beta"]
    host.load(str(tmp_path))  # loads once per process
    assert host.loaded == ["alpha", "beta"]
    host.bind(client(), None, lambda: False)
    order = SimpleNamespace(id="ABC", description="Lot [X1]", price=1.5, amount=2,
                            buyer_username="buyer", buyer_id=7, subcategory_name="Games",
                            status=SimpleNamespace(name="PAID"))
    host.dispatch(SimpleNamespace(type=EVENTS.NEW_ORDER, order=order))
    host.dispatch(SimpleNamespace(type=EVENTS.NEW_MESSAGE, message=SimpleNamespace(
        chat_id=5, chat_name="buyer", author="buyer", author_id=7, text="+")))
    host.dispatch(SimpleNamespace(type=EVENTS.LAST_CHAT_MESSAGE_CHANGED, chat=SimpleNamespace(
        id=6, name="buyer", last_message_text="+", unread=True)))
    host.dispatch(SimpleNamespace(type=EVENTS.INITIAL_CHAT, chat=object()))
    flush(host)
    import sys
    seen = sys.modules["funpayflow_plugin_alpha"].SEEN
    assert seen == [("pkg", {"order_id": "ABC", "description": "Lot [X1]", "price": 1.5,
                             "amount": 2, "buyer_username": "buyer", "buyer_id": 7,
                             "subcategory_name": "Games", "status": "PAID"})]
    msgs = sys.modules["funpayflow_plugin_beta"].MSGS
    assert msgs[0]["source"] == "message" and msgs[0]["text"] == "+"
    assert msgs[1] == {"chat_id": 6, "chat_name": "buyer", "author": "buyer", "text": "+",
                       "unread": True, "source": "chat_list"}


def test_send_message_respects_safe_mode_restart_and_unbind(tmp_path):
    host = plugins.PluginHost()
    api = plugins.PluginAPI(host, "demo")
    c = client()
    host.bind(c, None, lambda: True)  # SAFE_MODE on
    assert api.send_message(5, "hello") is False
    host.bind(c, None, lambda: False)
    assert api.send_message(5, "hello") is True
    c.account.send_message.assert_called_once_with(5, "hello", update_last_saved_message=True)
    assert api.send_message(5, "   ") is False
    runtime_control.claim_restart()
    assert api.send_message(5, "again") is False
    with api.actions_allowed() as allowed:
        assert allowed is False
    host.unbind()
    assert api.send_message(5, "late") is False
    assert c.account.send_message.call_count == 1


def test_account_call_uses_lock_and_notify_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNPAY_BOT_DATA_DIR", str(tmp_path))
    host = plugins.PluginHost()
    api = plugins.PluginAPI(host, "demo")
    with pytest.raises(RuntimeError):
        api.account_call(lambda account: account.id)
    c = client()
    notes = []
    host.bind(c, notes.append, lambda: False)
    assert api.account_call(lambda account: (c._account_lock._is_owned(), account.id)) == (True, 42)
    assert api.account_id == 42
    assert api.notify_owner("ping") is True and notes == ["ping"]
    assert api.data_dir == tmp_path / "plugins" / "demo" and api.data_dir.is_dir()
    with pytest.raises(ValueError):
        api.on("payment", print)


def test_buttons_reach_only_their_plugin_and_reply(tmp_path):
    write(tmp_path, "alpha.py",
          "def setup(api):\n"
          "    api.on('button', lambda e: f\"ok {e['token']} {e['user_id']}\")\n"
          "    api.notify_owner('code ready', buttons=[[('Send', 'send:A1'), ('Cancel', 'cancel:A1')]])\n")
    write(tmp_path, "beta.py", "def setup(api):\n    api.on('button', lambda e: 'beta')\n")
    host = plugins.PluginHost()
    sent = []
    host.bind(client(), lambda text, buttons=None: sent.append((text, buttons)), lambda: False)
    host.load(str(tmp_path))
    host.bind(client(), lambda text, buttons=None: sent.append((text, buttons)), lambda: False)
    assert sent == [("code ready", [[("Send", "plg:alpha:send:A1"),
                                     ("Cancel", "plg:alpha:cancel:A1")]])]
    assert host.press("plg:alpha:send:A1", 7) == "ok send:A1 7"
    assert host.press("plg:beta:x", 7) == "beta"
    assert host.press("plg:gamma:x", 7) is None          # unknown plugin
    assert host.press("plg:alpha:bad token!", 7) is None  # invalid token
    assert host.press("other:alpha:x", 7) is None


def test_invalid_buttons_are_refused():
    host = plugins.PluginHost()
    api = plugins.PluginAPI(host, "alpha")
    host.bind(client(), lambda *a: None, lambda: False)
    with pytest.raises(ValueError):
        api.notify_owner("x", buttons=[[("Send", "has space")]])
    with pytest.raises(ValueError):
        api.notify_owner("x", buttons=[[("Send", "a" * 41)]])


def test_failing_button_callback_reports_without_raising(tmp_path):
    write(tmp_path, "alpha.py", "def setup(api):\n    api.on('button', lambda e: 1 / 0)\n")
    host = plugins.PluginHost()
    host.load(str(tmp_path))
    host.bind(client(), lambda *a: None, lambda: False)
    assert "Ошибка" in host.press("plg:alpha:t", 1)


def test_toggles_default_flip_persist_and_validate(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNPAY_BOT_DATA_DIR", str(tmp_path))
    host = plugins.PluginHost()
    api = plugins.PluginAPI(host, "demo")
    api.toggle("game_a", "Game A", default=True)
    api.toggle("game_b", "Game B")
    assert api.enabled("game_a") is True and api.enabled("game_b") is False
    assert api.enabled("unknown") is False
    assert host.toggles() == [("demo", "game_a", "Game A", True), ("demo", "game_b", "Game B", False)]

    assert host.flip("demo", "game_a") is False
    assert host.flip("other", "game_a") is None
    assert api.enabled("game_a") is False

    restarted = plugins.PluginHost()
    again = plugins.PluginAPI(restarted, "demo")
    again.toggle("game_a", "Game A", default=True)
    assert again.enabled("game_a") is False

    for key, label in (("Bad Key", "x"), ("k" * 17, "x"), ("ok", ""), ("ok", None)):
        with pytest.raises(ValueError):
            api.toggle(key, label)


def test_telegram_toggle_menu_flips_for_owner_only(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from funpayflow import telegram as ui
    monkeypatch.setenv("FUNPAY_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_ID", "7")
    host = plugins.PluginHost()
    monkeypatch.setattr(plugins, "host", host)
    plugins.PluginAPI(host, "demo").toggle("game_a", "Game A", default=True)

    def press(user_id, data):
        callback = SimpleNamespace(answer=AsyncMock(),
                                   message=SimpleNamespace(edit_text=AsyncMock()))
        asyncio.run(ui._plugin_toggle_callback(callback, data, user_id))
        return callback

    assert "plgt:" in str(ui.get_main_keyboard(7))
    stranger = press(8, "plgt:demo:game_a")
    assert stranger.message.edit_text.await_count == 0 and host.toggles()[0][3] is True
    owner = press(7, "plgt:demo:game_a")
    assert host.toggles()[0][3] is False
    markup = owner.message.edit_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "plgt:demo:game_a"
    assert press(7, "plgt:demo:missing").answer.await_args.kwargs["show_alert"] is True
