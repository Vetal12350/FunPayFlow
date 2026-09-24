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
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
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
_telegram_polling_active: bool | None = None
_withdrawal_failures = 0
_last_withdrawal_success_monotonic: float | None = None
_problems_lock = threading.Lock()
_active_problems: dict[str, dict] = {}
_PROBLEM_TYPES = {
    "RUNNER_STOPPED": ("ERROR", "FunPay Runner остановлен."),
    "RUNNER_RETRYING": ("WARNING", "Повторяются ошибки FunPay Runner."),
    "QUEUE_PRESSURE": ("WARNING", "Очередь событий близка к заполнению."),
    "DB_UNAVAILABLE": ("ERROR", "Persistent SQLite недоступна."),
    "WITHDRAWAL_REPEATED": ("WARNING", "Повторяются ошибки проверки выводов."),
    "REVIEW_WORKER": ("ERROR", "Задача проверки отзывов остановилась с ошибкой."),
    "SETTINGS_PERSISTENCE": ("ERROR", "Не удалось сохранить настройки."),
    "AUDIT_UNAVAILABLE": ("WARNING", "Журнал действий временно недоступен."),
    "CRITICAL_TASK": ("ERROR", "Критическая фоновая задача остановилась."),
    "BACKLOG_UNAVAILABLE": ("ERROR", "Очередь важных событий SQLite недоступна."),
    "BACKLOG_MALFORMED": ("WARNING", "Некорректная запись важного события изолирована."),
}


def _set_problem(code: str) -> bool:
    if code not in _PROBLEM_TYPES:
        raise ValueError("Invalid problem code.")
    now = int(time.time())
    severity, description = _PROBLEM_TYPES[code]
    with _problems_lock:
        previous = _active_problems.get(code)
        _active_problems[code] = {
            "code": code, "severity": severity, "description": description,
            "first_seen": previous["first_seen"] if previous else now,
            "last_seen": now,
        }
        return previous is None


def _clear_problem(code: str) -> None:
    with _problems_lock:
        _active_problems.pop(code, None)


def set_telegram_polling_state(active: bool | None) -> None:
    global _telegram_polling_active
    _telegram_polling_active = active


def record_withdrawal_poll(success: bool) -> None:
    global _withdrawal_failures, _last_withdrawal_success_monotonic
    if success:
        _withdrawal_failures = 0
        _last_withdrawal_success_monotonic = time.monotonic()
        _clear_problem("WITHDRAWAL_REPEATED")
    else:
        _withdrawal_failures += 1
        if _withdrawal_failures >= 3:
            _set_problem("WITHDRAWAL_REPEATED")


def _sync_runtime_problems() -> None:
    client = _runtime_client
    if client is None:
        return
    try:
        health = client.get_runner_health()
        if health.get("state") in ("failed", "stopped"):
            _set_problem("RUNNER_STOPPED")
        else:
            _clear_problem("RUNNER_STOPPED")
        if (health.get("state") == "backoff"
                and type(health.get("consecutive_errors")) is int
                and health["consecutive_errors"] >= 3):
            _set_problem("RUNNER_RETRYING")
        else:
            _clear_problem("RUNNER_RETRYING")
    except Exception:
        pass
    try:
        size, capacity = client.event_queue.qsize(), client.event_queue.maxsize
        if type(size) is int and type(capacity) is int and capacity > 0 and size >= capacity * 0.8:
            _set_problem("QUEUE_PRESSURE")
        else:
            _clear_problem("QUEUE_PRESSURE")
    except Exception:
        pass
    try:
        if getattr(client, "_review_state_failed", False):
            raise StateError("Persistent state unavailable.")
        client.review_state.count_pending_review_requests()
    except Exception:
        _set_problem("DB_UNAVAILABLE")
    else:
        _clear_problem("DB_UNAVAILABLE")


def get_active_problems() -> list[dict]:
    _sync_runtime_problems()
    with _problems_lock:
        return sorted((dict(item) for item in _active_problems.values()),
                      key=lambda item: (item["severity"] != "ERROR", item["first_seen"], item["code"]))
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
    global _runtime_client, _runtime_started_at, _telegram_polling_active
    _runtime_client = None
    _runtime_started_at = None
    _telegram_polling_active = None


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
    """Local runtime snapshot and one bounded SQLite count; no network calls."""
    client = _runtime_client
    now = time.monotonic()
    if client is None or _runtime_started_at is None:
        return ("🩺 Состояние бота\nRuntime: не запущен\n"
                f"SAFE_MODE: {'включён' if is_safe_mode_enabled() else 'выключен'}")

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
            raise StateError("Persistent state unavailable.")
        pending = client.review_state.count_pending_review_requests()
        persistent = "доступно"
        _clear_problem("DB_UNAVAILABLE")
    except Exception:
        pending = None
        persistent = "недоступно"
        _set_problem("DB_UNAVAILABLE")

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
        f"FunPay Runner: {runner}",
        "Telegram: " + ("polling task запущена" if _telegram_polling_active is True
                        else "polling task остановлена" if _telegram_polling_active is False
                        else "недоступно"),
        f"Последний успешный poll: {last_success}",
        f"Ошибок подряд: {errors_text}",
        f"Очередь событий: {queue_text}",
        f"SQLite: {persistent}",
        f"Ожидающих запросов отзыва: {pending if pending is not None else 'недоступно'}",
        f"Автоподнятие: {bump_text}",
        f"Night Mode: {'включён' if is_night_mode_enabled() else 'выключен'}",
        f"Запрос отзыва: {'включён' if is_review_request_enabled() else 'выключен'}",
        f"SAFE_MODE: {'включён' if is_safe_mode_enabled() else 'выключен'}",
        "Withdrawal polling: " + ("ошибок подряд " + str(_withdrawal_failures)
                                 if _withdrawal_failures else "последняя проверка успешна"
                                 if _last_withdrawal_success_monotonic is not None
                                 else "ещё не проверялось"),
    ]
    if _last_withdrawal_success_monotonic is not None:
        lines.append("Последняя успешная проверка выводов: " +
                     _elapsed_since(_last_withdrawal_success_monotonic, now))
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
OLD_DEFAULT_REVIEW_REQUEST_TEXT = "Спасибо за покупку! Если всё понравилось, пожалуйста, оставьте отзыв о заказе."
DEFAULT_REVIEW_REQUEST_TEXT = OLD_DEFAULT_REVIEW_REQUEST_TEXT + "\n\n{order_url}"
DEFAULT_REVIEW_REQUEST_DELAY_SECONDS = 300

# Глобальные настройки — общие для всего бота
_DEFAULT_GLOBAL_SETTINGS: dict = {
    "auto_bump": False,
    "night_mode": False,
    "safe_mode": False,
    "authorized_user_ids": [],
    # OLD production-аккаунт был USD; это явная account-level настройка статистики.
    "stats_currency": "USD",
    "night_mode_reply": None,
    "review_request_enabled": False,
    "review_request_text": DEFAULT_REVIEW_REQUEST_TEXT,
    "review_request_delay_seconds": DEFAULT_REVIEW_REQUEST_DELAY_SECONDS,
}

# Дефолтные персональные настройки — используются при первой авторизации нового пользователя
_DEFAULT_USER_SETTINGS: dict = {
    "notifications_enabled": True,
    "notify_bump": True,
    "notify_message": True,
    "notify_order": True,
    "notify_review": True,
    "notify_system": True,
}

# Глобальные настройки бота (авто-подъём, список авторизованных)
bot_settings: dict = dict(_DEFAULT_GLOBAL_SETTINGS)
_night_mode_state_lock = threading.Lock()
_night_reply_echoes = deque([NIGHT_MODE_MESSAGE_TEXT, NIGHT_MODE_ORDER_TEXT], maxlen=8)
_interaction_state: dict[int, dict] = {}

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
                or type(saved.get("safe_mode", False)) is not bool
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
        if (custom_reply is not None and (type(custom_reply) is not str or not custom_reply.strip()
                                          or len(custom_reply) > 1000)):
            saved["night_mode_reply"] = None
            saved["night_mode"] = False
            print("[SETTINGS] Некорректный night_mode_reply пропущен; автоответ выключен.")
        review_text = saved.get("review_request_text", DEFAULT_REVIEW_REQUEST_TEXT)
        if type(review_text) is not str or not review_text.strip() or len(review_text) > 1000:
            saved["review_request_text"] = DEFAULT_REVIEW_REQUEST_TEXT
            saved["review_request_enabled"] = False
            print("[SETTINGS] Некорректный review_request_text пропущен; автоотправка выключена.")
        elif review_text == OLD_DEFAULT_REVIEW_REQUEST_TEXT:
            saved["review_request_text"] = DEFAULT_REVIEW_REQUEST_TEXT
        if "review_request_delay_seconds" in saved:
            review_delay = saved["review_request_delay_seconds"]
        else:
            old_minutes = saved.get("review_request_delay", 5)
            review_delay = old_minutes * 60 if type(old_minutes) is int and 0 <= old_minutes <= 1440 else None
        if type(review_delay) is not int or not 0 <= review_delay <= 86400:
            review_delay = DEFAULT_REVIEW_REQUEST_DELAY_SECONDS
            saved["review_request_enabled"] = False
            print("[SETTINGS] Некорректная задержка запроса отзыва пропущена; автоотправка выключена.")
        saved["review_request_delay_seconds"] = review_delay

        # --- Миграция старого формата (без user_settings) ---
        # Раньше notify_* хранились в корне — теперь они персональные.
        # При обнаружении старого формата переносим их в настройки ADMIN_ID.
        loaded_user_settings: dict[int, dict] = {}
        if "user_settings" not in saved:
            old_user_keys = {"notifications_enabled", "notify_bump", "notify_message", "notify_order", "notify_review"}
            migrated: dict = {}
            for key in old_user_keys:
                if key in saved:
                    migrated[key] = saved[key]
            if migrated:
                admin_id_str = os.getenv("ADMIN_ID", "")
                if admin_id_str.isdigit():
                    loaded_user_settings[int(admin_id_str)] = {**_DEFAULT_USER_SETTINGS, **migrated}
            print("[SETTINGS] Выполнена миграция настроек из старого формата в новый (персональные уведомления).")

        # Загружаем глобальные ключи
        bot_settings.clear()
        bot_settings.update(_DEFAULT_GLOBAL_SETTINGS)
        for key in _DEFAULT_GLOBAL_SETTINGS:
            if key in saved:
                bot_settings[key] = saved[key]
        bot_settings["safe_mode"] = saved.get("safe_mode", False)
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
                loaded_user_settings[uid] = merged
            except (ValueError, TypeError):
                pass

        _user_settings.clear()
        _user_settings.update(loaded_user_settings)

        print("[SETTINGS] Настройки загружены.")
    except Exception as e:
        raise RuntimeError(f"Persisted settings unavailable: {type(e).__name__}.") from None


def save_settings(*, required: bool = False) -> None:
    """Атомарно сохраняет настройки; required не скрывает ошибку записи."""
    temporary_path = None
    try:
        data = {
            "auto_bump": bot_settings["auto_bump"],
            "night_mode": bot_settings["night_mode"],
            "safe_mode": bot_settings["safe_mode"],
            "authorized_user_ids": bot_settings["authorized_user_ids"],
            "stats_currency": bot_settings["stats_currency"],
            "night_mode_reply": bot_settings["night_mode_reply"],
            "review_request_enabled": bot_settings["review_request_enabled"],
            "review_request_text": bot_settings["review_request_text"],
            "review_request_delay_seconds": bot_settings["review_request_delay_seconds"],
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
        _clear_problem("SETTINGS_PERSISTENCE")
    except Exception as e:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        if required:
            _set_problem("SETTINGS_PERSISTENCE")
            raise RuntimeError("Не удалось сохранить настройки.") from None
        _set_problem("SETTINGS_PERSISTENCE")
        print(f"[SETTINGS] Не удалось сохранить настройки: {type(e).__name__}.")


def is_night_mode_enabled() -> bool:
    with _night_mode_state_lock:
        return bot_settings.get("night_mode", False)


def is_safe_mode_enabled() -> bool:
    with _night_mode_state_lock:
        return bot_settings.get("safe_mode", False)


def toggle_safe_mode_saved(*, actor: str = "system") -> bool:
    """Persist the emergency gate before returning its new state."""
    with _night_mode_state_lock:
        previous = bot_settings["safe_mode"]
        bot_settings["safe_mode"] = not previous
        try:
            save_settings(required=True)
        except Exception:
            bot_settings["safe_mode"] = previous
            raise RuntimeError("Не удалось сохранить SAFE_MODE.") from None
        enabled = bot_settings["safe_mode"]
    _audit_action(actor, "SAFE_MODE", "global", "ON" if enabled else "OFF")
    return enabled


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


def _audit_action(actor: str, action: str, target: str, result: str) -> None:
    store = getattr(_runtime_client, "review_state", None)
    if store is None:
        return
    try:
        store.record_audit_event(actor, action, target, result)
        _clear_problem("AUDIT_UNAVAILABLE")
    except Exception:
        _set_problem("AUDIT_UNAVAILABLE")


def _save_global_setting(key: str, value, *, actor: str = "system") -> None:
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
    action = "REVIEW_REQUEST" if key == "review_request_enabled" else "SETTINGS"
    result = ("ON" if value else "OFF") if key == "review_request_enabled" else "UPDATED"
    _audit_action(actor, action, "global", result)


def toggle_night_mode_saved(*, actor: str = "system") -> bool:
    """Фиксирует состояние и required-save независимо от Account RLock."""
    with _night_mode_state_lock:
        previous = bot_settings["night_mode"]
        bot_settings["night_mode"] = not previous
        try:
            save_settings(required=True)
        except Exception:
            bot_settings["night_mode"] = previous
            raise RuntimeError("Не удалось сохранить ночной режим.") from None
        enabled = bot_settings["night_mode"]
    _audit_action(actor, "NIGHT_MODE", "global", "ON" if enabled else "OFF")
    return enabled


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
        [InlineKeyboardButton(text="📈 Аналитика", callback_data="analytics")],
        [InlineKeyboardButton(text="📦 Заказы", callback_data="orders")],
        [InlineKeyboardButton(text="🩺 Статус", callback_data="menu_status")],
        [InlineKeyboardButton(text="⚙️ Система", callback_data="system")],
        [InlineKeyboardButton(text="🔔 Мои уведомления", callback_data="menu_notifications")],
        [InlineKeyboardButton(text=f"😴 Ночной режим: {night_status}", callback_data="menu_night_mode")],
        [InlineKeyboardButton(text="⭐ Запрос отзыва", callback_data="review_menu")],
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
        [InlineKeyboardButton(text=f"⚠️ Системные события: {on_icon(u['notify_system'])}",
                              callback_data="notif_system")],
        [InlineKeyboardButton(text="🔙 Назад",
                              callback_data="menu_main")],
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


def _validate_review_text(value: str) -> None:
    for _, field, spec, conversion in Formatter().parse(value):
        if field is not None and (field not in {"account", "buyer", "order_id", "order_url"}
                                  or spec or conversion):
            raise ValueError("Доступны только {account}, {buyer}, {order_id}, {order_url}.")


def expand_review_request_text(text: str, account: str, buyer: str, order_id: str) -> str:
    _validate_review_text(text)
    if not account or not buyer or not re.fullmatch(r"[A-Z0-9]{8}", order_id):
        raise ValueError("Недостаточно данных заказа.")
    return text.format(account=account, buyer=buyer, order_id=order_id,
                       order_url=f"https://funpay.com/orders/{order_id}/")


def _cancel_keyboard(back: str) -> InlineKeyboardMarkup:
    return _menu([[_button("❌ Отмена", f"cancel:{back}")]])


def _review_delay_label(seconds: int) -> str:
    if seconds == 0:
        return "сразу"
    if seconds < 60:
        return f"{seconds} сек"
    return f"{seconds // 60} мин" if seconds % 60 == 0 else f"{seconds // 60} мин {seconds % 60} сек"


def _review_request_screen() -> tuple[str, InlineKeyboardMarkup]:
    status = "✅ Включён" if bot_settings["review_request_enabled"] else "⛔ Выключен"
    text = (f"⭐ <b>Запрос отзыва</b>\n\nСтатус: {status}\n"
            f"Текст: {_escaped_preview(bot_settings['review_request_text'], 3400)}\n"
            f"Задержка: {_review_delay_label(bot_settings['review_request_delay_seconds'])}")
    return text, _menu([
        [_button("⛔ Выключить" if bot_settings["review_request_enabled"] else "✅ Включить", "review_toggle")],
        [_button("✏️ Изменить текст", "review_edit")],
        [_button("⏱ Изменить задержку", "review_delay")],
        [_button("🔄 Сбросить текст", "review_reset")],
        [_button("🔙 Назад", "menu_main")],
    ])


def _review_delay_screen() -> tuple[str, InlineKeyboardMarkup]:
    options = ((0, "⚡ Сразу"), (5, "5 сек"), (10, "10 сек"),
               (30, "30 сек"), (60, "1 мин"), (300, "5 мин"))
    rows = [[_button(label, f"review_delay:{seconds}")] for seconds, label in options]
    rows += [[_button("✏️ Своя задержка", "review_delay_custom")],
             [_button("🔙 Назад", "review_menu")]]
    return "⏱ <b>Задержка запроса отзыва</b>", _menu(rows)


async def _review_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    prefix = action.split(":", 1)[0]
    if prefix not in {"review_menu", "review_toggle", "review_edit", "review_delay",
                      "review_delay_custom", "review_reset"}:
        return False
    _interaction_state.pop(user_id, None)
    answered = False
    try:
        if action == "review_menu":
            text, markup = _review_request_screen()
        elif action == "review_toggle":
            _save_global_setting("review_request_enabled", not bot_settings["review_request_enabled"],
                                 actor=f"telegram:{user_id}")
            text, markup = _review_request_screen()
        elif action == "review_edit":
            _interaction_state[user_id] = {"action": "review_edit"}
            text, markup = "✏️ Отправьте текст запроса отзыва (до 1000 символов).", _cancel_keyboard("review")
        elif action == "review_delay":
            text, markup = _review_delay_screen()
        elif prefix == "review_delay" and len(action.split(":")) == 2:
            seconds = int(action.split(":", 1)[1])
            if seconds not in (0, 5, 10, 30, 60, 300):
                raise ValueError("Invalid review delay.")
            _save_global_setting("review_request_delay_seconds", seconds, actor=f"telegram:{user_id}")
            text, markup = _review_request_screen()
        elif action == "review_delay_custom":
            _interaction_state[user_id] = {"action": "review_delay_custom"}
            text, markup = "⏱ Отправьте задержку от 0 до 86400 секунд.", _cancel_keyboard("review")
        elif action == "review_reset":
            _save_global_setting("review_request_text", DEFAULT_REVIEW_REQUEST_TEXT,
                                 actor=f"telegram:{user_id}")
            text, markup = _review_request_screen()
        else:
            raise ValueError("Invalid review action.")
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Review request UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Настройка недоступна.", show_alert=True)
    return True


async def _review_pending(message: Message, pending: dict) -> bool:
    action = pending.get("action")
    if action not in {"review_edit", "review_delay_custom"}:
        return False
    try:
        if action == "review_edit":
            value = _clean_text(message.text, 1000)
            _validate_review_text(value)
            _save_global_setting("review_request_text", value,
                                 actor=f"telegram:{message.from_user.id}")
        else:
            value = _clean_text(message.text, 5)
            if not value.isdecimal() or not 0 <= int(value) <= 86400:
                raise ValueError("Введите целое число от 0 до 86400 секунд.")
            _save_global_setting("review_request_delay_seconds", int(value),
                                 actor=f"telegram:{message.from_user.id}")
    except ValueError as e:
        await message.answer(escape(str(e)), parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Review request setting unavailable: {type(e).__name__}.")
        await message.answer("Не удалось сохранить. Настройка не изменилась.")
        return True
    _interaction_state.pop(message.from_user.id, None)
    text, markup = _review_request_screen()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")
    return True


_ORDER_KINDS = {"all": (None, "🕘 Последние заказы"),
                "closed": ("CLOSED", "✅ Завершённые"),
                "refunded": ("REFUNDED", "↩️ Возвраты")}
_ORDER_ICONS = {"PAID": "🆕", "CLOSED": "✅", "REFUNDED": "↩️"}


def _orders_store():
    if _runtime_client is None or getattr(_runtime_client, "review_state", None) is None:
        raise RuntimeError("Order history unavailable.")
    return _runtime_client.review_state


def _orders_screen() -> tuple[str, InlineKeyboardMarkup]:
    return "📦 <b>Заказы</b>\nВыберите список:", _menu([
        [_button("🕘 Последние заказы", "ord_list:all:0")],
        [_button("✅ Завершённые", "ord_list:closed:0")],
        [_button("↩️ Возвраты", "ord_list:refunded:0")],
        [_button("🔙 Назад", "menu_main")],
    ])


def _order_list_screen(kind: str, page: int, count: int, orders: list[dict]):
    if kind not in _ORDER_KINDS or page < 0:
        raise ValueError("Invalid order page.")
    total_pages = max(1, (count + 9) // 10)
    rows = []
    for order in orders:
        order_id = order["order_id"]
        product = order["product_description"]
        label = f"{_ORDER_ICONS.get(order['current_status'], '📦')} {order_id}"
        if product:
            label += " · " + " ".join(product.split())[:32]
        rows.append([_button(label, f"ord_open:{order_id}:{kind}:{page}")])
    if not rows:
        rows.append([_button("Заказов пока нет", "orders")])
    pages = []
    if page > 0:
        pages.append(_button("◀️", f"ord_list:{kind}:{page - 1}"))
    if page + 1 < total_pages:
        pages.append(_button("▶️", f"ord_list:{kind}:{page + 1}"))
    if pages:
        rows.append(pages)
    rows.append([_button("🔙 К заказам", "orders")])
    return (f"📦 <b>{_ORDER_KINDS[kind][1]}</b> · {page + 1}/{total_pages}",
            _menu(rows))


def _order_card_screen(order: dict, kind: str, page: int):
    status_names = {"PAID": "🆕 Оплачен", "CLOSED": "✅ Завершён",
                    "REFUNDED": "↩️ Возврат"}
    lines = [f"📦 <b>Заказ #{escape(order['order_id'])}</b>",
             f"Статус: {status_names.get(order['current_status'], 'Неизвестен')}"]
    if order.get("buyer_username"):
        lines.append("👤 Покупатель: " + _escaped_preview(order["buyer_username"], 100))
    if order.get("product_description"):
        lines.append("🛒 Товар: " + _escaped_preview(order["product_description"], 600))
    if order.get("subcategory_name"):
        lines.append("🏷 Подкатегория: " + _escaped_preview(order["subcategory_name"], 100))
    if order.get("quantity") is not None:
        lines.append(f"🔢 Количество: {order['quantity']}")
    if order.get("listed_price") is not None:
        price = escape(order["listed_price"])
        currency = order.get("confirmed_currency")
        lines.append("💰 Цена: " + price + (" " + escape(currency) if currency else ""))
    lines.append("🕒 Впервые получен ботом: " +
                 datetime.fromtimestamp(order["first_seen_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    if order.get("funpay_order_date_local"):
        lines.append("📅 Дата в FunPay (локальная): " +
                     escape(order["funpay_order_date_local"].replace("T", " ")))
    status_dates = order.get("observed_statuses", {})
    for status, label in (("CLOSED", "✅ Завершён замечен"),
                          ("REFUNDED", "↩️ Возврат замечен")):
        if status in status_dates:
            lines.append(label + ": " +
                         datetime.fromtimestamp(status_dates[status], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    rows = [[_button("🔙 К заказам", f"ord_list:{kind}:{page}")]]
    return "\n".join(lines), _menu(rows)


async def _orders_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    prefix = action.split(":", 1)[0]
    if prefix not in {"orders", "ord_list", "ord_open"}:
        return False
    answered = False
    try:
        parts = action.split(":")
        if prefix == "orders" and len(parts) == 1:
            text, markup = _orders_screen()
        elif prefix == "ord_list" and len(parts) == 3:
            kind, page = parts[1], int(parts[2])
            if kind not in _ORDER_KINDS or page < 0 or page > 100000:
                raise ValueError("Invalid order page.")
            count, rows = await asyncio.to_thread(
                _orders_store().list_order_history, _ORDER_KINDS[kind][0], page,
            )
            text, markup = _order_list_screen(kind, page, count, rows)
        elif prefix == "ord_open" and len(parts) == 4:
            order_id, kind, page = parts[1], parts[2], int(parts[3])
            if kind not in _ORDER_KINDS or page < 0 or page > 100000:
                raise ValueError("Invalid order page.")
            order = await asyncio.to_thread(_orders_store().get_order_history, order_id)
            if order is None:
                raise ValueError("Order unavailable.")
            text, markup = _order_card_screen(order, kind, page)
        else:
            raise ValueError("Invalid order action.")
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Order UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Заказы недоступны.", show_alert=True)
    return True


_ANALYTICS_PERIODS = {"today": "Сегодня", "7d": "7 дней",
                      "30d": "30 дней", "all": "Всё время"}
_ANALYTICS_NOTE = "ℹ️ Аналитика строится по данным, сохранённым ботом."


def _analytics_money(value) -> str:
    if value is None:
        return "нет данных"
    amount = Decimal(str(value))
    with localcontext() as context:
        context.prec = max(context.prec, len(amount.as_tuple().digits) + 4)
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f} $"


def _analytics_percent(numerator: int, denominator: int) -> str:
    if not denominator:
        return "нет данных"
    percent = (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent:.1f}%"


def _analytics_change(current, previous) -> str:
    if previous is None or current is None:
        return ""
    current, previous = Decimal(str(current)), Decimal(str(previous))
    if previous == 0:
        return " (новые данные)" if current > 0 else ""
    change = ((current - previous).copy_abs() * 100 / previous).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)
    direction = "↑" if current > previous else "↓" if current < previous else "→"
    return f" ({direction} {change:.1f}%)"


def _analytics_product(value) -> str:
    return _escaped_preview(value, 120) if value else "товар не указан"


def _analytics_sections(period: str):
    return [
        [_button("🏆 Топ товаров", f"ana_top:{period}:count")],
        [_button("👥 Покупатели", f"ana_buy:{period}")],
        [_button("⭐ Отзывы", f"ana_reviews:{period}")],
        [_button("📅 По времени", f"ana_time:{period}")],
        [_button("🏅 Рекорды", "ana_records")],
    ]


def _analytics_keyboard(period: str, *, detail=False):
    if detail:
        return _menu([[_button("🔙 К аналитике", f"ana_over:{period}")]])
    return _menu([
        [_button("📅 Сегодня", "ana_over:today"), _button("🗓 7 дней", "ana_over:7d")],
        [_button("🗓 30 дней", "ana_over:30d"), _button("♾ Всё время", "ana_over:all")],
        *_analytics_sections(period),
        [_button("🔙 Назад", "menu_stats")],
    ])


def _analytics_overview_text(period: str, data: dict) -> str:
    turnover = data["usd_turnover"]
    average = (Decimal(turnover) / data["usd_orders"]
               if turnover is not None and data["usd_orders"] else None)
    lines = [
        f"📈 <b>Аналитика продаж · {_ANALYTICS_PERIODS[period]}</b>", "",
        f"🛒 Заказов: {data['orders']}" + _analytics_change(data["orders"], data["previous_orders"]),
        f"💰 Оборот: {_analytics_money(turnover)}" +
        _analytics_change(turnover, data["previous_usd_turnover"]),
        f"💵 Средний чек: {_analytics_money(average)}",
        f"↩️ Возвратов: {data['refunds']}",
        f"⭐ Отзывов: {data['reviews']}",
        f"👥 Покупателей: {data['buyers']}",
        f"🔁 Повторных: {data['repeat_buyers']}",
    ]
    if data["terminal_orders"]:
        lines.append("Доля возвратов среди завершённых/возвращённых: " +
                     _analytics_percent(data["refunds"], data["terminal_orders"]))
    top = data["top_product"]
    if top:
        lines += ["", "🏆 Самый популярный:",
                  f"{_analytics_product(top['product'])} — {top['orders']} заказов"]
    expensive = data["most_expensive"]
    if expensive:
        lines += ["", "💎 Самый дорогой заказ:",
                  f"{_analytics_product(expensive['product'])} — "
                  f"{_analytics_money(expensive['usd_amount'])}"]
    else:
        lines += ["", "💎 Самый дорогой заказ: нет данных"]
    top_money = data["top_turnover_product"]
    if top_money:
        lines += ["", "💰 Больше всего подтверждённого оборота:",
                  f"{_analytics_product(top_money['product'])} — "
                  f"{_analytics_money(top_money['usd_turnover'])}"]
    best = data["best_day"]
    if best:
        money = (" / " + _analytics_money(best["usd_turnover"])
                 if best["usd_turnover"] is not None else "")
        lines += ["", "📅 Лучший день наблюдения закрытий:",
                  f"{best['day']} — {best['orders']} заказов{money}"]
    lines += ["", _ANALYTICS_NOTE]
    return "\n".join(lines)


def _analytics_top_text(period: str, sort: str, rows: list[dict]) -> str:
    mode = "по продажам" if sort == "count" else "по обороту"
    lines = [f"🏆 <b>Топ товаров · {_ANALYTICS_PERIODS[period]} · {mode}</b>", ""]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {_analytics_product(row['product'])}")
        lines.append(f"   🛒 {row['orders']} · 💰 {_analytics_money(row['usd_turnover'])}")
        lines.append("")
    if not rows:
        lines.append("Нет данных для выбранного периода.")
    return "\n".join(lines).rstrip()


def _analytics_buyers_text(period: str, data: dict) -> str:
    lines = [f"👥 <b>Покупатели · {_ANALYTICS_PERIODS[period]}</b>", "",
             f"👤 Уникальных: {data['unique']}", f"🔁 Повторных: {data['repeat']}"]
    if data["top"]:
        lines += ["", "🏆 Топ покупателей:"]
        for index, row in enumerate(data["top"], 1):
            name = _escaped_preview(row["username"], 100) if row["username"] else "Покупатель"
            lines.append(f"{index}. {name} — {row['orders']} заказов")
    return "\n".join(lines)


def _analytics_reviews_text(period: str, data: dict) -> str:
    return (f"⭐ <b>Отзывы · {_ANALYTICS_PERIODS[period]}</b>\n\n"
            f"⭐ Получено отзывов: {data['reviews']}\n"
            "📊 Доля закрытых заказов с отзывом: " +
            _analytics_percent(data["reviewed_orders"], data["closed_orders"]))


def _analytics_time_text(period: str, data: dict) -> str:
    lines = [f"📅 <b>По времени · {_ANALYTICS_PERIODS[period]}</b>", ""]
    best = data["best_day"]
    if best:
        lines += ["🏆 Лучший день наблюдения закрытий:",
                  f"{best['day']} — {best['orders']} заказов" +
                  (" / " + _analytics_money(best["usd_turnover"])
                   if best["usd_turnover"] is not None else "")]
    best_money = data["best_turnover_day"]
    if best_money:
        lines += ["", "💰 Максимум подтверждённого оборота за день:",
                  f"{best_money['day']} — {_analytics_money(best_money['usd_turnover'])}"]
    if not best:
        lines.append("Нет дат наблюдения закрытий.")
    return "\n".join(lines)


def _analytics_records_text(data: dict) -> str:
    lines = ["🏅 <b>Рекорды · вся известная история</b>"]
    expensive = data["most_expensive"]
    if expensive:
        lines += ["", "💎 Самый дорогой заказ:",
                  f"{_analytics_product(expensive['product'])} — "
                  f"{_analytics_money(expensive['usd_amount'])}"]
    for key, label in (("top_product", "🏆 Самый продаваемый товар"),
                       ("top_turnover_product", "💰 Товар с максимальным оборотом")):
        row = data[key]
        if row:
            value = (f"{row['orders']} заказов" if key == "top_product"
                     else _analytics_money(row["usd_turnover"]))
            lines += ["", label + ":", f"{_analytics_product(row['product'])} — {value}"]
    for key, label in (("best_day", "📅 Максимум замеченных закрытий за день"),
                       ("best_turnover_day", "💵 Максимальный оборот за день наблюдения")):
        row = data[key]
        if row:
            value = (f"{row['orders']} заказов" if key == "best_day"
                     else _analytics_money(row["usd_turnover"]))
            lines += ["", label + ":", f"{row['day']} — {value}"]
    top_buyer = data["top_buyer"]
    if top_buyer:
        name = (_escaped_preview(top_buyer["username"], 100)
                if top_buyer["username"] else "Покупатель")
        lines += ["", "👤 Самый частый покупатель:",
                  f"{name} — {top_buyer['orders']} заказов"]
    if len(lines) == 1:
        lines.append("\nНет данных.")
    lines += ["", _ANALYTICS_NOTE]
    return "\n".join(lines)


async def _analytics_callback(callback: CallbackQuery, action: str) -> bool:
    prefix = action.split(":", 1)[0]
    if prefix not in {"analytics", "ana_over", "ana_top", "ana_buy", "ana_reviews",
                      "ana_time", "ana_records"}:
        return False
    answered = False
    try:
        parts = action.split(":")
        store = _orders_store()
        if action == "analytics":
            text = "📈 <b>Аналитика продаж</b>\nВыберите период или раздел.\n\n" + _ANALYTICS_NOTE
            markup = _analytics_keyboard("30d")
        elif action == "ana_records":
            data = await asyncio.to_thread(store.get_sales_records)
            text, markup = _analytics_records_text(data), _analytics_keyboard("30d", detail=True)
        elif len(parts) in (2, 3) and parts[1] in _ANALYTICS_PERIODS:
            period = parts[1]
            if prefix == "ana_over" and len(parts) == 2:
                data = await asyncio.to_thread(store.get_sales_overview, period)
                text, markup = _analytics_overview_text(period, data), _analytics_keyboard(period)
            elif prefix == "ana_top" and len(parts) == 3 and parts[2] in ("count", "turnover"):
                sort = parts[2]
                data = await asyncio.to_thread(store.get_sales_top_products, period, sort)
                text = _analytics_top_text(period, sort, data)
                markup = _menu([
                    [_button("🛒 По продажам", f"ana_top:{period}:count"),
                     _button("💰 По обороту", f"ana_top:{period}:turnover")],
                    [_button("🔙 Назад", f"ana_over:{period}")],
                ])
            elif prefix == "ana_buy" and len(parts) == 2:
                data = await asyncio.to_thread(store.get_sales_buyers, period)
                text, markup = _analytics_buyers_text(period, data), _analytics_keyboard(period, detail=True)
            elif prefix == "ana_reviews" and len(parts) == 2:
                data = await asyncio.to_thread(store.get_sales_reviews, period)
                text, markup = _analytics_reviews_text(period, data), _analytics_keyboard(period, detail=True)
            elif prefix == "ana_time" and len(parts) == 2:
                data = await asyncio.to_thread(store.get_sales_by_time, period)
                text, markup = _analytics_time_text(period, data), _analytics_keyboard(period, detail=True)
            else:
                raise ValueError("Invalid analytics action.")
        else:
            raise ValueError("Invalid analytics action.")
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Sales analytics unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Аналитика недоступна.", show_alert=True)
    return True


def _system_screen() -> tuple[str, InlineKeyboardMarkup]:
    safe = "✅" if is_safe_mode_enabled() else "⛔"
    return "⚙️ <b>Система</b>\nУправление и диагностика:", _menu([
        [_button("⚠️ Проблемы", "sys_problems")],
        [_button("📜 Журнал действий", "sys_audit:0")],
        [_button(f"🛡 SAFE_MODE: {safe}", "sys_safe_toggle")],
        [_button("🔙 Назад", "menu_main")],
    ])


def _problems_screen() -> tuple[str, InlineKeyboardMarkup]:
    problems = get_active_problems()
    lines = [f"⚠️ <b>Активные проблемы: {len(problems)}</b>"]
    if not problems:
        lines += ["", "✅ Активных проблем нет."]
    for problem in problems:
        icon = "🔴" if problem["severity"] == "ERROR" else "🟡"
        first = datetime.fromtimestamp(problem["first_seen"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        last = datetime.fromtimestamp(problem["last_seen"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines += ["", f"{icon} <b>{problem['code']}</b> · {problem['severity']}",
                  problem["description"], f"Первое: {first}", f"Последнее: {last}"]
    return "\n".join(lines), _menu([[_button("🔄 Обновить", "sys_problems")],
                                     [_button("🔙 Назад", "system")]])


def _audit_screen(page: int, count: int, events: list[dict]):
    pages = max(1, (count + 19) // 20)
    rows = []
    for event in events:
        stamp = datetime.fromtimestamp(event["ts"], timezone.utc).strftime("%d.%m %H:%M")
        rows.append([_button(f"{stamp} · {event['action']} · {event['result']}",
                             f"sys_audit_view:{event['id']}:{page}")])
    if not rows:
        rows.append([_button("Записей пока нет", "system")])
    navigation = []
    if page > 0:
        navigation.append(_button("◀️", f"sys_audit:{page - 1}"))
    if page + 1 < pages:
        navigation.append(_button("▶️", f"sys_audit:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([_button("🔙 Назад", "system")])
    return f"📜 <b>Журнал действий</b> · {page + 1}/{pages}", _menu(rows)


def _audit_detail_screen(event: dict, page: int):
    stamp = datetime.fromtimestamp(event["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (f"📜 <b>Действие #{event['id']}</b>\n\n"
            f"Время: {stamp}\n"
            f"Кто: {escape(event['actor'])}\n"
            f"Действие: {escape(event['action'])}\n"
            f"Цель: {escape(event['target'])}\n"
            f"Результат: {escape(event['result'])}")
    return text, _menu([[_button("🔙 К журналу", f"sys_audit:{page}")]])


async def _notify_safe_mode_activated(callback: CallbackQuery, actor_id: int) -> None:
    try:
        bot = getattr(callback, "bot", None)
        recipients = get_all_recipients() if bot is not None else []
    except Exception:
        return
    for recipient in recipients:
        if recipient == actor_id:
            continue
        try:
            settings = get_user_settings(recipient)
            if not settings.get("notifications_enabled", True) or not settings.get("notify_system", True):
                continue
            await asyncio.wait_for(bot.send_message(
                recipient, "🛡 SAFE_MODE включён. Автоматические действия приостановлены."), timeout=5)
        except Exception as e:
            logger.warning(f"System notification unavailable: {type(e).__name__}.")


async def _operations_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    prefix = action.split(":", 1)[0]
    if prefix not in {"system", "sys_problems", "sys_audit", "sys_audit_view",
                      "sys_safe_toggle", "sys_safe_off", "sys_safe_cancel"}:
        return False
    answered = False
    try:
        parts = action.split(":")
        if action == "system" or action == "sys_safe_cancel":
            text, markup = _system_screen()
        elif action == "sys_problems":
            text, markup = _problems_screen()
        elif prefix == "sys_audit" and len(parts) == 2:
            page = int(parts[1])
            count, events = await asyncio.to_thread(_orders_store().list_audit_events, page)
            _clear_problem("AUDIT_UNAVAILABLE")
            text, markup = _audit_screen(page, count, events)
        elif prefix == "sys_audit_view" and len(parts) == 3:
            event_id, page = int(parts[1]), int(parts[2])
            if page < 0 or page > 100000:
                raise ValueError("Invalid audit page.")
            event = await asyncio.to_thread(_orders_store().get_audit_event, event_id)
            _clear_problem("AUDIT_UNAVAILABLE")
            if event is None:
                raise ValueError("Audit event unavailable.")
            text, markup = _audit_detail_screen(event, page)
        elif action == "sys_safe_toggle":
            if is_safe_mode_enabled():
                text = "⚠️ Разрешить автоматические действия?"
                markup = _menu([[_button("✅ Да", "sys_safe_off")],
                                [_button("❌ Отмена", "sys_safe_cancel")]])
            else:
                toggle_safe_mode_saved(actor=f"telegram:{user_id}")
                text, markup = _system_screen()
                await callback.answer("🛡 SAFE_MODE включён. Автоматические действия приостановлены.")
                answered = True
        elif action == "sys_safe_off":
            if not is_safe_mode_enabled():
                raise ValueError("SAFE_MODE already off.")
            toggle_safe_mode_saved(actor=f"telegram:{user_id}")
            text, markup = _system_screen()
            await callback.answer("🛡 SAFE_MODE выключен.")
            answered = True
        else:
            raise ValueError("Invalid system action.")
        if not answered:
            await callback.answer()
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        if action == "sys_safe_toggle" and answered and is_safe_mode_enabled():
            await _notify_safe_mode_activated(callback, user_id)
    except Exception as e:
        if prefix in {"sys_audit", "sys_audit_view"}:
            _set_problem("AUDIT_UNAVAILABLE")
        logger.warning(f"System UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Система недоступна.", show_alert=True)
    return True


async def _night_callback(callback: CallbackQuery, action: str, user_id: int) -> bool:
    if action not in {"menu_night_mode", "night_edit", "night_reset", "cancel:night",
                      "cancel:review"}:
        return False
    _interaction_state.pop(user_id, None)
    answered = False
    try:
        if action == "night_edit":
            _interaction_state[user_id] = {"action": "night_edit"}
            text, markup = "✏️ Отправьте новый автоответ (до 1000 символов).", _cancel_keyboard("night")
        elif action == "night_reset":
            _save_global_setting("night_mode_reply", None, actor=f"telegram:{user_id}")
            text, markup = _night_mode_screen()
        elif action == "cancel:review":
            text, markup = _review_request_screen()
        else:
            text, markup = _night_mode_screen()
        await callback.answer()
        answered = True
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Night Mode UI unavailable: {type(e).__name__}.")
        if not answered:
            await callback.answer("Настройка недоступна.", show_alert=True)
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
    _interaction_state.pop(user_id, None)
    if await _night_callback(callback, action, user_id):
        return
    if await _review_callback(callback, action, user_id):
        return
    if await _orders_callback(callback, action, user_id):
        return
    if await _analytics_callback(callback, action):
        return
    if await _operations_callback(callback, action, user_id):
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
        _audit_action(f"telegram:{user_id}", "AUTOBUMP", "global",
                      "ON" if bot_settings["auto_bump"] else "OFF")
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
            enabled = toggle_night_mode_saved(actor=f"telegram:{user_id}")
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
            previous = u[key]
            u[key] = not previous
            try:
                save_settings(required=True)
            except RuntimeError:
                u[key] = previous
                await callback.answer("Не удалось сохранить уведомления.", show_alert=True)
                return
            _audit_action(f"telegram:{user_id}", "SETTINGS", "settings", "UPDATED")
            await callback.answer("Настройка обновлена!")
            try:
                await callback.message.edit_reply_markup(reply_markup=get_notifications_keyboard(user_id))
            except Exception:
                pass

    else:
        await callback.answer("Действие больше недоступно.")


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
            if await _review_pending(message, pending):
                return
            try:
                value = _clean_text(message.text, 1000)
                if pending.get("action") != "night_edit":
                    _interaction_state.pop(user_id, None)
                    await message.answer("Действие устарело.")
                    return
                _save_global_setting("night_mode_reply", value, actor=f"telegram:{user_id}")
            except Exception as e:
                logger.warning(f"Night Mode setting unavailable: {type(e).__name__}.")
                await message.answer("Не удалось сохранить. Настройка не изменилась.")
                return
            _interaction_state.pop(user_id, None)
            text, markup = _night_mode_screen()
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
        previous_user_ids = list(bot_settings["authorized_user_ids"])
        had_user_settings = user_id in _user_settings
        was_authorized = user_id in authorized_users
        authorized_users.add(user_id)
        # Сохраняем список авторизованных между перезапусками
        bot_settings["authorized_user_ids"] = list(authorized_users)
        # Инициализируем персональные настройки нового пользователя дефолтными значениями,
        # если у него ещё нет своей записи (например, первый вход)
        if user_id not in _user_settings:
            _user_settings[user_id] = dict(_DEFAULT_USER_SETTINGS)
        try:
            save_settings(required=True)
        except RuntimeError:
            if not was_authorized:
                authorized_users.discard(user_id)
            bot_settings["authorized_user_ids"] = previous_user_ids
            if not had_user_settings:
                _user_settings.pop(user_id, None)
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("Не удалось сохранить авторизацию. Попробуйте позже.")
            return
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
