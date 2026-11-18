"""Optional local plugins: off unless FUNPAYFLOW_PLUGINS_DIR is set.

The directory (absolute path, outside any repository) may contain plugin
packages (``<name>/__init__.py``) or modules (``<name>.py``). Each defines
``setup(api)`` and subscribes with ``api.on(kind, callback)`` where kind is
"order" (new order), "message" (new chat message), "order_status" or
"button" (the owner pressed a button the plugin attached to a notice).

Callbacks run one at a time in a dedicated worker thread, never blocking the
bot. A failing plugin is logged by exception type only and never stops the
bot. Events may be delivered again after a restart (backlog replay): plugins
must keep their own idempotent state under ``api.data_dir``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import re
import sys
import threading

import FunPayAPI

from . import logger
from .runtime_control import automatic_action_gate, restart_requested
from .runtime_paths import data_dir

PLUGINS_DIR_ENV = "FUNPAYFLOW_PLUGINS_DIR"
KINDS = ("order", "message", "order_status", "button")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
_TOKEN = re.compile(r"[A-Za-z0-9_:-]{1,40}")
BUTTON_PREFIX = "plg:"
BUTTON_TIMEOUT = 60.0


def _order_fields(order) -> dict:
    status = getattr(order, "status", None)
    return {
        "order_id": getattr(order, "id", None),
        "description": getattr(order, "description", None),
        "price": getattr(order, "price", None),
        "amount": getattr(order, "amount", None),
        "buyer_username": getattr(order, "buyer_username", None),
        "buyer_id": getattr(order, "buyer_id", None),
        "subcategory_name": getattr(order, "subcategory_name", None),
        "status": getattr(status, "name", None),
    }


def event_payload(event) -> tuple[str, dict] | None:
    """Neutral plain-data view of a FunPayAPI event, or None if not exposed."""
    types = FunPayAPI.enums.EventTypes
    kind = getattr(event, "type", None)
    if kind is types.NEW_ORDER:
        return "order", _order_fields(event.order)
    if kind is types.ORDER_STATUS_CHANGED:
        return "order_status", _order_fields(event.order)
    if kind is types.NEW_MESSAGE:
        message = event.message
        return "message", {
            "chat_id": getattr(message, "chat_id", None),
            "chat_name": getattr(message, "chat_name", None),
            "author": getattr(message, "author", None),
            "author_id": getattr(message, "author_id", None),
            "text": getattr(message, "text", None),
            "source": "message",
        }
    if kind is types.LAST_CHAT_MESSAGE_CHANGED:
        # The runtime disables NEW_MESSAGE (history requests failed), so chat
        # activity arrives as the chat-list preview: last text, interlocutor
        # name, unread flag. The author is NOT known: it may be our own text.
        chat = event.chat
        return "message", {
            "chat_id": getattr(chat, "id", None),
            "chat_name": getattr(chat, "name", None),
            "author": getattr(chat, "name", None),
            "text": getattr(chat, "last_message_text", None),
            "unread": getattr(chat, "unread", None),
            "source": "chat_list",
        }
    return None


class PluginAPI:
    """What a plugin may do. Every write goes through the bot's own gates."""

    def __init__(self, host: "PluginHost", name: str):
        self._host, self.name = host, name
        self.log = logger

    @property
    def data_dir(self) -> Path:
        path = data_dir() / "plugins" / self.name
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def on(self, kind: str, callback) -> None:
        if kind not in KINDS or not callable(callback):
            raise ValueError("unknown event kind")
        self._host._subscribers[kind].append((self.name, callback))

    @property
    def account_id(self) -> int | None:
        client = self._host._client
        return getattr(getattr(client, "account", None), "id", None)

    @contextmanager
    def actions_allowed(self):
        """Yields True only while automatic actions are allowed (no SAFE_MODE,
        no restart, runner running). Wrap any external write in it."""
        client = self._host._client
        with automatic_action_gate() as allowed:
            yield bool(allowed and client is not None and not client.runner_stop_requested()
                       and not self._host._safe_mode() and not restart_requested())

    def account_call(self, function):
        """Run function(account) under the shared Account lock (reads)."""
        client = self._host._client
        if client is None:
            raise RuntimeError("bot is not running")
        with client._account_lock:
            return function(client.account)

    def send_message(self, chat_id, text: str) -> bool:
        """Send a FunPay chat message; False when actions are not allowed."""
        client = self._host._client
        if client is None or not isinstance(text, str) or not text.strip():
            return False
        with client._account_lock, self.actions_allowed() as allowed:
            if not allowed:
                return False
            client.account.send_message(chat_id, text, update_last_saved_message=True)
            return True

    def notify_owner(self, text: str, buttons=None) -> bool:
        """Telegram message to the bot owner(s).

        ``buttons``: optional rows of (label, token). Pressing one delivers a
        "button" event {"token", "user_id"} to this plugin; the callback's
        return value (a short text) is shown to the owner under the notice."""
        markup = None
        if buttons:
            markup = []
            for row in buttons:
                cells = []
                for label, token in row:
                    data = f"{BUTTON_PREFIX}{self.name}:{token}"
                    if (not isinstance(label, str) or not label.strip()
                            or not isinstance(token, str) or not _TOKEN.fullmatch(token)
                            or len(data.encode()) > 64):
                        raise ValueError("invalid button")
                    cells.append((label[:40], data))
                markup.append(cells)
        return self._host._notify(text, markup)


class PluginHost:
    def __init__(self):
        self._subscribers = {kind: [] for kind in KINDS}
        self._client = None
        self._notifier = None
        self._safe_mode = lambda: True
        self._executor = None
        self._lock = threading.Lock()
        self.loaded: list[str] = []
        self._attempted = False

    @property
    def active(self) -> bool:
        return any(self._subscribers.values())

    def load(self, directory: str | None) -> None:
        if self._attempted or not directory:
            return
        self._attempted = True
        base = Path(directory)
        if not base.is_absolute() or not base.is_dir():
            logger.warning("Плагины: каталог из FUNPAYFLOW_PLUGINS_DIR недоступен.")
            return
        for entry in sorted(base.iterdir()):
            name = entry.stem if entry.suffix == ".py" else entry.name
            path = entry / "__init__.py" if entry.is_dir() else entry
            if (not _NAME.fullmatch(name) or not path.is_file()
                    or (entry.is_file() and entry.suffix != ".py")):
                continue
            try:
                module_name = f"funpayflow_plugin_{name}"
                spec = importlib.util.spec_from_file_location(
                    module_name, path,
                    submodule_search_locations=[str(entry)] if entry.is_dir() else None)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                module.setup(PluginAPI(self, name))
                self.loaded.append(name)
            except Exception as error:
                sys.modules.pop(f"funpayflow_plugin_{name}", None)
                logger.warning(f"Плагин {name} не загружен: {type(error).__name__}.")
        if self.loaded:
            logger.info(f"Плагины загружены: {', '.join(self.loaded)}.")

    def bind(self, client, notifier, safe_mode) -> None:
        """Attach to the current runtime cycle's Account and owner notifier."""
        with self._lock:
            self._client, self._notifier, self._safe_mode = client, notifier, safe_mode
            if self.active and self._executor is None:
                self._executor = ThreadPoolExecutor(1, thread_name_prefix="plugins")

    def unbind(self) -> None:
        with self._lock:
            self._client, self._notifier, self._safe_mode = None, None, (lambda: True)

    def _notify(self, text, buttons=None) -> bool:
        notifier = self._notifier
        if notifier is None or not isinstance(text, str) or not text.strip():
            return False
        try:
            if buttons:
                notifier(text[:3500], buttons)
            else:
                notifier(text[:3500])
            return True
        except Exception:
            return False

    def press(self, data: str, user_id: int) -> str | None:
        """Owner pressed a plugin button (blocking; call from a thread).

        Runs in the plugin worker, after events already queued, and returns
        the plugin's reply text, or None when nobody handled it."""
        if not isinstance(data, str) or not data.startswith(BUTTON_PREFIX):
            return None
        name, _, token = data[len(BUTTON_PREFIX):].partition(":")
        if not _NAME.fullmatch(name) or not _TOKEN.fullmatch(token or "") or self._executor is None:
            return None
        callbacks = [cb for owner, cb in self._subscribers["button"] if owner == name]
        if not callbacks:
            return None

        def run():
            for callback in callbacks:
                try:
                    reply = callback({"token": token, "user_id": user_id})
                except Exception as error:
                    logger.warning(f"Плагин {name}: ошибка кнопки: {type(error).__name__}.")
                    return "⚠️ Ошибка обработки кнопки, подробности у плагина."
                if isinstance(reply, str) and reply.strip():
                    return reply[:1000]
            return None
        return self._executor.submit(run).result(timeout=BUTTON_TIMEOUT)

    def dispatch(self, event) -> None:
        """Hand an event to subscribed plugins without waiting for them."""
        if not self.active or self._executor is None:
            return
        payload = event_payload(event)
        if payload is None or not self._subscribers[payload[0]]:
            return
        kind, data = payload
        self._executor.submit(self._run, kind, data)

    def _run(self, kind, data) -> None:
        for name, callback in list(self._subscribers[kind]):
            try:
                callback(dict(data))
            except Exception as error:
                logger.warning(f"Плагин {name}: ошибка обработки события: {type(error).__name__}.")


host = PluginHost()


def load_from_env() -> None:
    host.load(os.environ.get(PLUGINS_DIR_ENV))
