import asyncio
import hashlib
import json
import os
import queue
import re
import sys
import atexit
from html import escape
from dotenv import load_dotenv
from aiogram import Bot

load_dotenv()

import FunPayAPI
from telegram import (dp, bot_settings, get_user_settings, get_all_recipients,
                      set_runtime_status_context, clear_runtime_status_context,
                      disable_autobump, get_reply_keyboard, is_night_mode_enabled)
from funpay import (FunPayClient, _AmbiguousRaiseOutcome,
                    NIGHT_MODE_MESSAGE_TEXT, NIGHT_MODE_ORDER_TEXT)
from state import ReviewReceiptStore, StateError
import logger


def _is_ignorable_send_error(e: Exception) -> bool:
    """
    True для ошибок отправки в Telegram, которые не о чем нам сообщить:
    пользователь заблокировал бота / удалил чат / деактивировал аккаунт.
    Раньше эта проверка была скопирована в трех местах по-разному (где-то
    была, где-то нет) - отсюда мусорные "Не удалось отправить отчет
    пользователю ...: bot was blocked by the user" в логах на каждый цикл.
    """
    text = str(e).lower()
    return any(marker in text for marker in ("bot was blocked", "chat not found", "deactivated", "user is deactivated"))


def _strip_html(text: str) -> str:
    """
    Убирает HTML-теги из текста Telegram-уведомления для чистого вывода в
    консоль - в CMD разметка <b>/<a> не нужна и только мешает читать.
    """
    clean = re.sub(r"<[^>]+>", "", text)
    clean = clean.replace("\n\n", " | ").replace("\n", " | ").strip()
    return clean


# ---------------------------------------------------------------------------
# Валидация обязательных переменных окружения
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """
    Возвращает значение переменной окружения или завершает процесс с понятной
    ошибкой — вместо того чтобы бот падал где-то внутри с cryptic TypeError/
    AttributeError из-за None.
    """
    value = os.getenv(name)
    if not value:
        logger.error(f"Обязательная переменная окружения {name!r} не задана.")
        logger.error(f"Заполни .env (см. .env.example) и перезапусти бота.")
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Защита от двойного запуска
# ---------------------------------------------------------------------------
# Если запустить бота одновременно в двух местах (например, в консоли VS Code
# и через start.bat) - Telegram начнет отвечать 409 Conflict на long polling,
# а FunPay может "путать" сессии с одним и тем же golden_key (см. комментарий
# в funpay.py про golden_seal/сессии). Поэтому перед стартом проверяем,
# не запущен ли уже другой процесс этого же бота, и если да - сразу выходим
# с понятным сообщением, вместо непонятных 409/ошибок сессии.
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
_lock_handle = None


def acquire_lock():
    """Атомарная блокировка ОС; PID в файле — только справочная информация."""
    global _lock_handle
    if _lock_handle is not None:
        raise RuntimeError("Process lock already acquired.")
    handle = open(LOCK_FILE, "a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
    except OSError:
        handle.close()
        logger.error("Не удалось получить bot.lock: другой экземпляр или ошибка файловой системы.")
        raise SystemExit(1) from None
    _lock_handle = handle
    atexit.register(release_lock)


def release_lock():
    global _lock_handle
    if _lock_handle is not None:
        # Close освобождает блокировку и при аварийном завершении процесса.
        # Файл не удаляем: unlink позволил бы заблокировать другой inode на Linux.
        try:
            _lock_handle.close()
        except OSError as e:
            logger.error(f"Не удалось закрыть bot.lock: {type(e).__name__}.")
        finally:
            _lock_handle = None


async def auto_bump_loop(bot: Bot, client: FunPayClient):
    # ADMIN_ID нужен для валидации конфига при старте
    _require_env("ADMIN_ID")
    user_id = int(_require_env("FUNPAY_USER_ID"))

    # Переменная для отслеживания предыдущего состояния тумблера (чтобы слать уведомления только при переключении)
    last_state = bot_settings.get("auto_bump", False)

    while True:
        current_state = bot_settings.get("auto_bump", False)

        # 1. Проверяем, изменилось ли состояние (Включили или Выключили)
        if current_state != last_state:
            # Рассылаем всем авторизованным пользователям, у которых включён notify_bump
            for recipient_id in get_all_recipients():
                u = get_user_settings(recipient_id)
                if u.get("notify_bump", True):
                    try:
                        if current_state:
                            await bot.send_message(recipient_id, "🚀 Автоподнятие лотов успешно запущено! Ожидайте первый отчет...")
                        else:
                            await bot.send_message(recipient_id, "🛑 Автоподнятие лотов полностью выключено.")
                    except Exception as e:
                        if not _is_ignorable_send_error(e):
                            logger.bump(f"Не удалось отправить уведомление: {type(e).__name__}.")
            last_state = current_state

        # 2. Если тумблер включен - запускаем работу
        if current_state:
            logger.bump("Запуск цикла сканирования и поднятия лотов...")

            # Передаем статус тумблера для мгновенной остановки внутри funpay.py
            try:
                success, message_text, wait_time = await client.bump_lots(
                    user_id,
                    is_cancelled=lambda: not bot_settings["auto_bump"],
                )
            except _AmbiguousRaiseOutcome:
                persistence_failed = False
                try:
                    disable_autobump()
                except RuntimeError:
                    persistence_failed = True
                if asyncio.current_task().cancelling():
                    if persistence_failed:
                        raise RuntimeError("Не удалось надёжно отключить автоподнятие.") from None
                    raise asyncio.CancelledError
                notice = (
                    "⚠️ Автоподнятие остановлено: не удалось сохранить безопасное "
                    "состояние. Проверьте FunPay и настройку до перезапуска."
                    if persistence_failed else
                    "⚠️ Автоподнятие отключено. Результат последнего запроса "
                    "поднятия неизвестен. Проверьте FunPay и включите "
                    "автоподнятие вручную."
                )
                for recipient_id in get_all_recipients():
                    try:
                        await bot.send_message(recipient_id, notice)
                    except Exception as e:
                        logger.error(f"Не удалось отправить предупреждение об автоподнятии: {type(e).__name__}.")
                if persistence_failed:
                    raise RuntimeError("Не удалось надёжно отключить автоподнятие.") from None
                last_state = False
                continue

            if wait_time == 0:  # Прервано пользователем во время перебора
                continue

            logger.bump(f"Цикл завершён. Следующая проверка через {wait_time} сек.")

            # Отправляем отчет всем, у кого включены notifications_enabled + notify_bump.
            # message_text пустой, если все лоты были на кулдауне — в этом случае ничего не шлём.
            if bot_settings["auto_bump"] and message_text:
                logger.bump(_strip_html(message_text))
                for recipient_id in get_all_recipients():
                    u = get_user_settings(recipient_id)
                    if u.get("notifications_enabled", True) and u.get("notify_bump", True):
                        try:
                            await bot.send_message(recipient_id, message_text, parse_mode="HTML")
                        except Exception as e:
                            if not _is_ignorable_send_error(e):
                                logger.bump(f"Не удалось отправить отчет: {type(e).__name__}.")

            # Сон с ежесекундной проверкой кнопки выключения
            for _ in range(int(wait_time)):
                if not bot_settings["auto_bump"]:
                    logger.bump("Автоподнятие выключено пользователем во время ожидания.")
                    break  # Выходим из цикла сна. Основной цикл (while True) заметит изменения и отправит сообщение об остановке
                await asyncio.sleep(1)
        else:
            # Спим в режиме ожидания, если тумблер выключен
            await asyncio.sleep(1)


async def _review_state_operation(client: FunPayClient, operation, *args):
    try:
        return await asyncio.to_thread(operation, *args)
    except StateError:
        client._review_state_failed = True
        raise
    except Exception:
        client._review_state_failed = True
        raise StateError("Persistent state operation failed.") from None


def _review_fingerprint(stars, text: str) -> str:
    """Fingerprint только оценки и текста покупателя, без reply продавца."""
    payload = json.dumps([stars if type(stars) is int else None, text],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_review_event_part(value: str, limit: int) -> str:
    clean = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    clean = re.sub(
        r"(?i)\b(?:golden_key|bot_token|password|cookie|phpsessid|csrf_token)\s*[:=]\s*\S+",
        "[скрыто]", clean,
    )
    return clean[:limit] + ("…" if len(clean) > limit else "")


async def _fetch_and_send_review(
    bot: Bot,
    client: FunPayClient,
    order_id: str,
    delay: float = 0,
) -> None:
    """Проверяет входящий отзыв по Order; событие служит только триггером."""
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Z0-9]{8}", order_id):
        logger.warning("Отзыв пропущен: некорректный ID заказа.")
        return
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        review = None
        full_order = None
        # Сохраняем первые 3 быстрые проверки старого бота. После закрытия
        # заказа отзыв может появиться значительно позже; дополнительные
        # проверки ограничены примерно 130 секундами.
        retry_pauses = (0, 1, 1, 8, 15, 30, 30, 30, 10) if delay > 0 else (0, 1, 1)
        for pause in retry_pauses:
            if pause:
                await asyncio.sleep(pause)
            try:
                full_order, account_id = await asyncio.to_thread(client.get_order_snapshot, order_id)
            except StateError:
                raise
            except Exception as e:
                logger.warning(f"Не удалось прочитать заказ при проверке отзыва: {type(e).__name__}.")
                continue
            review = getattr(full_order, "review", None)
            if review is not None and isinstance(getattr(review, "text", None), str) and review.text.strip():
                break

        if review is None or not isinstance(getattr(review, "text", None), str) or not review.text.strip():
            logger.warning("Отзыв покупателя не найден после ограниченных проверок.")
            return

        verified_order_id = getattr(full_order, "id", None)
        if verified_order_id != order_id or getattr(review, "order_id", None) != verified_order_id:
            logger.warning("Отзыв пропущен: несоответствие ID заказа и отзыва.")
            return

        seller_id = getattr(full_order, "seller_id", None)
        buyer_id = getattr(full_order, "buyer_id", None)
        author_id = getattr(review, "author_id", None)
        if not all(type(value) is int and value > 0 for value in (account_id, seller_id, buyer_id, author_id)):
            logger.warning("Отзыв пропущен: отсутствуют достоверные ID участников.")
            return
        if seller_id != account_id or buyer_id == account_id or author_id != buyer_id:
            logger.warning("Отзыв пропущен: направление не соответствует нашей продаже.")
            return

        text_content = getattr(review, "text", None)
        if not isinstance(text_content, str) or not text_content.strip():
            # reply продавца сам по себе не является входящим отзывом.
            logger.warning("Отзыв пропущен: нет текста отзыва покупателя.")
            return

        buyer = getattr(full_order, "buyer_username", None) or "Покупатель"
        stars = getattr(review, "stars", None)
        stars_str = "⭐" * stars if type(stars) is int and 1 <= stars <= 5 else ""
        fingerprint = _review_fingerprint(stars, text_content)

        # Два пути обнаружения могут дойти сюда одновременно.
        async with client._review_notification_lock:
            if getattr(client, "_review_state_failed", False):
                raise StateError("Persistent state unavailable.")
            await _review_state_operation(
                client, client.review_state.record_review_observation, verified_order_id
            )
            if client._notified_reviews.get(verified_order_id) == fingerprint:
                return
            exists, previous = await _review_state_operation(
                client, client.review_state.get_review_receipt, verified_order_id
            )
            if exists and previous is None:
                # Legacy receipt: сначала фиксируем baseline, без ложного повтора.
                await _review_state_operation(
                    client, client.review_state.baseline_review_fingerprint,
                    verified_order_id, fingerprint,
                )
                client._notified_reviews[verified_order_id] = fingerprint
                return
            if previous == fingerprint:
                client._notified_reviews[verified_order_id] = fingerprint
                return
            title = "✏️ Отзыв изменён" if exists else "🌟 Новый отзыв"
            review_text = (
                f"<b>{title}</b>\n"
                f"Покупатель: <b>{escape(str(buyer))}</b>\n"
                + (f"Оценка: {stars_str}\n" if stars_str else "")
                + f"Текст: {escape(text_content)}"
            )
            delivered = False
            for recipient_id in get_all_recipients():
                u = get_user_settings(recipient_id)
                if not u.get("notifications_enabled", True) or not u.get("notify_review", True):
                    continue
                try:
                    await bot.send_message(recipient_id, review_text, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"Не удалось отправить отзыв: {type(e).__name__}.")
                else:
                    if not delivered:
                        # Receipt пишется только после первой успешной доставки.
                        # При частичной доставке повтор всей рассылки не выполняется.
                        await _review_state_operation(
                            client, client.review_state.record_review_receipt,
                            verified_order_id, fingerprint,
                        )
                        client._notified_reviews[verified_order_id] = fingerprint
                        delivered = True

            if delivered:
                safe_buyer = _safe_review_event_part(str(buyer), 64)
                safe_text = _safe_review_event_part(text_content, 180)
                safe_stars = str(stars) if type(stars) is int and 1 <= stars <= 5 else "—"
                logger.notify(
                    f"{title} | Покупатель: {safe_buyer} | № заказа: {verified_order_id} "
                    f"| Оценка: {safe_stars} | Текст: {safe_text}"
                )

    except StateError:
        raise
    except Exception as e:
        logger.warning(f"Не удалось проверить отзыв: {type(e).__name__}.")


async def _send_night_mode_reply(
    client: FunPayClient, value: str | tuple[str, int | None], kind: str,
) -> None:
    """Старый автоответ, с повторной проверкой тумблера под Account RLock."""
    if not is_night_mode_enabled():
        return

    def send_if_enabled() -> bool:
        with client._account_lock:
            if not is_night_mode_enabled():
                return False
            if kind == "message":
                chat_id = int(value) if value.isdecimal() else None
            else:
                buyer, fallback_chat_id = value
                chat = None
                if isinstance(buyer, str) and buyer != "—":
                    try:
                        chat = client.account.get_chat_by_name(buyer)
                    except Exception:
                        pass
                chat_id = getattr(chat, "id", None)
                if chat_id is None:
                    chat_id = fallback_chat_id
            if type(chat_id) is not int or chat_id <= 0:
                return False
            if not is_night_mode_enabled():
                return False
            client.account.send_message(
                chat_id,
                NIGHT_MODE_MESSAGE_TEXT if kind == "message" else NIGHT_MODE_ORDER_TEXT,
                update_last_saved_message=True,
            )
            return True

    try:
        if await asyncio.to_thread(send_if_enabled):
            logger.notify("Ночной режим: автоответ отправлен.")
    except Exception as e:
        logger.warning(f"Не удалось отправить автоответ: {type(e).__name__}.")


def _order_observation(event) -> tuple[str, str] | None:
    """Извлекает только ID и проверенный статус уже полученного события продажи."""
    if event.type not in (FunPayAPI.enums.EventTypes.NEW_ORDER,
                          FunPayAPI.enums.EventTypes.ORDER_STATUS_CHANGED):
        return None
    order = getattr(event, "order", None)
    order_id = getattr(order, "id", None)
    status = getattr(order, "status", None)
    status_names = (
        (FunPayAPI.types.OrderStatuses.PAID, "PAID"),
        (FunPayAPI.types.OrderStatuses.CLOSED, "CLOSED"),
        (FunPayAPI.types.OrderStatuses.REFUNDED, "REFUNDED"),
    )
    for known_status, name in status_names:
        if status == known_status:
            return order_id, name
    raise StateError("Order event status unavailable.")


async def notifications_loop(bot: Bot, client: FunPayClient):
    """
    Слушает события FunPay (новые сообщения, заказы, закрытие заказа, отзывы)
    и рассылает уведомления в Telegram каждому авторизованному пользователю
    согласно его ПЕРСОНАЛЬНЫМ тумблерам notify_message / notify_order /
    notify_review / notifications_enabled.

    Runner.listen() - блокирующий генератор, поэтому он крутится в отдельном
    потоке (runner_thread), а сюда, в асинхронный код, события попадают через
    потокобезопасную очередь client.event_queue.

    ВАЖНО: используется тот же самый client (тот же Account/сессия), что и
    auto_bump_loop - НЕ создаем отдельный FunPayClient. Если завести вторую
    независимую сессию с тем же golden_key, FunPay начинает "путать" сессии
    друг с другом, и запросы (в т.ч. внутри Runner'а) начинают массово падать
    с ошибками - именно это давало частые "Произошла ошибка при получении
    событий" в логах.
    """
    # Проверка закрытого заказа может ждать до ~130 секунд.
    # Один полный burst очереди допустим, но backlog после dequeue не растёт бесконечно.
    max_review_tasks = 256
    review_tasks: set[asyncio.Task] = set()
    review_failed = False

    def review_done(task: asyncio.Task) -> None:
        nonlocal review_failed
        review_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(f"Review task failed: {type(error).__name__}.")
            review_failed = True

    def start_review_check(order_id: str, delay: float) -> None:
        if len(review_tasks) >= max_review_tasks:
            raise RuntimeError("Review task capacity exceeded.")
        task = asyncio.create_task(_fetch_and_send_review(bot, client, order_id, delay=delay))
        review_tasks.add(task)
        task.add_done_callback(review_done)

    client.start_runner()
    try:
        while True:
            if client.runner_stop_requested():
                return
            if client.runner_failed():
                raise RuntimeError("Runner producer неожиданно завершился.") from None
            if review_failed:
                raise RuntimeError("Review task unexpectedly failed.") from None
            # Забираем следующее событие из очереди, не блокируя asyncio-цикл
            try:
                event = await asyncio.to_thread(client.event_queue.get, True, 1.0)
            except queue.Empty:
                continue
            if client.runner_stop_requested():
                return
            if client.runner_failed():
                raise RuntimeError("Runner producer неожиданно завершился.") from None
            if review_failed:
                raise RuntimeError("Review task unexpectedly failed.") from None

            observation = _order_observation(event)
            if observation is not None:
                try:
                    order = getattr(event, "order", None)
                    closed = observation[1] == "CLOSED"
                    event_currency = getattr(order, "currency", None) if closed else None
                    await asyncio.to_thread(
                        client.review_state.record_order_observation, *observation,
                        amount=(getattr(order, "price", None) or getattr(order, "sum", None))
                        if closed else None,
                        currency=(event_currency if event_currency is not None
                                  else bot_settings.get("stats_currency")) if closed else None,
                    )
                except StateError:
                    logger.error("Order statistics write failed: StateError.")
                    raise
                except Exception as e:
                    logger.error(f"Order statistics write failed: {type(e).__name__}.")
                    raise StateError("Persistent order write failed.") from None

            for setting_key, text in client.describe_event(event):
                if setting_key == "_night_mode_reply_message":
                    await _send_night_mode_reply(client, text, "message")
                    continue
                if setting_key == "_night_mode_reply_order":
                    await _send_night_mode_reply(client, text, "order")
                    continue
                # _review_check_immediate — отзыв поймали через NEW_FEEDBACK в чате, сразу проверяем
                if setting_key == "_review_check_immediate":
                    start_review_check(text, delay=0)
                    continue
                # _review_check — после закрытия проверяем заказ до ~130 секунд.
                if setting_key == "_review_check":
                    start_review_check(text, delay=5)
                    continue

                # Раньше здесь не было НИКАКОГО вывода в консоль - Telegram получал
                # уведомление, а в CMD было тихо. Теперь то же самое, что летит в
                # Telegram, сразу видно и в консоли (без HTML-тегов, одной строкой).
                logger.notify(_strip_html(text))

                # Рассылаем каждому авторизованному пользователю согласно его
                # персональным настройкам уведомлений
                for recipient_id in get_all_recipients():
                    u = get_user_settings(recipient_id)
                    # Проверяем главный рубильник пользователя
                    if not u.get("notifications_enabled", True):
                        continue
                    # Проверяем конкретный тип уведомления
                    if not u.get(setting_key, True):
                        continue
                    try:
                        await bot.send_message(recipient_id, text, parse_mode="HTML")
                    except Exception as e:
                        if not _is_ignorable_send_error(e):
                            logger.notify(f"Не удалось отправить уведомление: {type(e).__name__}.")
    finally:
        for task in tuple(review_tasks):
            task.cancel()
        if review_tasks:
            _, pending = await asyncio.wait(tuple(review_tasks), timeout=10.0)
            if pending:
                logger.error("Review task cleanup failed.")
                primary_type = sys.exc_info()[0]
                if primary_type is None or issubclass(primary_type, asyncio.CancelledError):
                    raise RuntimeError("Review task cleanup failed.") from None
        if review_failed:
            primary_type = sys.exc_info()[0]
            if primary_type is None or issubclass(primary_type, asyncio.CancelledError):
                raise RuntimeError("Review task unexpectedly failed.") from None


async def withdrawals_poll_loop(client: FunPayClient):
    """OLD interval: сразу, затем каждые 1800 с; только read-only balance GET."""
    while True:
        try:
            completed = await asyncio.to_thread(client.get_completed_withdrawals)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Withdrawal poll failed: {type(e).__name__}.")
        else:
            for transaction_id, amount, currency in completed:
                await asyncio.to_thread(
                    client.review_state.record_withdrawal_observation,
                    transaction_id, amount, currency,
                )
        await asyncio.sleep(1800)


async def session_refresh_loop(client: FunPayClient):
    """
    Раз в час обновляет PHPSESSID - 1 в 1 как update_session_loop в Кардинале
    (там тоже сначала sleep(3600), потом account.get(update_phpsessid=True),
    и именно так, а не наоборот).
    """
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(client.refresh_session)  # update_phpsessid=True
            logger.info("Сессия FunPay обновлена (PHPSESSID установлен).")
        except Exception as e:
            logger.warning(f"Не удалось обновить сессию FunPay: {type(e).__name__}.")


async def _send_runtime_notice(bot: Bot, message: str, *, restore_keyboard: bool = False) -> None:
    """Одна попытка отправки каждому авторизованному получателю."""
    try:
        recipients = get_all_recipients()
    except Exception as e:
        logger.warning(f"Не удалось получить получателей системного уведомления: {type(e).__name__}.")
        return
    notice_kwargs = {"reply_markup": get_reply_keyboard()} if restore_keyboard else {}
    for recipient_id in recipients:
        try:
            await asyncio.wait_for(
                bot.send_message(recipient_id, message, **notice_kwargs),
                timeout=5.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Не удалось отправить системное уведомление: {type(e).__name__}.")


async def _supervise_tasks(bot: Bot, client: FunPayClient):
    """Завершает runtime при остановке polling или критической фоновой задачи."""
    tasks = {
        "telegram_polling": asyncio.create_task(
            dp.start_polling(bot, close_bot_session=False), name="telegram_polling"
        ),
        "auto_bump": asyncio.create_task(auto_bump_loop(bot, client), name="auto_bump"),
        "notifications": asyncio.create_task(notifications_loop(bot, client), name="notifications"),
        "withdrawals": asyncio.create_task(withdrawals_poll_loop(client), name="withdrawals"),
        "session_refresh": asyncio.create_task(session_refresh_loop(client), name="session_refresh"),
    }
    polling = tasks["telegram_polling"]
    failed = False
    primary_error = None
    try:
        done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        for name, task in tasks.items():
            if task in done:
                if not task.cancelled() and task.exception() is not None:
                    if primary_error is None:
                        primary_error = task.exception()
                elif task is not polling:
                    logger.error(f"Критическая задача {name} неожиданно завершилась.")
                    failed = True
    finally:
        client.stop_runner()
        try:
            for task in tasks.values():
                if task is not polling and not task.done():
                    task.cancel()
            try:
                if not polling.done():
                    try:
                        # Штатная остановка позволяет aiogram завершить свои polling tasks.
                        await asyncio.wait_for(dp.stop_polling(), timeout=10.0)
                    except Exception as e:
                        logger.error(f"Не удалось штатно остановить polling: {type(e).__name__}.")
                        failed = True
            finally:
                if not polling.done():
                    polling.cancel()
                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                for name, result in zip(tasks, results):
                    if isinstance(result, asyncio.CancelledError):
                        continue
                    if isinstance(result, BaseException):
                        # Текст исключения и traceback могут содержать credentials.
                        logger.error(f"Ошибка задачи {name}: {type(result).__name__}.")
                        if primary_error is None:
                            primary_error = result
                        failed = True
        finally:
            try:
                if not await client.join_runner():
                    logger.error("Runner producer не завершился при shutdown.")
                    failed = True
            except Exception as e:
                logger.error(f"Не удалось дождаться Runner producer: {type(e).__name__}.")
                failed = True

    if primary_error is not None:
        raise primary_error
    if failed:
        raise RuntimeError("Runtime остановлен из-за завершения критической задачи.") from None


def _validate_funpay_user_id(client: FunPayClient) -> None:
    """Разрешает runtime только для ID инициализированного Account."""
    try:
        configured_id = int(_require_env("FUNPAY_USER_ID"))
    except ValueError:
        logger.error("FUNPAY_USER_ID должен быть положительным числовым ID.")
        raise RuntimeError("Невалидный FUNPAY_USER_ID.") from None

    account_id = client.account.id if client.account.is_initiated else None
    if (type(account_id) is not int or account_id <= 0
            or configured_id <= 0 or configured_id != account_id):
        logger.error("FUNPAY_USER_ID не соответствует авторизованному аккаунту FunPay.")
        raise RuntimeError("FUNPAY_USER_ID не соответствует авторизованному аккаунту FunPay.") from None


async def main():

    golden_key = _require_env("FUNPAY_GOLDEN_KEY")
    bot_token = _require_env("BOT_TOKEN")
    try:
        if int(_require_env("ADMIN_ID")) <= 0 or int(_require_env("FUNPAY_USER_ID")) <= 0:
            raise ValueError
    except ValueError:
        raise RuntimeError("ADMIN_ID и FUNPAY_USER_ID должны быть положительными числовыми ID.") from None
    # Каждый новый process lifecycle требует явного ручного включения.
    disable_autobump()
    # ОДИН общий клиент/аккаунт на весь бот - и для поднятия лотов, и для уведомлений.
    client = FunPayClient(golden_key)

    logger.info("Инициализация системы...")

    # Как в Кардинале (__init_account): самый первый запрос к аккаунту -
    # ОБЫЧНЫЙ account.get() БЕЗ update_phpsessid=True. update_phpsessid=True
    # используется только позже, в session_refresh_loop, раз в час - как и в
    # Кардинале (update_session_loop). Если update_phpsessid=True вызвать
    # самым первым запросом (до того как обычная сессия вообще установлена),
    # похоже, это и приводит к "Необходимая cookie отсутствует или устарела".
    try:
        await asyncio.to_thread(client.initialize_account)
    except Exception:
        logger.error("Не удалось инициализировать сессию FunPay.")
        raise RuntimeError("Начальная сессия FunPay не инициализирована.") from None

    _validate_funpay_user_id(client)
    client.review_state = ReviewReceiptStore()
    try:
        await asyncio.to_thread(client.review_state.initialize)
    except Exception as e:
        logger.error(f"Не удалось инициализировать persistent state: {type(e).__name__}.")
        raise RuntimeError("Persistent state недоступен.") from None

    logger.banner(client.account.username)
    logger.success(f"Сессия FunPay инициализирована (аккаунт: {client.account.username}).")
    if os.name == "nt":
        # Обновляем заголовок окна cmd, если бот запущен через .bat на Windows.
        # На сервере (Linux) os.name == "posix", эта строка просто не выполнится.
        os.system("title FunPay Bot")

    bot = Bot(token=bot_token)

    runtime_ready = False
    try:
        logger.success("Бот подключен к Telegram. Запуск фоновых процессов...")
        await _send_runtime_notice(bot, "🟢 Бот запущен.", restore_keyboard=True)
        runtime_ready = True
        set_runtime_status_context(client)
        await _supervise_tasks(bot, client)
    finally:
        clear_runtime_status_context()
        primary_type = sys.exc_info()[0]
        unwinding_exception = primary_type is not None
        try:
            if runtime_ready:
                message = ("⚠️ Бот остановлен из-за критической ошибки."
                           if primary_type is not None and not issubclass(primary_type, asyncio.CancelledError)
                           else "🔴 Бот остановлен.")
                try:
                    await _send_runtime_notice(bot, message)
                except asyncio.CancelledError:
                    if not unwinding_exception:
                        raise
                except Exception as e:
                    logger.error(f"Не удалось отправить системное уведомление: {type(e).__name__}.")
        finally:
            try:
                await bot.session.close()
            except asyncio.CancelledError:
                if not unwinding_exception:
                    raise
            except Exception as e:
                logger.error(f"Не удалось закрыть сессию Telegram: {type(e).__name__}.")


def run():
    """Точка входа для `uv run funpay-bot` (project.scripts)."""
    try:
        acquire_lock()
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Бот завершён с ошибкой: {type(e).__name__}.")
        raise SystemExit(1) from None
    finally:
        release_lock()


if __name__ == "__main__":
    run()
