import asyncio
import os
import queue
import re
import subprocess
import sys
import atexit
import threading
import time
from dotenv import load_dotenv
from aiogram import Bot

load_dotenv()

from telegram import dp, bot_settings, get_user_settings, get_all_recipients
from funpay import FunPayClient
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


def _is_process_running(pid: int) -> bool:
    """Проверяет, жив ли процесс с указанным PID (кроссплатформенно)."""
    if os.name == "nt":
        try:
            # FIX: shell=True убран — pid валидируется выше как int,
            # но shell=True + данные из файла = потенциальная shell injection.
            # Аргументы передаём списком, никакого shell-интерпретатора.
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return str(pid) in output
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # процесс есть, просто нет прав - считаем, что жив
        except Exception:
            return False


def acquire_lock():
    """
    Создает bot.lock с PID текущего процесса.
    Если файл уже есть и процесс из него жив - завершает работу.
    Если файл есть, но процесс мертв (бот упал/был убит без очистки) -
    считаем lock "зависшим" и спокойно перезаписываем его.
    """
    if os.path.exists(LOCK_FILE):
        old_pid: int | None = None
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            # Явная валидация: PID должен быть целым положительным числом.
            # Защищает от ситуации, когда кто-то записал в файл произвольный текст.
            if raw.isdigit():
                old_pid = int(raw)
        except Exception:
            pass

        if old_pid and _is_process_running(old_pid):
            logger.error(f"Бот уже запущен (PID {old_pid}). Останови тот процесс, прежде чем запускать новый.")
            sys.exit(1)
        else:
            logger.info("Обнаружен зависший bot.lock от завершенного процесса - перезаписываю.")

    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    atexit.register(release_lock)


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                saved_pid = f.read().strip()
            if saved_pid == str(os.getpid()):
                os.remove(LOCK_FILE)
    except Exception:
        pass


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
                            logger.bump(f"Не удалось отправить уведомление пользователю {recipient_id}: {e}")
            last_state = current_state

        # 2. Если тумблер включен - запускаем работу
        if current_state:
            logger.bump("Запуск цикла сканирования и поднятия лотов...")

            # Передаем статус тумблера для мгновенной остановки внутри funpay.py
            success, message_text, wait_time = await client.bump_lots(
                user_id,
                is_cancelled=lambda: not bot_settings["auto_bump"]
            )

            if wait_time == 0:  # Прервано пользователем во время перебора
                continue

            logger.bump(f"Цикл завершен. Следующий запуск через {wait_time} сек.")

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
                                logger.bump(f"Не удалось отправить отчет пользователю {recipient_id}: {e}")

            # Сон с ежесекундной проверкой кнопки выключения
            for _ in range(int(wait_time)):
                if not bot_settings["auto_bump"]:
                    logger.bump("Автоподнятие выключено пользователем во время ожидания.")
                    break  # Выходим из цикла сна. Основной цикл (while True) заметит изменения и отправит сообщение об остановке
                await asyncio.sleep(1)
        else:
            # Спим в режиме ожидания, если тумблер выключен
            await asyncio.sleep(1)


async def _fetch_and_send_review(
    bot: Bot,
    client: FunPayClient,
    order_id: str,
    delay: float = 0,
) -> None:
    """Проверяет входящий отзыв по Order; событие служит только триггером."""
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Z0-9]{8}", order_id):
        logger.notify("Отзыв пропущен: некорректный ID заказа.")
        return
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        review = None
        full_order = None
        for attempt in range(3):
            full_order = await asyncio.to_thread(client.account.get_order, order_id)
            review = getattr(full_order, "review", None)
            if review is not None:
                break
            await asyncio.sleep(1)

        if review is None:
            logger.notify("Отзыв не найден после 3 попыток.")
            return

        verified_order_id = getattr(full_order, "id", None)
        if verified_order_id != order_id or getattr(review, "order_id", None) != verified_order_id:
            logger.notify("Отзыв пропущен: несоответствие ID заказа и отзыва.")
            return

        account_id = getattr(client.account, "id", None)
        seller_id = getattr(full_order, "seller_id", None)
        buyer_id = getattr(full_order, "buyer_id", None)
        author_id = getattr(review, "author_id", None)
        if not all(type(value) is int and value > 0 for value in (account_id, seller_id, buyer_id, author_id)):
            logger.notify("Отзыв пропущен: отсутствуют достоверные ID участников.")
            return
        if seller_id != account_id or buyer_id == account_id or author_id != buyer_id:
            logger.notify("Отзыв пропущен: направление не соответствует нашей продаже.")
            return

        text_content = getattr(review, "text", None)
        if not isinstance(text_content, str) or not text_content.strip():
            # reply продавца сам по себе не является входящим отзывом.
            logger.notify("Отзыв пропущен: нет текста отзыва покупателя.")
            return

        buyer = getattr(full_order, "buyer_username", None) or "Покупатель"
        stars = getattr(review, "stars", None)
        stars_str = "⭐" * stars if type(stars) is int and 1 <= stars <= 5 else ""
        review_text = (
            "🌟 <b>Новый отзыв</b>\n"
            f"Покупатель: <b>{buyer}</b>\n"
            + (f"Оценка: {stars_str}\n" if stars_str else "")
            + f"Текст: {text_content}"
        )

        # Два пути обнаружения могут дойти сюда одновременно.
        async with client._review_notification_lock:
            if verified_order_id in client._notified_reviews:
                return
            for recipient_id in get_all_recipients():
                u = get_user_settings(recipient_id)
                if not u.get("notifications_enabled", True) or not u.get("notify_review", True):
                    continue
                try:
                    await bot.send_message(recipient_id, review_text, parse_mode="HTML")
                except Exception as e:
                    logger.notify(f"Не удалось отправить отзыв: {type(e).__name__}.")
                else:
                    # Отметка означает доставку хотя бы одному получателю.
                    # При частичной доставке повтор всей рассылки не выполняется.
                    client._notified_reviews.add(verified_order_id)

    except Exception as e:
        logger.notify(f"Не удалось проверить отзыв: {type(e).__name__}.")


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
    def runner_thread():
        while True:
            try:
                client.init_runner()
                # is_cancelled всегда False - слушаем, пока не упадет с ошибкой
                client.listen_events(is_cancelled=lambda: False)
                logger.warning("Runner неожиданно завершился, перезапуск через 30 сек...")
            except Exception as e:
                logger.error(f"Ошибка в потоке Runner'а: {e}")
            time.sleep(30)

    threading.Thread(target=runner_thread, daemon=True).start()

    while True:
        # Забираем следующее событие из очереди, не блокируя asyncio-цикл
        try:
            event = await asyncio.to_thread(client.event_queue.get, True, 1.0)
        except queue.Empty:
            continue

        for setting_key, text in client.describe_event(event):
            # _review_check_immediate — отзыв поймали через NEW_FEEDBACK в чате, сразу проверяем
            if setting_key == "_review_check_immediate":
                asyncio.create_task(
                    _fetch_and_send_review(bot, client, text, delay=0)
                )
                continue
            # _review_check — заказ закрыт, ждём 5 сек (покупатель мог оставить отзыв
            # чуть позже закрытия), затем 3 попытки — точно как у Кардинала
            if setting_key == "_review_check":
                asyncio.create_task(
                    _fetch_and_send_review(bot, client, text, delay=5)
                )
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
                        logger.notify(f"Не удалось отправить уведомление пользователю {recipient_id}: {e}")


async def session_refresh_loop(client: FunPayClient):
    """
    Раз в час обновляет PHPSESSID - 1 в 1 как update_session_loop в Кардинале
    (там тоже сначала sleep(3600), потом account.get(update_phpsessid=True),
    и именно так, а не наоборот).
    """
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(client.account.get, True)  # update_phpsessid=True
            logger.info("Сессия FunPay обновлена (PHPSESSID установлен).")
        except Exception as e:
            logger.warning(f"Не удалось обновить сессию FunPay: {e}")


async def _supervise_tasks(bot: Bot, client: FunPayClient):
    """Завершает runtime при остановке polling или критической фоновой задачи."""
    tasks = {
        "telegram_polling": asyncio.create_task(
            dp.start_polling(bot, close_bot_session=False), name="telegram_polling"
        ),
        "auto_bump": asyncio.create_task(auto_bump_loop(bot, client), name="auto_bump"),
        "notifications": asyncio.create_task(notifications_loop(bot, client), name="notifications"),
        "session_refresh": asyncio.create_task(session_refresh_loop(client), name="session_refresh"),
    }
    polling = tasks["telegram_polling"]
    failed = False
    try:
        done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        for name, task in tasks.items():
            if task in done and task is not polling:
                if task.cancelled() or task.exception() is None:
                    logger.error(f"Критическая задача {name} неожиданно завершилась.")
                    failed = True
    finally:
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
                    failed = True

    if failed:
        raise RuntimeError("Runtime остановлен из-за завершения критической задачи.") from None


async def main():

    bot = Bot(token=_require_env("BOT_TOKEN"))

    golden_key = _require_env("FUNPAY_GOLDEN_KEY")
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
        await asyncio.to_thread(client.account.get)
        logger.banner(client.account.username)
        logger.success(f"Сессия FunPay инициализирована (аккаунт: {client.account.username}).")
        if os.name == "nt":
            # Обновляем заголовок окна cmd, если бот запущен через .bat на Windows.
            # На сервере (Linux) os.name == "posix", эта строка просто не выполнится.
            os.system(f"title FunPay Bot - {client.account.username}")
    except Exception as e:
        logger.error(f"Не удалось получить начальную сессию FunPay: {e}")

    logger.success("Бот подключен к Telegram. Запуск фоновых процессов...")

    try:
        await _supervise_tasks(bot, client)
    finally:
        unwinding_exception = sys.exc_info()[0] is not None
        try:
            await bot.session.close()
        except asyncio.CancelledError:
            if not unwinding_exception:
                raise
        except Exception as e:
            logger.error(f"Не удалось закрыть сессию Telegram: {type(e).__name__}.")


def run():
    """Точка входа для `uv run funpay-bot` (project.scripts)."""
    acquire_lock()
    asyncio.run(main())


if __name__ == "__main__":
    run()
