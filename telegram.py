import asyncio
import hmac
import os
import json
import math
import time
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from funpay import FunPayClient
from state import StateError
import logger

dp = Dispatcher()

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

# Глобальные настройки — общие для всего бота
_DEFAULT_GLOBAL_SETTINGS: dict = {
    "auto_bump": False,
    "authorized_user_ids": [],
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

        print(f"[SETTINGS] Настройки загружены из {SETTINGS_FILE}. "
              f"Глобальные: {bot_settings} | Пользователи с персональными настройками: {list(_user_settings.keys())}")
    except Exception as e:
        print(f"[SETTINGS] Не удалось загрузить {SETTINGS_FILE}, использую значения по умолчанию: {e}")


def save_settings() -> None:
    """Сохраняет текущие настройки (глобальные + персональные) на диск."""
    try:
        data = {
            "auto_bump": bot_settings["auto_bump"],
            "authorized_user_ids": bot_settings["authorized_user_ids"],
            "user_settings": {
                str(uid): sett for uid, sett in _user_settings.items()
            },
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SETTINGS] Не удалось сохранить настройки: {e}")


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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Автоподнятие лотов: {bump_status}", callback_data="toggle_bump")],
        [InlineKeyboardButton(text="🔔 Мои уведомления", callback_data="menu_notifications")],
    ])
    return keyboard


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


MAIN_MENU_TEXT = "🎛 <b>Панель управления FunPay</b>\nВыберите действие с помощью кнопок ниже:"
NOTIFICATIONS_MENU_TEXT = "🔔 <b>Мои настройки уведомлений</b>\nНастройте, о чём вас оповещать (настройки независимые для каждого пользователя):"


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    if is_authorized(user_id):
        await message.answer("Клавиатура управления активирована 👇", reply_markup=get_reply_keyboard())
        await message.answer(
            MAIN_MENU_TEXT,
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer("🔒 <b>Доступ закрыт.</b>\nВведите пароль:", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    try:
        status_text = get_runtime_status_text()
    except Exception as e:
        logger.warning(f"Status unavailable: {type(e).__name__}.")
        status_text = "🩺 Состояние бота\nRuntime: недоступно"
    await message.answer(status_text)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    try:
        store = _runtime_client.review_state if _runtime_client is not None else None
        if store is None:
            raise StateError("Persistent state unavailable.")
        stats = await asyncio.to_thread(store.get_order_statistics)
        lines = ["📊 Статистика заказов", "Первое наблюдение ботом (UTC)",
                 "Статусы — по последнему полученному событию"]
        for title, key in (("Сегодня", "today"), ("7 дней", "7_days"),
                           ("30 дней", "30_days"), ("Всё время", "all_time")):
            period = stats[key]
            lines.append(
                f"\n{title}:\nЗаказов: {period['orders']}\n"
                f"Завершено: {period['closed']}\nВозвратов: {period['refunded']}"
            )
    except Exception as e:
        logger.warning(f"Order statistics unavailable: {type(e).__name__}.")
        await message.answer("📊 Статистика недоступна")
        return
    await message.answer("\n".join(lines))


@dp.callback_query()
async def callback_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_authorized(user_id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return

    action = callback.data

    # Включение/выключение автоподнятия (глобальная настройка — влияет на всех)
    if action == "toggle_bump":
        bot_settings["auto_bump"] = not bot_settings["auto_bump"]
        save_settings()
        state_text = "включено" if bot_settings["auto_bump"] else "выключено"
        await callback.answer(f"Автоподнятие {state_text}!")
        try:
            await callback.message.edit_reply_markup(reply_markup=get_main_keyboard(user_id))
        except Exception:
            pass

    # Переход в персональное подменю уведомлений
    elif action == "menu_notifications":
        try:
            await callback.message.edit_text(
                NOTIFICATIONS_MENU_TEXT,
                reply_markup=get_notifications_keyboard(user_id),
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Возврат в главное меню
    elif action == "menu_main":
        await callback.answer()
        await callback.message.answer(
            "Клавиатура управления активирована 👇",
            reply_markup=get_reply_keyboard(),
        )
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
            await message.answer(
                MAIN_MENU_TEXT,
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
            )
        return

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
