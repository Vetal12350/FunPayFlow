import asyncio
import hmac
import os
import json
import math
import re
import threading
import time
import tempfile
from html import escape
from string import Formatter
import requests
import FunPayAPI
import secrets
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from funpay import FunPayClient, NIGHT_MODE_MESSAGE_TEXT, NIGHT_MODE_ORDER_TEXT
from state import StateError
import logger

dp = Dispatcher()
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


async def _send_log_file(target: Message | CallbackQuery, date_str: str) -> None:
    """Отправляет существующий файл лога за проверенную дату."""
    answer = target.message.answer if isinstance(target, CallbackQuery) else target.answer
    answer_document = (target.message.answer_document if isinstance(target, CallbackQuery)
                       else target.answer_document)
    log_path = os.path.join(LOGS_DIR, f"bot_{date_str}.log")
    try:
        if not os.path.isfile(log_path):
            await answer(f"Лог за {date_str} не найден.")
        elif os.path.getsize(log_path) == 0:
            await answer(f"Лог за {date_str} пустой.")
        else:
            await answer_document(FSInputFile(log_path, filename=f"bot_{date_str}.log"))
    except Exception as e:
        logger.warning(f"Не удалось отправить файл лога: {type(e).__name__}.")
        await answer("Не удалось отправить файл лога.")

_runtime_client: FunPayClient | None = None
_runtime_started_at: float | None = None
_SAFE_RUNNER_FAILURE_TYPES = frozenset({
    "UnexpectedStop", "_EventQueueOverflow", "RequestFailedError", "UnauthorizedError",
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "ProxyError", "ChunkedEncodingError", "ValueError", "TypeError", "KeyError",
    "AttributeError", "AssertionError", "RuntimeError",
})


def set_runtime_status_context(client: FunPayClient) -> None:
    """Вызывается один раз после успешного startup приложения."""
    global _runtime_client, _runtime_started_at
    _runtime_client = client
    _runtime_started_at = time.monotonic()


def clear_runtime_status_context() -> None:
    global _runtime_client, _runtime_started_at
    _runtime_client = None
    _runtime_started_at = None


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return f"{seconds} сек"


def _elapsed_since(value: float | None, now: float) -> str:
    if value is None:
        return "ещё не было"
    if type(value) not in (int, float) or not math.isfinite(value):
        return "недоступно"
    return f"{_format_duration(max(0, now - value))} назад"


def get_runtime_status_text() -> str:
    """Только локальный snapshot; сетевых и SQLite операций здесь нет."""
    client = _runtime_client
    now = time.monotonic()
    if client is None or _runtime_started_at is None:
        return "🩺 Состояние бота\nRuntime: не запущен"

    uptime = _elapsed_since(_runtime_started_at, now).removesuffix(" назад")
    health_available = True
    try:
        health = client.get_runner_health()
        if not isinstance(health, dict):
            raise TypeError("Invalid runner snapshot")
    except Exception as e:
        logger.warning(f"Status runner snapshot unavailable: {type(e).__name__}.")
        health = {}
        health_available = False

    runner_states = {
        "starting": "starting",
        "healthy": "running",
        "backoff": "backoff",
        "failed": "failed",
        "stopped": "stopped",
    }
    state = health.get("state")
    runner = runner_states.get(state, "недоступно") if isinstance(state, str) else "недоступно"
    errors = health.get("consecutive_errors")
    errors_text = str(errors) if type(errors) is int and errors >= 0 else "недоступно"
    last_success = (
        _elapsed_since(health.get("last_success_monotonic"), now)
        if health_available else "недоступно"
    )

    try:
        size = client.event_queue.qsize()
        capacity = client.event_queue.maxsize
        if type(size) is not int or type(capacity) is not int or size < 0 or capacity <= 0:
            raise ValueError("Invalid queue snapshot")
        queue_text = f"{size} / {capacity}"
    except Exception as e:
        logger.warning(f"Status queue snapshot unavailable: {type(e).__name__}.")
        queue_text = "недоступно"

    try:
        if getattr(client, "_review_state_failed", False):
            persistent = "недоступно"
        elif getattr(client, "review_state", None) is not None:
            persistent = "инициализировано при запуске"
        else:
            persistent = "недоступно"
    except Exception as e:
        logger.warning(f"Status persistent state unavailable: {type(e).__name__}.")
        persistent = "недоступно"

    try:
        bump = bot_settings["auto_bump"]
        bump_text = "включено" if bump is True else "выключено" if bump is False else "недоступно"
    except Exception as e:
        logger.warning(f"Status autobump setting unavailable: {type(e).__name__}.")
        bump_text = "недоступно"

    lines = [
        "🩺 Состояние бота",
        "Runtime: 🟢 работает",
        f"Uptime: {uptime}",
        f"Runner: {runner}",
        f"Последний успешный poll: {last_success}",
        f"Ошибок подряд: {errors_text}",
        f"Очередь событий: {queue_text}",
        f"Persistent state: {persistent}",
        f"Автоподнятие: {bump_text}",
    ]
    category = health.get("last_failure_category")
    failure_type = health.get("last_failure_type")
    if category is not None or failure_type is not None:
        safe_category = category if category in ("AUTH", "RECOVERABLE", "FATAL", "UNKNOWN") else "недоступно"
        safe_type = (
            failure_type if isinstance(failure_type, str)
            and failure_type in _SAFE_RUNNER_FAILURE_TYPES else "недоступно"
        )
        lines.append(f"Последний сбой: {safe_category} / {safe_type}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Сохранение настроек между перезапусками
# ---------------------------------------------------------------------------
# Настройки разделены на два уровня:
#   1. Глобальные (bot_settings) — auto_bump, authorized_user_ids.
#      Влияют на работу бота в целом.
#   2. Персональные (_user_settings) — notify_*, notifications_enabled.
#      У каждого авторизованного пользователя свои, независимые друг от друга.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_settings.json")
DEFAULT_REVIEW_REQUEST_TEXT = "Спасибо за покупку! Если всё понравилось, пожалуйста, оставьте отзыв о заказе."

# Глобальные настройки — общие для всего бота
_DEFAULT_GLOBAL_SETTINGS: dict = {
    "auto_bump": False,
    "night_mode": False,
    "authorized_user_ids": [],
    # OLD production-аккаунт был USD; это явная account-level настройка статистики.
    "stats_currency": "USD",
    "night_mode_reply": None,
    "reply_templates": [],
    "autoresponder_enabled": False,
    "autoresponder_rules": [],
    "review_request_enabled": False,
    "review_request_text": DEFAULT_REVIEW_REQUEST_TEXT,
    "review_request_delay": 5,
}

# Дефолтные персональные настройки — используются при первой авторизации нового пользователя
_DEFAULT_USER_SETTINGS: dict = {
    "notifications_enabled": True,
    "notify_bump": True,
    "notify_message": True,
    "notify_order": True,
    "notify_review": True,
}

# Глобальные настройки бота (авто-подъём, список авторизованных)
bot_settings: dict = dict(_DEFAULT_GLOBAL_SETTINGS)
_night_mode_state_lock = threading.Lock()
_night_reply_echoes = deque([NIGHT_MODE_MESSAGE_TEXT, NIGHT_MODE_ORDER_TEXT], maxlen=8)
_interaction_state: dict[int, dict] = {}
_chat_pages: dict[int, list[tuple[int, str, bool]]] = {}

# Персональные настройки: {user_id (int): {notify_*, notifications_enabled}}
_user_settings: dict[int, dict] = {}


def get_user_settings(user_id: int) -> dict:
    """
    Возвращает персональные настройки уведомлений для конкретного пользователя.
    Если пользователь ещё не имеет записи — создаёт её с дефолтными значениями.
    """
    if user_id not in _user_settings:
        _user_settings[user_id] = dict(_DEFAULT_USER_SETTINGS)
    return _user_settings[user_id]


def get_all_recipients() -> list[int]:
    """
    Возвращает список всех user_id, которым нужно рассылать уведомления:
    ADMIN_ID из .env + все пользователи из authorized_user_ids.
    """
    ids: set[int] = set()
    admin_id_str = os.getenv("ADMIN_ID", "")
    try:
        ids.add(int(admin_id_str))
    except (ValueError, TypeError):
        pass
    for uid in bot_settings.get("authorized_user_ids", []):
        try:
            ids.add(int(uid))
        except (ValueError, TypeError):
            pass
    return list(ids)


def load_settings() -> None:
    """
    Подтягивает сохранённые настройки с диска.
    Поддерживает автоматическую миграцию старого плоского формата
    (когда notify_* были глобальными) в новый формат с user_settings.
    """
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        # Проверяем до изменения глобального состояния: строка "false" truthy.
        if (not isinstance(saved, dict)
                or type(saved.get("auto_bump", False)) is not bool
                or type(saved.get("night_mode", False)) is not bool
                or type(saved.get("autoresponder_enabled", False)) is not bool
                or type(saved.get("review_request_enabled", False)) is not bool):
            raise ValueError("Invalid global settings.")
        user_ids = saved.get("authorized_user_ids", [])
        if (not isinstance(user_ids, list)
                or any(type(uid) is not int or uid <= 0 for uid in user_ids)):
            raise ValueError("Invalid authorized users.")
        personal = saved.get("user_settings", {})
        if not isinstance(personal, dict):
            raise ValueError("Invalid user settings.")
        for uid, settings in personal.items():
            if (not isinstance(uid, str) or not uid.isdecimal() or int(uid) <= 0
                    or not isinstance(settings, dict)
                    or any(type(value) is not bool for key, value in settings.items()
                           if key in _DEFAULT_USER_SETTINGS)):
                raise ValueError("Invalid user settings.")
        if any(type(saved[key]) is not bool for key in _DEFAULT_USER_SETTINGS if key in saved):
            raise ValueError("Invalid legacy settings.")
        custom_reply = saved.get("night_mode_reply")
        templates = saved.get("reply_templates", [])
        if (custom_reply is not None and (type(custom_reply) is not str or not custom_reply.strip()
                                          or len(custom_reply) > 1000)):
            raise ValueError("Invalid night mode reply.")
        if (type(templates) is not list or len(templates) > 20
                or any(type(item) is not dict or set(item) != {"id", "title", "text"}
                       or type(item["id"]) is not str or not re.fullmatch(r"[0-9a-f]{8}", item["id"])
                       or type(item["title"]) is not str or not 0 < len(item["title"]) <= 40
                       or type(item["text"]) is not str or not 0 < len(item["text"]) <= 1000
                       for item in templates)
                or len({item["id"] for item in templates}) != len(templates)):
            raise ValueError("Invalid templates.")
        rules = saved.get("autoresponder_rules", [])
        if (type(rules) is not list or len(rules) > 50
                or any(type(rule) is not dict or set(rule) != {
                    "id", "trigger", "template_id", "match_mode", "enabled"}
                    or type(rule["id"]) is not str or not re.fullmatch(r"[0-9a-f]{8}", rule["id"])
                    or type(rule["trigger"]) is not str or not 0 < len(rule["trigger"].strip()) <= 200
                    or type(rule["template_id"]) is not str
                    or not re.fullmatch(r"[0-9a-f]{8}", rule["template_id"])
                    or rule["match_mode"] not in ("EXACT", "CONTAINS")
                    or type(rule["enabled"]) is not bool for rule in rules)
                or len({rule["id"] for rule in rules}) != len(rules)):
            raise ValueError("Invalid autoresponder rules.")
        review_text = saved.get("review_request_text", DEFAULT_REVIEW_REQUEST_TEXT)
        review_delay = saved.get("review_request_delay", 5)
        if (type(review_text) is not str or not review_text.strip() or len(review_text) > 1000
                or type(review_delay) is not int or not 0 <= review_delay <= 1440):
            raise ValueError("Invalid review request settings.")

        # --- Миграция старого формата (без user_settings) ---
        # Раньше notify_* хранились в корне — теперь они персональные.
        # При обнаружении старого формата переносим их в настройки ADMIN_ID.
        if "user_settings" not in saved:
            old_user_keys = {"notifications_enabled", "notify_bump", "notify_message", "notify_order", "notify_review"}
            migrated: dict = {}
            for key in old_user_keys:
                if key in saved:
                    migrated[key] = saved[key]
            if migrated:
                admin_id_str = os.getenv("ADMIN_ID", "")
                if admin_id_str.isdigit():
                    _user_settings[int(admin_id_str)] = {**_DEFAULT_USER_SETTINGS, **migrated}
            print("[SETTINGS] Выполнена миграция настроек из старого формата в новый (персональные уведомления).")

        # Загружаем глобальные ключи
        for key in _DEFAULT_GLOBAL_SETTINGS:
            if key in saved:
                bot_settings[key] = saved[key]
        configured_currency = bot_settings.get("stats_currency")
        if (type(configured_currency) is not str
                or not re.fullmatch(r"[A-Z]{3}", configured_currency)):
            # Некорректная явная настройка не должна молча превращаться в USD.
            bot_settings["stats_currency"] = None

        # Загружаем персональные настройки пользователей
        for uid_str, usett in saved.get("user_settings", {}).items():
            try:
                uid = int(uid_str)
                merged = dict(_DEFAULT_USER_SETTINGS)
                for k, v in usett.items():
                    if k in _DEFAULT_USER_SETTINGS:
                        merged[k] = v
                _user_settings[uid] = merged
            except (ValueError, TypeError):
                pass

        print("[SETTINGS] Настройки загружены.")
    except Exception as e:
        bot_settings.clear()
        bot_settings.update(_DEFAULT_GLOBAL_SETTINGS)
        _user_settings.clear()
        print(f"[SETTINGS] Ошибка загрузки, использую значения по умолчанию: {type(e).__name__}.")


def save_settings(*, required: bool = False) -> None:
    """Атомарно сохраняет настройки; required не скрывает ошибку записи."""
    temporary_path = None
    try:
        data = {
            "auto_bump": bot_settings["auto_bump"],
            "night_mode": bot_settings["night_mode"],
            "authorized_user_ids": bot_settings["authorized_user_ids"],
            "stats_currency": bot_settings["stats_currency"],
            "night_mode_reply": bot_settings["night_mode_reply"],
            "reply_templates": bot_settings["reply_templates"],
            "autoresponder_enabled": bot_settings["autoresponder_enabled"],
            "autoresponder_rules": bot_settings["autoresponder_rules"],
            "review_request_enabled": bot_settings["review_request_enabled"],
            "review_request_text": bot_settings["review_request_text"],
            "review_request_delay": bot_settings["review_request_delay"],
            "user_settings": {
                str(uid): sett for uid, sett in _user_settings.items()
            },
        }
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".bot_settings-", suffix=".tmp", dir=os.path.dirname(SETTINGS_FILE)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, SETTINGS_FILE)
        temporary_path = None
    except Exception as e:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        if required:
            raise RuntimeError("Не удалось сохранить настройки.") from None
        print(f"[SETTINGS] Не удалось сохранить настройки: {type(e).__name__}.")


def is_night_mode_enabled() -> bool:
    with _night_mode_state_lock:
        return bot_settings.get("night_mode", False)


def is_review_request_enabled() -> bool:
    with _night_mode_state_lock:
        return bot_settings.get("review_request_enabled", False)


def get_night_mode_reply_text(kind: str = "message") -> str:
    with _night_mode_state_lock:
        return bot_settings.get("night_mode_reply") or (
            NIGHT_MODE_MESSAGE_TEXT if kind == "message" else NIGHT_MODE_ORDER_TEXT
        )


def is_night_mode_reply_text(value: str | None) -> bool:
    with _night_mode_state_lock:
        return type(value) is str and (
            value in _night_reply_echoes or value == bot_settings.get("night_mode_reply")
        )


def _save_global_setting(key: str, value) -> None:
    with _night_mode_state_lock:
        previous = bot_settings[key]
        bot_settings[key] = value
        try:
            save_settings(required=True)
        except Exception:
            bot_settings[key] = previous
            raise
        if key == "night_mode_reply":
            if previous:
                _night_reply_echoes.append(previous)
            if value:
                _night_reply_echoes.append(value)


def toggle_night_mode_saved() -> bool:
    """Фиксирует состояние и required-save независимо от Account RLock."""
    with _night_mode_state_lock:
        previous = bot_settings["night_mode"]
        bot_settings["night_mode"] = not previous
        try:
            save_settings(required=True)
        except Exception:
            bot_settings["night_mode"] = previous
            raise RuntimeError("Не удалось сохранить ночной режим.") from None
        return bot_settings["night_mode"]


def disable_autobump() -> None:
    """Выключает автоподнятие и надёжно сохраняет OFF."""
    bot_settings["auto_bump"] = False
    save_settings(required=True)


load_settings()

# Восстанавливаем authorized_users из сохранённых настроек
authorized_users: set[int] = set(bot_settings.get("authorized_user_ids", []))

# ---------------------------------------------------------------------------
# Rate-limiting для неверных попыток ввода пароля
# ---------------------------------------------------------------------------
# Защита от брутфорса: после _MAX_ATTEMPTS неверных попыток пользователь
# блокируется на _BLOCK_SECONDS секунд. Хранится только в памяти — при
# перезапуске блокировки сбрасываются (осознанно: это не критичная защита,
# а базовый deterrent). ADMIN_ID никогда не попадает сюда — он проходит
# проверку is_authorized() раньше.
_MAX_ATTEMPTS = 5
_BLOCK_SECONDS = 300  # 5 минут

# {user_id: {"attempts": int, "blocked_until": float}}
_failed_attempts: dict[int, dict] = {}


def _is_rate_limited(user_id: int) -> bool:
    """Возвращает True, если пользователь заблокирован из-за превышения попыток."""
    entry = _failed_attempts.get(user_id)
    if not entry:
        return False
    blocked_until = entry.get("blocked_until", 0)
    if blocked_until > 0:
        if blocked_until > time.monotonic():
            return True
        # Сбрасываем только действительно истёкшую блокировку.
        _failed_attempts.pop(user_id, None)
    return False


def _record_failed_attempt(user_id: int) -> int:
    """
    Фиксирует неверную попытку и возвращает количество оставшихся попыток.
    При достижении лимита выставляет блокировку.
    """
    entry = _failed_attempts.setdefault(user_id, {"attempts": 0, "blocked_until": 0.0})
    entry["attempts"] += 1
    remaining = _MAX_ATTEMPTS - entry["attempts"]
    if remaining <= 0:
        entry["blocked_until"] = time.monotonic() + _BLOCK_SECONDS
        entry["attempts"] = 0  # сбрасываем счётчик для следующей серии после разблокировки
        return 0
    return remaining


def _reset_failed_attempts(user_id: int) -> None:
    """Сбрасывает счётчик неверных попыток после успешной авторизации."""
    _failed_attempts.pop(user_id, None)


def is_authorized(user_id: int) -> bool:
    admin_id_str = os.getenv("ADMIN_ID", "")
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        admin_id = None
    return user_id == admin_id or user_id in authorized_users


def on_icon(state: bool) -> str:
    return "✅" if state else "⛔"


# Постоянная клавиатура внизу экрана (Контекстное меню)
def get_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛠 Главное меню")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


# Главное inline-меню.
# Авто-подъём — глобальный тумблер (один на всех).
# Кнопка уведомлений ведёт в персональные настройки.
def get_main_keyboard(user_id: int):
    bump_status = on_icon(bot_settings["auto_bump"])
    night_status = on_icon(bot_settings["night_mode"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Автоподнятие лотов: {bump_status}", callback_data="toggle_bump")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton(text="🩺 Статус", callback_data="menu_status")],
        [InlineKeyboardButton(text="🔔 Мои уведомления", callback_data="menu_notifications")],
        [InlineKeyboardButton(text=f"😴 Ночной режим: {night_status}", callback_data="menu_night_mode")],
        [InlineKeyboardButton(text="💬 Чаты", callback_data="chats:0")],
        [InlineKeyboardButton(text="🤖 Автоответчик", callback_data="auto_menu")],
        [InlineKeyboardButton(text="📄 Лог", callback_data="menu_logs")],
    ])
    return keyboard


def get_logs_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Сегодня", callback_data="log_today")],
        [InlineKeyboardButton(text="📄 Вчера", callback_data="log_yesterday")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")],
    ])


def get_stats_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="stats_today")],
        [InlineKeyboardButton(text="🗓 Неделя", callback_data="stats_week")],
        [InlineKeyboardButton(text="🗓 Месяц", callback_data="stats_month")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")],
    ])


# Подменю уведомлений — полностью персональное для каждого пользователя.
def get_notifications_keyboard(user_id: int):
    u = get_user_settings(user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔔 Все уведомления: {on_icon(u['notifications_enabled'])}", callback_data="notif_notifications_enabled")],
        [InlineKeyboardButton(text=f"🚀 Автоподнятие: {on_icon(u['notify_bump'])}", callback_data="notif_bump")],
        [InlineKeyboardButton(text=f"💬 Новые сообщения: {on_icon(u['notify_message'])}", callback_data="notif_message")],
        [InlineKeyboardButton(text=f"💰 Оплата заказов: {on_icon(u['notify_order'])}", callback_data="notif_order")],
        [InlineKeyboardButton(text=f"🌟 Отзывы: {on_icon(u['notify_review'])}", callback_data="notif_review")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")],
    ])
    return keyboard


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _menu(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _night_mode_screen() -> tuple[str, InlineKeyboardMarkup]:
    current = _escaped_preview(get_night_mode_reply_text(), 3500)
    status = "✅ Включён" if is_night_mode_enabled() else "⛔ Выключен"
    text = f"😴 <b>Ночной режим</b>\n\nСтатус: {status}\n\nТекущий автоответ:\n{current}"
    return text, _menu([
        [_button("⛔ Выключить" if is_night_mode_enabled() else "✅ Включить", "toggle_night_mode")],
        [_button("✏️ Изменить автоответ", "night_edit")],
        [_button("🔄 Сбросить автоответ", "night_reset")],
        [_button("🔙 Назад", "menu_main")],
    ])


def _clean_text(value: str | None, limit: int) -> str:
    if type(value) is not str:
        raise ValueError("Нужен текст.")
    cleaned = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    cleaned = cleaned.replace("\t", " ").strip()
    if not cleaned or len(cleaned) > limit:
        raise ValueError(f"Введите текст от 1 до {limit} символов.")
    return cleaned


def _escaped_preview(value: str, limit: int) -> str:
    pieces, size = [], 0
    for char in value:
        escaped = escape(char)
        if size + len(escaped) > limit:
            pieces.append("…")
            break
        pieces.append(escaped)
        size += len(escaped)
    return "".join(pieces)


_VARIABLES = frozenset({"chat_name", "account"})


def _validate_template(text: str) -> None:
    for _, field, format_spec, conversion in Formatter().parse(text):
        if field is not None and (field not in _VARIABLES or format_spec or conversion):
            raise ValueError("Доступны только {chat_name} и {account}.")


def _expand_template(text: str, chat_name: str, account_name: str) -> str:
    _validate_template(text)
    return text.format(chat_name=chat_name, account=account_name)


def _normalized_trigger(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip().casefold()


def match_autoresponder(message: str) -> tuple[dict, dict] | None:
    if not bot_settings["autoresponder_enabled"] or type(message) is not str:
        return None
    normalized = _normalized_trigger(message)
    if not normalized:
        return None
    rules = bot_settings["autoresponder_rules"]
    for mode in ("EXACT", "CONTAINS"):
        for rule in rules:
            if not rule["enabled"] or rule["match_mode"] != mode:
                continue
            trigger = _normalized_trigger(rule["trigger"])
            if not trigger or not (normalized == trigger if mode == "EXACT" else trigger in normalized):
                continue
            template = _template(rule["template_id"])
            if template is None:
                continue
            try:
                _validate_template(template["text"])
            except ValueError:
                continue
            return rule, template
    return None


def _validate_review_text(value: str) -> None:
    for _, field, spec, conversion in Formatter().parse(value):
        if field is not None and (field not in {"account", "buyer", "order_id"}
                                  or spec or conversion):
            raise ValueError("Доступны только {account}, {buyer}, {order_id}.")


def expand_review_request_text(text: str, account: str, buyer: str, order_id: str) -> str:
    _validate_review_text(text)
    if not account or not buyer or not re.fullmatch(r"[A-Z0-9]{8}", order_id):
        raise ValueError("Недостаточно данных заказа.")
    return text.format(account=account, buyer=buyer, order_id=order_id)


def _templates() -> list[dict]:
    return bot_settings["reply_templates"]


def _template(template_id: str) -> dict | None:
    return next((item for item in _templates() if item["id"] == template_id), None)


def _templates_screen(chat_id: int = 0, *, quick: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for item in _templates():
        action = "quick_preview" if quick else "tpl_view"
        rows.append([_button(item["title"][:40], f"{action}:{item['id']}:{chat_id}")])
    if not quick:
        rows.append([_button("➕ Добавить", f"tpl_add:{chat_id}")])
    back = f"chat:{chat_id}" if quick else (f"chats:0" if chat_id == 0 else f"chat:{chat_id}")
    rows.append([_button("🔙 Назад", back)])
    return ("⚡ <b>Быстрый ответ</b>" if quick else "⚡ <b>Шаблоны</b>"), _menu(rows)


def _template_screen(item: dict, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    return (f"⚡ <b>{escape(item['title'])}</b>\n\n{_escaped_preview(item['text'], 3400)}", _menu([
        [_button("✏️ Название", f"tpl_title:{item['id']}:{chat_id}")],
        [_button("✏️ Текст", f"tpl_text:{item['id']}:{chat_id}")],
        [_button("🗑 Удалить", f"tpl_delete:{item['id']}:{chat_id}")],
        [_button("🔙 Назад", f"tpl_menu:{chat_id}")],
    ]))


def _cancel_keyboard(back: str) -> InlineKeyboardMarkup:
    return _menu([[_button("❌ Отмена", f"cancel:{back}")]])


def _rule(rule_id: str) -> dict | None:
    return next((rule for rule in bot_settings["autoresponder_rules"]
                 if rule["id"] == rule_id), None)


def _save_rule(replacement: dict | None, rule_id: str) -> None:
    updated = [replacement if rule["id"] == rule_id else rule
               for rule in bot_settings["autoresponder_rules"] if replacement is not None or rule["id"] != rule_id]
    _save_global_setting("autoresponder_rules", updated)


def _auto_screen() -> tuple[str, InlineKeyboardMarkup]:
    status = "✅ Включён" if bot_settings["autoresponder_enabled"] else "⛔ Выключен"
    return f"🤖 <b>Автоответчик</b>\n\nСтатус: {status}", _menu([
        [_button("⛔ Выключить" if bot_settings["autoresponder_enabled"] else "✅ Включить", "auto_toggle")],
        [_button("📋 Правила", "auto_rules")],
        [_button("➕ Добавить правило", "auto_add")],
        [_button("⭐ Запрос отзыва", "review_menu")],
        [_button("🔙 Назад", "menu_main")],
    ])


def _rules_screen() -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_button(("✅ " if rule["enabled"] else "⛔ ") + rule["trigger"].replace("\n", " ⏎ ")[:35],
                     f"auto_rule:{rule['id']}")]
            for rule in bot_settings["autoresponder_rules"]]
    rows.extend([[_button("➕ Добавить правило", "auto_add")],
                 [_button("🔙 Назад", "auto_menu")]])
    return "📋 <b>Правила автоответчика</b>", _menu(rows)


def _rule_screen(rule: dict) -> tuple[str, InlineKeyboardMarkup]:
    template = _template(rule["template_id"])
    name = template["title"] if template else "Недоступен"
    text = (f"🤖 <b>Правило</b> · {'✅' if rule['enabled'] else '⛔'}\n\n"
            f"Триггер: {escape(rule['trigger'][:200])}\n"
            f"Тип: {rule['match_mode']}\nШаблон: {escape(name)}")
    return text, _menu([
        [_button("⛔ Выключить" if rule["enabled"] else "✅ Включить", f"auto_rule_toggle:{rule['id']}")],
        [_button("✏️ Триггер", f"auto_rule_trigger:{rule['id']}")],
        [_button("🔁 EXACT / CONTAINS", f"auto_rule_mode:{rule['id']}")],
        [_button("⚡ Шаблон", f"auto_rule_template:{rule['id']}")],
        [_button("🗑 Удалить", f"auto_rule_delete:{rule['id']}")],
        [_button("🔙 Назад", "auto_rules")],
    ])


def _choose_rule_template(back: str, action: str) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_button(item["title"][:40], f"{action}:{item['id']}")]
            for item in _templates()]
    rows.append([_button("🔙 Назад", back)])
    return "⚡ <b>Выберите шаблон</b>", _menu(rows)


def _review_request_screen() -> tuple[str, InlineKeyboardMarkup]:
    status = "✅ Включён" if bot_settings["review_request_enabled"] else "⛔ Выключен"
    text = (f"⭐ <b>Запрос отзыва</b>\n\nСтатус: {status}\n"
            f"Текст: {_escaped_preview(bot_settings['review_request_text'], 3400)}\n"
            f"Задержка: {bot_settings['review_request_delay']} минут")
    return text, _menu([
        [_button("⛔ Выключить" if bot_settings["review_request_enabled"] else "✅ Включить", "review_toggle")],
        [_button("✏️ Изменить текст", "review_edit")],
        [_button("⏱ Изменить задержку", "review_delay")],
        [_button("🔄 Сбросить текст", "review_reset")],
        [_button("🔙 Назад", "auto_menu")],
    ])


async def _automation_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    prefix = action.split(":", 1)[0]
    if prefix not in {"auto_menu", "auto_toggle", "auto_rules", "auto_add", "auto_mode",
                      "auto_template", "auto_rule", "auto_rule_toggle", "auto_rule_trigger",
                      "auto_rule_mode", "auto_rule_template", "auto_select", "auto_rule_delete",
                      "review_menu", "review_toggle", "review_edit", "review_delay", "review_reset"}:
        return False
    if prefix not in {"auto_mode", "auto_template"}:
        _interaction_state.pop(user_id, None)
    answered = False
    try:
        parts = action.split(":")
        if prefix == "auto_menu":
            text, markup = _auto_screen()
        elif prefix == "auto_toggle":
            _save_global_setting("autoresponder_enabled", not bot_settings["autoresponder_enabled"])
            text, markup = _auto_screen()
        elif prefix == "auto_rules":
            text, markup = _rules_screen()
        elif prefix == "auto_add":
            if len(bot_settings["autoresponder_rules"]) >= 50:
                raise ValueError("Можно сохранить не более 50 правил.")
            if not _templates():
                raise ValueError("Сначала добавьте шаблон в разделе «Чаты».")
            _interaction_state[user_id] = {"action": "auto_add_trigger"}
            text, markup = "➕ Отправьте триггер (до 200 символов).", _cancel_keyboard("menu")
        elif prefix == "auto_mode":
            pending = _interaction_state.get(user_id)
            if not pending or pending.get("action") != "auto_add_mode" or parts[1] not in ("EXACT", "CONTAINS"):
                raise ValueError("Редактирование устарело.")
            _interaction_state[user_id] = {**pending, "action": "auto_add_template", "match_mode": parts[1]}
            text, markup = _choose_rule_template("auto_rules", "auto_template")
        elif prefix == "auto_template":
            pending = _interaction_state.pop(user_id, None)
            if not pending or pending.get("action") != "auto_add_template" or _template(parts[1]) is None:
                raise ValueError("Редактирование устарело.")
            if len(bot_settings["autoresponder_rules"]) >= 50:
                raise ValueError("Можно сохранить не более 50 правил.")
            rule_id = secrets.token_hex(4)
            while _rule(rule_id) is not None:
                rule_id = secrets.token_hex(4)
            rule = {"id": rule_id, "trigger": pending["trigger"], "template_id": parts[1],
                    "match_mode": pending["match_mode"], "enabled": True}
            _save_global_setting("autoresponder_rules", [*bot_settings["autoresponder_rules"], rule])
            text, markup = _rule_screen(rule)
        elif prefix in {"auto_rule", "auto_rule_toggle", "auto_rule_trigger", "auto_rule_mode",
                        "auto_rule_template", "auto_rule_delete"}:
            rule = _rule(parts[1])
            if rule is None:
                raise ValueError("Правило не найдено.")
            if prefix == "auto_rule_toggle":
                rule = {**rule, "enabled": not rule["enabled"]}
                _save_rule(rule, rule["id"])
            elif prefix == "auto_rule_mode":
                rule = {**rule, "match_mode": "CONTAINS" if rule["match_mode"] == "EXACT" else "EXACT"}
                _save_rule(rule, rule["id"])
            elif prefix == "auto_rule_trigger":
                _interaction_state[user_id] = {"action": "auto_edit_trigger", "id": rule["id"]}
                text, markup = "✏️ Отправьте новый триггер (до 200 символов).", _cancel_keyboard("menu")
            elif prefix == "auto_rule_template":
                text, markup = _choose_rule_template("auto_rules", f"auto_select:{rule['id']}")
            elif prefix == "auto_rule_delete":
                _save_rule(None, rule["id"])
                text, markup = _rules_screen()
            if prefix in {"auto_rule", "auto_rule_toggle", "auto_rule_mode"}:
                text, markup = _rule_screen(rule)
        elif prefix == "auto_select":
            rule = _rule(parts[1])
            if rule is None or _template(parts[2]) is None:
                raise ValueError("Правило или шаблон не найден.")
            rule = {**rule, "template_id": parts[2]}
            _save_rule(rule, rule["id"])
            text, markup = _rule_screen(rule)
        elif prefix == "review_menu":
            text, markup = _review_request_screen()
        elif prefix == "review_toggle":
            _save_global_setting("review_request_enabled", not bot_settings["review_request_enabled"])
            text, markup = _review_request_screen()
        elif prefix == "review_edit":
            _interaction_state[user_id] = {"action": "review_edit"}
            text, markup = "✏️ Отправьте текст запроса отзыва (до 1000 символов).", _cancel_keyboard("menu")
        elif prefix == "review_delay":
            _interaction_state[user_id] = {"action": "review_delay"}
            text, markup = "⏱ Отправьте задержку от 0 до 1440 минут.", _cancel_keyboard("menu")
        else:
            _save_global_setting("review_request_text", DEFAULT_REVIEW_REQUEST_TEXT)
            text, markup = _review_request_screen()
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except (ValueError, RuntimeError) as e:
        if not answered:
            safe_error = str(e) if str(e) in {
                "Можно сохранить не более 50 правил.",
                "Сначала добавьте шаблон в разделе «Чаты»."} else None
            await callback.answer(safe_error or "Не удалось выполнить действие или сохранить настройку.",
                                  show_alert=True)
    except Exception as e:
        logger.warning(f"Automation UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Данные недоступны.", show_alert=True)
    return True


async def _automation_pending(message: Message, pending: dict) -> bool:
    action = pending["action"]
    if action not in {"auto_add_trigger", "auto_add_mode", "auto_add_template",
                      "auto_edit_trigger", "review_edit", "review_delay"}:
        return False
    user_id = message.from_user.id
    if action in {"auto_add_mode", "auto_add_template"}:
        await message.answer("Выберите вариант кнопкой в предыдущем сообщении.")
        return True
    try:
        if action == "review_delay":
            value = _clean_text(message.text, 4)
            if not value.isdecimal() or not 0 <= int(value) <= 1440:
                raise ValueError("Введите целое число от 0 до 1440.")
            _save_global_setting("review_request_delay", int(value))
            text, markup = _review_request_screen()
        else:
            limit = 200 if action.endswith("trigger") else 1000
            value = _clean_text(message.text, limit).replace("\r\n", "\n").replace("\r", "\n")
            if action == "auto_add_trigger":
                _interaction_state[user_id] = {"action": "auto_add_mode", "trigger": value}
                await message.answer("Выберите тип совпадения:", reply_markup=_menu([
                    [_button("EXACT", "auto_mode:EXACT"), _button("CONTAINS", "auto_mode:CONTAINS")],
                    [_button("❌ Отмена", "cancel:menu")],
                ]))
                return True
            if action == "auto_edit_trigger":
                rule = _rule(pending["id"])
                if rule is None:
                    raise ValueError("Правило не найдено.")
                rule = {**rule, "trigger": value}
                _save_rule(rule, rule["id"])
                text, markup = _rule_screen(rule)
            else:
                _validate_review_text(value)
                _save_global_setting("review_request_text", value)
                text, markup = _review_request_screen()
    except ValueError as e:
        await message.answer(escape(str(e)), parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Automation settings save failed: {type(e).__name__}.")
        await message.answer("Не удалось сохранить. Настройка не изменилась.")
        return True
    _interaction_state.pop(user_id, None)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")
    return True


def _chat_snapshot(chat_id: int) -> tuple[str, str]:
    if _runtime_client is None or type(chat_id) is not int or chat_id <= 0:
        raise RuntimeError("Chat unavailable.")
    with _runtime_client._account_lock:
        account = _runtime_client.account
        shortcut = account.get_chat_by_id(chat_id)
        name = getattr(shortcut, "name", None)
        account_name = getattr(account, "username", None)
    if type(name) is not str or not name or type(account_name) is not str or not account_name:
        raise RuntimeError("Chat unavailable.")
    return name, account_name


def _fetch_chats() -> list[tuple[int, str, bool]]:
    if _runtime_client is None:
        raise RuntimeError("Account unavailable.")
    with _runtime_client._account_lock:
        account = _runtime_client.account
        chats = account.request_chats()
        account.add_chats(chats)
        return [(chat.id, chat.name or "Чат", chat.unread) for chat in chats
                if type(chat.id) is int and chat.id > 0]


def _fetch_chat(chat_id: int) -> tuple[str, list[tuple[str, str]]]:
    if _runtime_client is None:
        raise RuntimeError("Account unavailable.")
    with _runtime_client._account_lock:
        account = _runtime_client.account
        shortcut = account.get_chat_by_id(chat_id)
        if shortcut is None:
            raise RuntimeError("Chat unavailable.")
        name = shortcut.name or "Чат"
        history_method = getattr(_runtime_client, "_manual_get_chat_history", None)
        if history_method is None:
            raise RuntimeError("History unavailable.")
        try:
            messages = history_method(chat_id, interlocutor_username=name)
        except Exception as e:
            logger.warning(f"Chat history unavailable: {type(e).__name__}.")
            preview = getattr(shortcut, "last_message_text", None)
            return name, [("Последний фрагмент · история недоступна", preview)] if preview else []
        account_id = getattr(account, "id", None)
    display = []
    for item in messages[-10:]:
        author_id = getattr(item, "author_id", None)
        if type(account_id) is int and type(author_id) is int and author_id == account_id:
            direction = "🧑‍💻 я"
        elif type(author_id) is int and author_id > 0 and getattr(item, "author", None) == name:
            direction = "👤 собеседник"
        else:
            direction = "Сообщение"
        display.append((direction, getattr(item, "text", None) or "[без текста]"))
    return name, display


def _chats_screen(chats: list[tuple[int, str, bool]], page: int) -> tuple[str, InlineKeyboardMarkup]:
    count = max(1, (len(chats) + 9) // 10)
    page = min(max(0, page), count - 1)
    rows = [[_button(("🟠 " if unread else "") + name[:45], f"chat:{chat_id}")]
            for chat_id, name, unread in chats[page * 10:(page + 1) * 10]]
    pages = []
    if page > 0:
        pages.append(_button("◀️", f"chats:{page - 1}"))
    if page + 1 < count:
        pages.append(_button("▶️", f"chats:{page + 1}"))
    if pages:
        rows.append(pages)
    rows.extend([[_button("🔄 Обновить", "chats_refresh:0")],
                 [_button("⚡ Шаблоны", "tpl_menu:0")],
                 [_button("🔙 Назад", "menu_main")]])
    return f"💬 <b>Чаты</b> · {page + 1}/{count}", _menu(rows)


def _chat_screen(chat_id: int, name: str, messages: list[tuple[str, str]]) -> tuple[str, InlineKeyboardMarkup]:
    header = f"💬 <b>{escape(name[:100])}</b>\n\n"
    remaining = 3500 - len(header)
    blocks = []
    for direction, body in messages:
        block = f"{direction}: {_escaped_preview(str(body), 600)}"
        if len(block) > remaining:
            break
        blocks.append(block)
        remaining -= len(block) + 2
    text = header + ("\n\n".join(blocks) if blocks else "Сообщений нет.")
    return text, _menu([
        [_button("✉️ Ответить", f"reply:{chat_id}"), _button("⚡ Быстрый ответ", f"quick_menu:{chat_id}")],
        [_button("🔄 Обновить", f"chat:{chat_id}")],
        [_button("🔙 К чатам", "chats:0")],
    ])


def _send_to_chat(chat_id: int, value: str) -> None:
    if _runtime_client is None:
        raise RuntimeError("Account unavailable.")
    _runtime_client.send_message_once(chat_id, value)


async def _send_one_reply(message: Message, chat_id: int, value: str) -> None:
    try:
        await asyncio.to_thread(_send_to_chat, chat_id, value)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        logger.warning(f"Manual message result ambiguous: {type(e).__name__}.")
        await message.answer("⚠️ Не удалось подтвердить отправку. Проверьте чат перед повторной попыткой.")
    except FunPayAPI.exceptions.MessageNotDeliveredError as e:
        if type(getattr(e, "error_message", None)) is str and e.error_message:
            logger.warning("Manual message rejected by FunPay.")
            await message.answer("⛔ FunPay отклонил сообщение. Отправка не выполнена.")
        else:
            logger.warning("Manual message result ambiguous: MessageNotDeliveredError.")
            await message.answer("⚠️ Отправка не подтверждена. Проверьте чат перед повторной попыткой.")
    except Exception as e:
        logger.warning(f"Manual message failed: {type(e).__name__}.")
        # A remote error can occur after transport; do not imply it is safe to retry.
        await message.answer("⚠️ Отправка не подтверждена. Проверьте чат перед повторной попыткой.")
    else:
        await message.answer("✅ Отправлено")


async def _communication_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    """Handles communication screens; callback values contain IDs, never user text."""
    prefix = action.split(":", 1)[0]
    if prefix not in {"menu_night_mode", "night_edit", "night_reset", "chats", "chats_refresh",
                      "chat", "reply", "tpl_menu", "tpl_view", "tpl_add", "tpl_title",
                      "tpl_text", "tpl_delete", "quick_menu", "quick_preview", "quick_send", "cancel"}:
        return False
    if prefix != "quick_send":
        _interaction_state.pop(user_id, None)
    answered = False
    try:
        parts = action.split(":")
        if prefix == "menu_night_mode":
            text, markup = _night_mode_screen()
        elif prefix == "night_edit":
            _interaction_state[user_id] = {"action": "night_edit"}
            text, markup = "✏️ Отправьте новый автоответ (до 1000 символов).", _cancel_keyboard("night")
        elif prefix == "night_reset":
            _save_global_setting("night_mode_reply", None)
            text, markup = _night_mode_screen()
        elif prefix in {"chats", "chats_refresh"}:
            page = int(parts[1])
            if prefix == "chats_refresh" or user_id not in _chat_pages:
                _chat_pages[user_id] = await asyncio.to_thread(_fetch_chats)
            text, markup = _chats_screen(_chat_pages[user_id], page)
        elif prefix == "chat":
            chat_id = int(parts[1])
            if chat_id not in {item[0] for item in _chat_pages.get(user_id, [])}:
                raise ValueError("Chat unavailable.")
            name, messages = await asyncio.to_thread(_fetch_chat, chat_id)
            text, markup = _chat_screen(chat_id, name, messages)
        elif prefix == "reply":
            chat_id = int(parts[1])
            if chat_id not in {item[0] for item in _chat_pages.get(user_id, [])}:
                raise ValueError("Chat unavailable.")
            _interaction_state[user_id] = {"action": "reply", "chat_id": chat_id}
            text, markup = "✉️ Отправьте текст ответа.", _cancel_keyboard(f"chat_{chat_id}")
        elif prefix in {"tpl_menu", "quick_menu"}:
            chat_id = int(parts[1])
            if chat_id and chat_id not in {item[0] for item in _chat_pages.get(user_id, [])}:
                raise ValueError("Chat unavailable.")
            text, markup = _templates_screen(chat_id, quick=prefix == "quick_menu")
        elif prefix == "cancel":
            back = parts[1]
            if back == "night":
                text, markup = _night_mode_screen()
            elif back.startswith("chat_"):
                chat_id = int(back.removeprefix("chat_"))
                name, messages = await asyncio.to_thread(_fetch_chat, chat_id)
                text, markup = _chat_screen(chat_id, name, messages)
            else:
                text, markup = MAIN_MENU_TEXT, get_main_keyboard(user_id)
        elif prefix == "tpl_add":
            chat_id = int(parts[1])
            if len(_templates()) >= 20:
                raise ValueError("Можно сохранить не более 20 шаблонов.")
            _interaction_state[user_id] = {"action": "tpl_add_title", "chat_id": chat_id}
            text, markup = "➕ Отправьте название шаблона (до 40 символов).", _cancel_keyboard("menu")
        elif prefix in {"tpl_view", "tpl_title", "tpl_text", "tpl_delete", "quick_preview"}:
            template_id, chat_id = parts[1], int(parts[2])
            item = _template(template_id)
            if item is None:
                raise ValueError("Шаблон не найден.")
            if chat_id and chat_id not in {row[0] for row in _chat_pages.get(user_id, [])}:
                raise ValueError("Chat unavailable.")
            if prefix == "tpl_view":
                text, markup = _template_screen(item, chat_id)
            elif prefix in {"tpl_title", "tpl_text"}:
                _interaction_state[user_id] = {"action": prefix, "id": template_id, "chat_id": chat_id}
                text, markup = ("✏️ Отправьте новое название (до 40 символов)." if prefix == "tpl_title"
                                else "✏️ Отправьте новый текст (до 1000 символов)."), _cancel_keyboard("menu")
            elif prefix == "tpl_delete":
                if any(rule["template_id"] == template_id for rule in bot_settings["autoresponder_rules"]):
                    raise ValueError("Шаблон используется правилом. Сначала удалите правило.")
                _save_global_setting("reply_templates", [row for row in _templates() if row["id"] != template_id])
                text, markup = _templates_screen(chat_id)
            else:
                if not chat_id:
                    raise ValueError("Chat unavailable.")
                chat_name, account_name = await asyncio.to_thread(_chat_snapshot, chat_id)
                expanded = _expand_template(item["text"], chat_name, account_name)
                if len(expanded) > 2000:
                    raise ValueError("Развёрнутый текст слишком длинный.")
                _interaction_state[user_id] = {"action": "quick_confirm", "chat_id": chat_id,
                                               "text": expanded, "id": template_id}
                text = f"⚡ <b>{escape(item['title'])}</b>\n\n{_escaped_preview(expanded, 3400)}"
                markup = _menu([[_button("✅ Отправить", f"quick_send:{template_id}:{chat_id}")],
                                [_button("🔙 Назад", f"quick_menu:{chat_id}")]])
        elif prefix == "quick_send":
            template_id, chat_id = parts[1], int(parts[2])
            pending = _interaction_state.pop(user_id, None)
            if (not pending or pending.get("action") != "quick_confirm"
                    or pending.get("id") != template_id or pending.get("chat_id") != chat_id):
                raise ValueError("Подтверждение устарело.")
            await callback.answer()
            answered = True
            await _send_one_reply(callback.message, chat_id, pending["text"])
            return True
        else:
            return False
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except (ValueError, RuntimeError) as e:
        if not answered:
            await callback.answer(str(e) if isinstance(e, ValueError) and str(e) in {
                "Шаблон не найден.", "Можно сохранить не более 20 шаблонов.",
                "Шаблон используется правилом. Сначала удалите правило.",
                "Развёрнутый текст слишком длинный.", "Подтверждение устарело."}
                else "Данные недоступны или не удалось сохранить.", show_alert=True)
    except Exception as e:
        logger.warning(f"Communication UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Данные недоступны или не удалось сохранить.", show_alert=True)
    return True


MAIN_MENU_TEXT = "🎛 <b>Панель управления FunPay</b>\nВыберите действие с помощью кнопок ниже:"
NOTIFICATIONS_MENU_TEXT = "🔔 <b>Мои настройки уведомлений</b>\nНастройте, о чём вас оповещать (настройки независимые для каждого пользователя):"


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    _interaction_state.pop(user_id, None)
    if is_authorized(user_id):
        await message.answer(
            MAIN_MENU_TEXT,
            reply_markup=get_reply_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer("🔒 <b>Доступ закрыт.</b>\nВведите пароль:", parse_mode="HTML")


def _status_message_text() -> str:
    try:
        return get_runtime_status_text()
    except Exception as e:
        logger.warning(f"Status unavailable: {type(e).__name__}.")
        return "🩺 Состояние бота\nRuntime: недоступно"


async def _send_status(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    await message.answer(_status_message_text())


async def _read_legacy_stats(period: str) -> dict:
    store = _runtime_client.review_state if _runtime_client is not None else None
    if store is None:
        raise StateError("Persistent state unavailable.")
    return await asyncio.to_thread(store.get_legacy_statistics, period)


_STATS_PERIOD_TITLES = {
    "today": "за сегодня",
    "week": "за неделю",
    "month": "за месяц",
}


def format_stats_text(period: str, data: dict) -> str:
    """Старый компактный layout; оборот содержит только подтверждённые USD."""
    title = _STATS_PERIOD_TITLES[period]
    return (
        f"📊 <b>Статистика {title}</b>\n\n"
        f"🛒 Заказов: <b>{data['orders_count']}</b>\n"
        f"🌟 Отзывов: <b>{data['reviews_count']}</b>\n"
        f"💰 Оборот: <b>{data['usd_turnover']:.2f} $</b>\n"
        f"🏦 Выводов: <b>{data['withdrawals_count']}</b> "
        f"на сумму <b>{data['usd_withdrawals']:.2f} $</b>"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await _send_status(message)


@dp.message(Command("log"))
async def cmd_log(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите дату: /log ГГГГ-ММ-ДД. Сегодня и вчера доступны в меню «Лог».")
        return
    date_str = parts[1].strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            raise ValueError("Invalid log date")
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат даты. Нужно: ГГГГ-ММ-ДД.")
        return
    await _send_log_file(message, date_str)


@dp.callback_query()
async def callback_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_authorized(user_id):
        _interaction_state.pop(user_id, None)
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return

    action = callback.data or ""
    if not action or not action.startswith(("quick_send:", "auto_mode:", "auto_template:")):
        _interaction_state.pop(user_id, None)
    if await _communication_callback(callback, action, user_id):
        return
    if await _automation_callback(callback, action, user_id):
        return

    # Включение/выключение автоподнятия (глобальная настройка — влияет на всех)
    if action == "toggle_bump":
        enabling = not bot_settings["auto_bump"]
        bot_settings["auto_bump"] = enabling
        try:
            save_settings(required=True)
        except RuntimeError:
            if enabling:
                bot_settings["auto_bump"] = False
            await callback.answer("Не удалось сохранить настройку. Автоподнятие не включено.", show_alert=True)
            return
        state_text = "включено" if bot_settings["auto_bump"] else "выключено"
        await callback.answer(f"Автоподнятие {state_text}!")
        try:
            await callback.message.edit_reply_markup(reply_markup=get_main_keyboard(user_id))
        except Exception:
            pass

    # Переход в персональное подменю уведомлений
    elif action == "menu_notifications":
        await callback.answer()
        try:
            await callback.message.edit_text(
                NOTIFICATIONS_MENU_TEXT,
                reply_markup=get_notifications_keyboard(user_id),
                parse_mode="HTML"
            )
        except Exception:
            pass

    elif action == "toggle_night_mode":
        try:
            enabled = toggle_night_mode_saved()
        except RuntimeError:
            await callback.answer("Не удалось сохранить ночной режим.", show_alert=True)
            return
        await callback.answer("Ночной режим включен 😴" if enabled
                              else "Ночной режим выключен")
        try:
            text, markup = _night_mode_screen()
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass

    elif action == "menu_logs":
        await callback.answer()
        try:
            await callback.message.edit_text(
                "📄 <b>Лог бота</b>\nВыберите дату или отправьте /log ГГГГ-ММ-ДД:",
                reply_markup=get_logs_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass

    elif action in ("log_today", "log_yesterday"):
        date = datetime.now() if action == "log_today" else datetime.now() - timedelta(days=1)
        await callback.answer("Отправляю файл...")
        await _send_log_file(callback, date.strftime("%Y-%m-%d"))

    elif action == "menu_stats":
        await callback.answer()
        try:
            await callback.message.edit_text(
                "📊 <b>Статистика</b>\nВыберите период:",
                reply_markup=get_stats_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass

    elif action == "menu_status":
        await callback.answer()
        await callback.message.answer(_status_message_text())

    elif action in ("stats_today", "stats_week", "stats_month"):
        period = action.removeprefix("stats_")
        try:
            stats = await _read_legacy_stats(period)
            text = format_stats_text(period, stats)
        except Exception as e:
            logger.warning(f"Order statistics unavailable: {type(e).__name__}.")
            await callback.answer("Статистика недоступна.", show_alert=True)
            return
        await callback.answer()
        try:
            await callback.message.edit_text(
                text, reply_markup=get_stats_keyboard(), parse_mode="HTML"
            )
        except Exception:
            pass

    # Возврат в главное меню
    elif action == "menu_main":
        await callback.answer()
        try:
            await callback.message.edit_text(
                MAIN_MENU_TEXT,
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Обработка персональных тумблеров уведомлений
    elif action.startswith("notif_"):
        key = (
            "notifications_enabled"
            if action == "notif_notifications_enabled"
            else action.replace("notif_", "notify_", 1)
        )
        u = get_user_settings(user_id)  # берём настройки ЭТОГО пользователя
        if key in u:
            u[key] = not u[key]
            save_settings()
            await callback.answer("Настройка обновлена!")
            try:
                await callback.message.edit_reply_markup(reply_markup=get_notifications_keyboard(user_id))
            except Exception:
                pass


@dp.message()
async def text_handler(message: Message):
    user_id = message.from_user.id

    if is_authorized(user_id):
        # Если нажата кнопка вызова контекстного меню
        if message.text == "🛠 Главное меню":
            _interaction_state.pop(user_id, None)
            await message.answer(
                MAIN_MENU_TEXT,
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
            )
            return
        if message.text == "❌ Отмена":
            _interaction_state.pop(user_id, None)
            await message.answer("Отменено.", reply_markup=get_main_keyboard(user_id))
            return
        pending = _interaction_state.get(user_id)
        if pending:
            if message.text and message.text.startswith("/"):
                _interaction_state.pop(user_id, None)
                await message.answer("Действие отменено. Команда не сохранена.")
                return
            if await _automation_pending(message, pending):
                return
            if pending["action"] == "quick_confirm":
                await message.answer("Для отправки используйте кнопку «✅ Отправить».")
                return
            try:
                limit = 40 if pending["action"] in {"tpl_add_title", "tpl_title"} else (
                    2000 if pending["action"] == "reply" else 1000)
                value = _clean_text(message.text, limit)
                if pending["action"] in {"tpl_add_text", "tpl_text"}:
                    _validate_template(value)
            except ValueError as e:
                await message.answer(escape(str(e)), parse_mode="HTML")
                return
            action = pending["action"]
            if action == "reply":
                _interaction_state.pop(user_id, None)
                await _send_one_reply(message, pending["chat_id"], value)
                return
            if action == "tpl_add_title":
                _interaction_state[user_id] = {**pending, "action": "tpl_add_text", "title": value}
                await message.answer("✏️ Отправьте текст шаблона (до 1000 символов).",
                                     reply_markup=_cancel_keyboard("menu"))
                return
            try:
                if action == "night_edit":
                    _save_global_setting("night_mode_reply", value)
                    text, markup = _night_mode_screen()
                elif action == "tpl_add_text":
                    if len(_templates()) >= 20:
                        raise ValueError("Можно сохранить не более 20 шаблонов.")
                    template_id = secrets.token_hex(4)
                    while _template(template_id) is not None:
                        template_id = secrets.token_hex(4)
                    item = {"id": template_id, "title": pending["title"], "text": value}
                    _save_global_setting("reply_templates", [*_templates(), item])
                    text, markup = _templates_screen(pending["chat_id"])
                else:
                    item = _template(pending["id"])
                    if item is None:
                        raise ValueError("Шаблон не найден.")
                    replacement = {**item, "title" if action == "tpl_title" else "text": value}
                    _save_global_setting("reply_templates", [replacement if row["id"] == item["id"]
                                                          else row for row in _templates()])
                    text, markup = _template_screen(replacement, pending["chat_id"])
            except Exception as e:
                logger.warning(f"Communication settings save failed: {type(e).__name__}.")
                await message.answer("Не удалось сохранить. Настройка не изменилась.")
                return
            _interaction_state.pop(user_id, None)
            await message.answer(text, reply_markup=markup, parse_mode="HTML")
        return

    _interaction_state.pop(user_id, None)
    # Проверяем блокировку до сравнения пароля — не тратим время на сравнение,
    # если пользователь уже заблокирован после превышения лимита попыток.
    if _is_rate_limited(user_id):
        await message.answer(
            f"⏳ Слишком много неверных попыток. Попробуй снова через "
            f"{_BLOCK_SECONDS // 60} мин."
        )
        return

    bot_password = os.getenv("BOT_PASSWORD", "")

    # FIX: используем hmac.compare_digest вместо == для защиты от timing attack.
    # Оба операнда приводим к bytes — compare_digest требует одинаковые типы.
    if bot_password and hmac.compare_digest(
        (message.text or "").encode(),
        bot_password.encode(),
    ):
        _reset_failed_attempts(user_id)
        authorized_users.add(user_id)
        # Сохраняем список авторизованных между перезапусками
        bot_settings["authorized_user_ids"] = list(authorized_users)
        # Инициализируем персональные настройки нового пользователя дефолтными значениями,
        # если у него ещё нет своей записи (например, первый вход)
        if user_id not in _user_settings:
            _user_settings[user_id] = dict(_DEFAULT_USER_SETTINGS)
        save_settings()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔓 <b>Авторизация успешна!</b>", reply_markup=get_reply_keyboard(), parse_mode="HTML")
        await message.answer(MAIN_MENU_TEXT, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
    else:
        remaining = _record_failed_attempt(user_id)
        if remaining == 0:
            await message.answer(
                f"❌ Неверный пароль. Вы заблокированы на {_BLOCK_SECONDS // 60} мин. "
                f"из-за превышения числа попыток."
            )
        else:
            await message.answer(f"❌ Неверный пароль. Осталось попыток: {remaining}.")
