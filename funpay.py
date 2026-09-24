import asyncio
import os
import time
import random
import re
import queue
import threading
from decimal import Decimal, InvalidOperation
from html import escape
import requests
from bs4 import BeautifulSoup
import FunPayAPI
from FunPayAPI.common.enums import SubCategoryTypes
import logger


class _AutobumpActionCancelled(Exception):
    """Отмена поднятия до отправки запроса."""


class _AmbiguousRaiseOutcome(Exception):
    """Результат modifying lots/raise неизвестен; автоподнятие приостановлено."""


class _RunnerStopRequested(BaseException):
    """Остановка producer до отправки следующего runner/ запроса."""


class _EventQueueOverflow(Exception):
    """Распарсенное событие не удалось передать consumer без потери."""


class _WithdrawalParseError(ValueError):
    """Строка завершённого вывода не содержит проверяемых данных."""


# Буфер для короткого всплеска Runner; переполнение завершает runtime, а не растит память.
EVENT_QUEUE_CAPACITY = 256
NIGHT_MODE_MESSAGE_TEXT = "😴 Продавец спит, как только проснется сразу ответит Вам"
NIGHT_MODE_ORDER_TEXT = "😴 Продавец спит, как проснётся — сразу приступит"

# ---------------------------------------------------------------------------
# ФИКС ДЛЯ 400 "Необходимая cookie отсутствует или устарела" на runner/
# ---------------------------------------------------------------------------
# Установленная версия FunPayAPI (1.1.0 с PyPI) собирает Cookie-заголовок
# только из golden_key + PHPSESSID (см. self.account.method внутри библиотеки).
# В более новых форках библиотеки (используемых, например, в актуальных сборках
# FunPayCardinal) добавлен отдельный self.cookies - словарь ДОПОЛНИТЕЛЬНЫХ кук,
# которые FunPay стал присылать в Set-Cookie и требовать обратно при каждом
# запросе (в частности к runner/). Установленная версия 1.1.0 этот механизм не
# реализует - отсюда "cookie отсутствует".
#
# Чтобы не переустанавливать/патчить саму библиотеку, патчим requests.Session.request
# на уровне процесса: любой ответ с funpay.com запоминаем все "лишние" куки
# (кроме PHPSESSID, которым и так управляет сама библиотека), и добавляем их
# в заголовок Cookie каждого следующего запроса к funpay.com.
_funpay_extra_cookies: dict[str, str] = {}
_funpay_cookie_patch_applied = False


def _apply_funpay_cookie_patch():
    global _funpay_cookie_patch_applied
    if _funpay_cookie_patch_applied:
        return

    original_request = requests.sessions.Session.request

    def patched_request(self, method, url, *args, **kwargs):
        is_funpay = isinstance(url, str) and "funpay.com" in url

        if is_funpay and _funpay_extra_cookies:
            headers = dict(kwargs.get("headers") or {})
            cookie_key = next((k for k in headers if k.lower() == "cookie"), None)
            extra = "; ".join(f"{k}={v}" for k, v in _funpay_extra_cookies.items())
            if cookie_key:
                if extra not in headers[cookie_key]:
                    headers[cookie_key] = headers[cookie_key].rstrip("; ") + "; " + extra
            else:
                headers["cookie"] = extra
            kwargs["headers"] = headers

        response = original_request(self, method, url, *args, **kwargs)

        if is_funpay:
            try:
                new_cookies = response.cookies.get_dict()
                for name, value in new_cookies.items():
                    if name in ("PHPSESSID", "fav_games"):
                        continue
                    if _funpay_extra_cookies.get(name) != value:
                        logger.debug("Получена новая доп. кука от FunPay.")
                    _funpay_extra_cookies[name] = value
            except Exception:
                pass

        return response

    requests.sessions.Session.request = patched_request
    _funpay_cookie_patch_applied = True


def _format_seconds(seconds: int) -> str:
    """
    Форматирует секунды в читаемый вид для Telegram-уведомлений.
    7200 → "2 ч 00 мин",  90 → "1 мин 30 сек",  45 → "45 сек"
    """
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} ч {m:02d} мин"
    elif seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m} мин {s:02d} сек" if s else f"{m} мин"
    return f"{seconds} сек"


class FunPayClient:
    def __init__(self, golden_key: str):
        _apply_funpay_cookie_patch()

        self.golden_key = golden_key
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.account = FunPayAPI.Account(self.golden_key, self.user_agent, proxy={})
        self.raise_time = {}
        self.session = None
        self.csrf_token = ""  # Переменная для хранения токена

        self.runner = None  # FunPayAPI.Runner - слушатель событий (сообщения/заказы/отзывы)
        self.event_queue: "queue.Queue" = queue.Queue()  # сюда Runner кладет события из отдельного потока

        # Поднятие лотов (в потоке asyncio.to_thread) и Runner (в отдельном Thread)
        # используют ОДИН И ТОТ ЖЕ self.account - общую сессию/CSRF-токен. Без
        # блокировки они могут одновременно дергать сеть: пока Runner готовит
        # запрос со старым токеном, account.get() из другого потока успевает
        # обновить токен - и запрос Runner'а улетает с уже недействительным
        # токеном, из-за чего FunPay отвечает 400. Лок гарантирует, что запросы
        # к self.account всегда идут строго по одному, без гонки.
        self._account_lock = threading.Lock()

        # Чаты, по которым уведомление УЖЕ отправлено, пока они остаются
        # непрочитанными. LAST_CHAT_MESSAGE_CHANGED срабатывает на КАЖДОЕ
        # новое сообщение - без этого набора, если человек написал 2-3
        # сообщения подряд, пока вы не открыли чат, прилетело бы 2-3
        # уведомления. Как только чат снова становится прочитанным
        # (chat.unread == False - вы открыли его на FunPay), id убирается
        # отсюда, и следующее новое сообщение снова даст ровно одно
        # уведомление. Хранится только в памяти - это осознанно: после
        # перезапуска бота максимум придет одно "лишнее" уведомление по
        # уже прочитанному, но еще не переоткрытому чату, что не критично.
        #
        # ХРАНИМ timestamp последнего уведомления (а не просто id в set),
        # чтобы вдобавок к признаку chat.unread работал СТРАХОВОЧНЫЙ таймер
        # (см. _NOTIFY_COOLDOWN_SECONDS ниже): если chat.unread у FunPay
        # обновляется с задержкой/нестабильно и "прочитано" не долетает
        # вовремя, повторное уведомление по тому же чату все равно не уйдет
        # раньше, чем через cooldown - даже если формально unread снова True.
        self._notified_unread_chats: dict[int, float] = {}
        # Не слать повторное уведомление по одному и тому же чату чаще,
        # чем раз в это количество секунд - даже если chat.unread из FunPay
import asyncio
import os
import time
import random
import re
import queue
import threading
import requests
from bs4 import BeautifulSoup
import FunPayAPI
from FunPayAPI.common.enums import SubCategoryTypes
import logger

# ---------------------------------------------------------------------------
# ФИКС ДЛЯ 400 "Необходимая cookie отсутствует или устарела" на runner/
# ---------------------------------------------------------------------------
# Установленная версия FunPayAPI (1.1.0 с PyPI) собирает Cookie-заголовок
# только из golden_key + PHPSESSID (см. self.account.method внутри библиотеки).
# В более новых форках библиотеки (используемых, например, в актуальных сборках
# FunPayCardinal) добавлен отдельный self.cookies - словарь ДОПОЛНИТЕЛЬНЫХ кук,
# которые FunPay стал присылать в Set-Cookie и требовать обратно при каждом
# запросе (в частности к runner/). Установленная версия 1.1.0 этот механизм не
# реализует - отсюда "cookie отсутствует".
#
# Чтобы не переустанавливать/патчить саму библиотеку, патчим requests.Session.request
# на уровне процесса: любой ответ с funpay.com запоминаем все "лишние" куки
# (кроме PHPSESSID, которым и так управляет сама библиотека), и добавляем их
# в заголовок Cookie каждого следующего запроса к funpay.com.
_funpay_extra_cookies: dict[str, str] = {}
_funpay_cookie_patch_applied = False


def _apply_funpay_cookie_patch():
    global _funpay_cookie_patch_applied
    if _funpay_cookie_patch_applied:
        return

    original_request = requests.sessions.Session.request

    def patched_request(self, method, url, *args, **kwargs):
        is_funpay = isinstance(url, str) and "funpay.com" in url

        if is_funpay and _funpay_extra_cookies:
            headers = dict(kwargs.get("headers") or {})
            cookie_key = next((k for k in headers if k.lower() == "cookie"), None)
            extra = "; ".join(f"{k}={v}" for k, v in _funpay_extra_cookies.items())
            if cookie_key:
                if extra not in headers[cookie_key]:
                    headers[cookie_key] = headers[cookie_key].rstrip("; ") + "; " + extra
            else:
                headers["cookie"] = extra
            kwargs["headers"] = headers

        response = original_request(self, method, url, *args, **kwargs)

        if is_funpay:
            try:
                new_cookies = response.cookies.get_dict()
                for name, value in new_cookies.items():
                    if name in ("PHPSESSID", "fav_games"):
                        continue
                    if _funpay_extra_cookies.get(name) != value:
                        logger.debug("Получена новая доп. кука от FunPay.")
                    _funpay_extra_cookies[name] = value
            except Exception:
                pass

        return response

    requests.sessions.Session.request = patched_request
    _funpay_cookie_patch_applied = True


def _format_seconds(seconds: int) -> str:
    """
    Форматирует секунды в читаемый вид для Telegram-уведомлений.
    7200 → "2 ч 00 мин",  90 → "1 мин 30 сек",  45 → "45 сек"
    """
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} ч {m:02d} мин"
    elif seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m} мин {s:02d} сек" if s else f"{m} мин"
    return f"{seconds} сек"


class FunPayClient:
    def __init__(self, golden_key: str):
        _apply_funpay_cookie_patch()

        self.golden_key = golden_key
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.account = FunPayAPI.Account(self.golden_key, self.user_agent, proxy={})
        self.raise_time = {}
        self.session = None
        self.csrf_token = ""  # Переменная для хранения токена

        self.runner = None  # FunPayAPI.Runner - слушатель событий (сообщения/заказы/отзывы)
        self.event_queue: "queue.Queue" = queue.Queue(maxsize=EVENT_QUEUE_CAPACITY)

        # Поднятие лотов (в потоке asyncio.to_thread) и Runner (в отдельном Thread)
        # используют ОДИН И ТОТ ЖЕ self.account - общую сессию/CSRF-токен. Без
        # блокировки они могут одновременно дергать сеть: пока Runner готовит
        # запрос со старым токеном, account.get() из другого потока успевает
        # обновить токен - и запрос Runner'а улетает с уже недействительным
        # токеном, из-за чего FunPay отвечает 400. Лок гарантирует, что запросы
        # к self.account всегда идут строго по одному, без гонки.
        self._account_lock = threading.RLock()
        self._outgoing_echo_lock = threading.Lock()
        self._recent_outgoing_text: dict[int, tuple[str, float]] = {}

        # Чаты, по которым уведомление УЖЕ отправлено, пока они остаются
        # непрочитанными. LAST_CHAT_MESSAGE_CHANGED срабатывает на КАЖДОЕ
        # новое сообщение - без этого набора, если человек написал 2-3
        # сообщения подряд, пока вы не открыли чат, прилетело бы 2-3
        # уведомления. Как только чат снова становится прочитанным
        # (chat.unread == False - вы открыли его на FunPay), id убирается
        # отсюда, и следующее новое сообщение снова даст ровно одно
        # уведомление. Хранится только в памяти - это осознанно: после
        # перезапуска бота максимум придет одно "лишнее" уведомление по
        # уже прочитанному, но еще не переоткрытому чату, что не критично.
        #
        # ХРАНИМ timestamp последнего уведомления (а не просто id в set),
        # чтобы вдобавок к признаку chat.unread работал СТРАХОВОЧНЫЙ таймер
        # (см. _NOTIFY_COOLDOWN_SECONDS ниже): если chat.unread у FunPay
        # обновляется с задержкой/нестабильно и "прочитано" не долетает
        # вовремя, повторное уведомление по тому же чату все равно не уйдет
        # раньше, чем через cooldown - даже если формально unread снова True.
        self._notified_unread_chats: dict[int, float] = {}
        # Не слать повторное уведомление по одному и тому же чату чаще,
        # чем раз в это количество секунд - даже если chat.unread из FunPay
        # почему-то говорит "непрочитано" на каждое новое сообщение.
        self._NOTIFY_COOLDOWN_SECONDS = 90  # 60-90 сек - разумный компромисс, без привязки к недоказанному 5-минутному интервалу

        # Отзыв теперь пытаются поймать ДВА независимых пути (через чат и
        # через закрытие заказа) - без этого набора один и тот же отзыв мог
        # бы улететь в Telegram дважды.
        self._notified_reviews: dict[str, str] = {}
        self._review_notification_lock = asyncio.Lock()
        self._runner_thread: threading.Thread | None = None
        self._runner_start_lock = threading.Lock()
        self._runner_publish_lock = threading.Lock()
        self._runner_stop = threading.Event()
        self._runner_finished = threading.Event()
        self._runner_failure_type: str | None = None
        self._runner_health_lock = threading.Lock()
        self._runner_health = "starting"
        self._runner_last_success_monotonic: float | None = None
        self._runner_consecutive_errors = 0
        self._runner_last_failure_category: str | None = None
        self._runner_last_failure_type: str | None = None
        self._raise_action_gate = threading.local()
        original_account_method = self.account.method

        def action_gated_method(request_method, api_method, headers, payload, *args, **kwargs):
            allowed = getattr(self._raise_action_gate, "is_allowed", None)
            if api_method == "lots/raise" and allowed is not None and not allowed():
                raise _AutobumpActionCancelled()
            return original_account_method(request_method, api_method, headers, payload, *args, **kwargs)

        self.account.method = action_gated_method

    async def get_dashboard(self):
        try:
            balance = await asyncio.to_thread(self.get_dashboard_balance)
            return True, {
                "balance": balance,
                "messages": "0"
            }
        except Exception as e:
            return False, f"Ошибка получения данных: {type(e).__name__}."

    def get_dashboard_balance(self):
        with self._account_lock:
            self.account.get()
            return getattr(self.account, "balance", None) or "—"

    def initialize_account(self):
        with self._account_lock:
            return self.account.get()

    def refresh_session(self):
        with self._account_lock:
            return self.account.get(True)

    def get_order_snapshot(self, order_id: str):
        with self._account_lock:
            order = self.account.get_order(order_id)
            return order, self.account.id

    def send_message_once(self, chat_id: int, text: str, *, enabled_check=None):
        """One modifying call under the existing Account lock; callers never retry."""
        if type(chat_id) is not int or chat_id <= 0 or type(text) is not str or not text.strip():
            raise ValueError("Invalid outgoing message.")
        with self._account_lock:
            if enabled_check is not None and not enabled_check():
                return False
            with self._outgoing_echo_lock:
                self._recent_outgoing_text[chat_id] = (text, time.monotonic())
                if len(self._recent_outgoing_text) > 10000:
                    oldest = next(iter(self._recent_outgoing_text))
                    self._recent_outgoing_text.pop(oldest, None)
            return self.account.send_message(
                chat_id, text, update_last_saved_message=True,
            )

    def _is_recent_outgoing_echo(self, chat_id: int, text: str | None) -> bool:
        with self._outgoing_echo_lock:
            recent = self._recent_outgoing_text.get(chat_id)
            return bool(recent and text == recent[0] and time.monotonic() - recent[1] < 600)

    def get_completed_withdrawals(self) -> list[tuple[str, Decimal, str | None]]:
        """Read-only первая страница Финансов через тот же Account и RLock."""
        with self._account_lock:
            response = self.account.method(
                "get", "account/balance", {"accept": "*/*"}, {}, raise_not_200=True
            )
            content = response.content
        parser = BeautifulSoup(content.decode("utf-8"), "html.parser")
        withdrawals = []
        for item in parser.find_all("div", class_="tc-item"):
            title = item.find("span", class_="tc-title")
            if title is None or not title.get_text(strip=True).startswith("Вывод"):
                continue
            status = item.find("div", class_="tc-status")
            if status is None or status.get_text(strip=True) != "Завершено":
                continue
            transaction_id = item.get("data-transaction")
            price = item.find("div", class_="tc-price")
            if (not isinstance(transaction_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", transaction_id)
                    or price is None):
                raise _WithdrawalParseError()
            unit = price.find("span", class_="unit")
            currency = unit.get_text(strip=True) if unit is not None else None
            amount_text = price.get_text(" ", strip=True)
            if currency:
                amount_text = amount_text.replace(currency, "", 1)
            amount_text = amount_text.replace("−", "-").replace(",", ".").strip()
            amount_text = re.sub(r"^-\s+(?=\d)", "-", amount_text)
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", amount_text):
                raise _WithdrawalParseError()
            try:
                amount = abs(Decimal(amount_text))
            except InvalidOperation:
                raise _WithdrawalParseError() from None
            if not amount.is_finite() or amount <= 0:
                raise _WithdrawalParseError()
            withdrawals.append((transaction_id, amount, currency))
        return withdrawals

    # ВАЖНО: специально НЕ создаем здесь отдельную requests.Session с тем же
    # golden_key (как было раньше в _init_session/_raise_lot_safe). У self.account
    # уже есть своя авторизованная сессия, через которую параллельно работает
    # Runner (слушатель уведомлений). Если поднимать лоты через ВТОРУЮ, независимую
    # сессию с тем же golden_key - FunPay начинает "путать" сессии между собой,
    # и именно это было причиной постоянных "Произошла ошибка при получении
    # событий" в Runner'е. Поэтому поднятие лотов теперь идет через официальный
    # self.account.raise_lots(...) - он использует ТУ ЖЕ сессию, что и Runner,
    # и сам поднимает все разделы (node_id), относящиеся к переданной категории.
    # Положительный RaiseError.wait_time означает штатный cooldown.
    # Прочие ошибки после начала lots/raise остаются неопределёнными.

    async def bump_lots(self, user_id: int, is_cancelled=None):
        abandoned = threading.Event()
        transport_started = threading.Event()
        boundary_lock = threading.Lock()

        def _allow_raise():
            # Синхронизируем late gate с отменой ожидающей coroutine.
            with boundary_lock:
                if abandoned.is_set() or (is_cancelled and is_cancelled()):
                    return False
                transport_started.set()
                return True

        def _raise_if_active(game_id):
            # to_thread может начать работу уже после отмены ожидающей coroutine.
            if abandoned.is_set() or (is_cancelled and is_cancelled()):
                return False
            with self._account_lock:
                if abandoned.is_set() or (is_cancelled and is_cancelled()):
                    return False
                self._raise_action_gate.is_allowed = _allow_raise
                try:
                    self.account.raise_lots(game_id)
                except _AutobumpActionCancelled:
                    return False
                finally:
                    del self._raise_action_gate.is_allowed
            return True

        def _prepare_profile():
            with self._account_lock:
                self.account.get()
                user_profile = self.account.get_user(user_id)
                return user_profile.get_sorted_lots(2)

        try:
            sorted_lots = await asyncio.to_thread(_prepare_profile)
        except Exception as e:
            logger.warning(f"Ошибка чтения профиля (парсинг лотов): {type(e).__name__}.")
            return False, "Ошибка получения данных профиля.", 600

        raised_cats = []
        cooldown_count = 0
        min_wait = 7200
        current_time = time.time()

        sorted_subcats = sorted(list(sorted_lots.keys()), key=lambda x: getattr(x.category, 'position', 0))

        games_to_raise = {}
        for subcat in sorted_subcats:
            if subcat.type is SubCategoryTypes.CURRENCY:
                continue

            game_obj = getattr(subcat, 'category', None) or getattr(subcat, 'game', None)
            if not game_obj or not getattr(game_obj, 'id', None):
                continue

            game_id = int(game_obj.id)
            game_name = getattr(game_obj, 'name', None) or f"Категория #{game_id}"

            if game_id not in games_to_raise:
                games_to_raise[game_id] = {
                    'name': game_name,
                    'node_count': 0
                }

            if getattr(subcat, 'id', None):
                games_to_raise[game_id]['node_count'] += 1

        for game_id, data in games_to_raise.items():
            if is_cancelled and is_cancelled():
                logger.info("Автоподнятие прервано из-за кнопки выключателя.")
                return False, "Автоподнятие остановлено.", 0

            game_name = data['name']
            node_count = data['node_count']

            display_name = f"{game_name} ({node_count} раздела/ов)" if node_count else game_name

            if (saved_time := self.raise_time.get(game_id)) and saved_time > current_time:
                cooldown_count += 1
                min_wait = min(min_wait, int(saved_time - current_time))
                continue

            await asyncio.sleep(random.uniform(5.0, 9.0))

            if is_cancelled and is_cancelled():
                logger.info("Автоподнятие прервано из-за кнопки выключателя.")
                return False, "Автоподнятие остановлено.", 0

            try:
                # account.raise_lots сам поднимает ВСЕ разделы (node_id) данной
                # категории через уже авторизованную сессию self.account.
                performed = await asyncio.to_thread(_raise_if_active, game_id)

            except asyncio.CancelledError:
                with boundary_lock:
                    abandoned.set()
                    uncertain = transport_started.is_set()
                if uncertain:
                    raise _AmbiguousRaiseOutcome() from None
                raise

            except FunPayAPI.exceptions.RaiseError as e:
                wait_time = getattr(e, "wait_time", None)
                if type(wait_time) is not int or wait_time <= 0:
                    wait_time = None
                    error_message = getattr(e, "error_message", None)
                    if isinstance(error_message, str):
                        match = re.search(
                            r"^\s*Подождите\s+([1-9]\d*)\s+"
                            r"(час(?:а|ов)?|минут(?:а|ы)?|секунд(?:а|ы|у)?)(?!\w)",
                            error_message,
                            re.IGNORECASE,
                        )
                        if match:
                            amount = int(match.group(1))
                            unit = match.group(2).lower()
                            if unit.startswith("час"):
                                wait_time = amount * 3600
                            elif unit.startswith("минут"):
                                wait_time = amount * 60
                            else:
                                wait_time = amount + 2  # Как в старой рабочей версии.

                if transport_started.is_set() and type(wait_time) is int and wait_time > 0:
                    self.raise_time[game_id] = time.time() + wait_time
                    min_wait = min(min_wait, wait_time)
                    cooldown_count += 1
                    transport_started.clear()
                    logger.bump(f"[{display_name}] на кулдауне (~{wait_time} сек).")
                    continue

                if not transport_started.is_set():
                    logger.error("Не удалось подготовить lots/raise: RaiseError.")
                    return False, "Не удалось подготовить поднятие лотов.", 600
                logger.error("Результат lots/raise неизвестен: RaiseError.")
                raise _AmbiguousRaiseOutcome() from None

            except Exception as e:
                if not transport_started.is_set():
                    # Ошибка возникла до Account.method("lots/raise");
                    # modifying transport ещё не начинался.
                    logger.error(f"Не удалось подготовить lots/raise: {type(e).__name__}.")
                    return False, "Не удалось подготовить поднятие лотов.", 600
                logger.error(f"Результат lots/raise неизвестен: {type(e).__name__}.")
                raise _AmbiguousRaiseOutcome() from None

            if not performed:
                return False, "Автоподнятие остановлено.", 0

            transport_started.clear()
            wait_time = 7200
            next_time = time.time() + wait_time
            self.raise_time[game_id] = next_time
            # Храним кортеж: (имя игры, кол-во разделов, время кулдауна) — для красивого Telegram-сообщения
            raised_cats.append((game_name, node_count, wait_time))
            logger.success(f"[BUMP] ✅ {display_name}: поднята")

        min_wait_final = max(min_wait, 60)

        # Формируем Telegram-уведомление только по успешно поднятым лотам.
        # Если ничего не поднялось (всё на кулдауне) — сообщение не отправляется.
        if raised_cats:
            msg_lines = ["🚀 <b>Лоты подняты!</b>"]
            for g_name, n_count, w_time in raised_cats:
                msg_lines.append("")
                time_str = _format_seconds(w_time)
                logger.success(f"Лот поднят: {g_name}")
                msg_lines.append(f"🎮 <b>{escape(str(g_name))}</b>")
                msg_lines.append("✅ Лоты подняты")
                msg_lines.append(f"⏳ Повтор для этой игры: через {time_str}")
            msg_lines.append("")
            msg_lines.append(f"🔄 Следующая общая проверка: через {_format_seconds(min_wait_final)}")
            summary_text = "\n".join(msg_lines)
        else:
            summary_text = ""  # Всё на кулдауне — уведомление не нужно

        return True, summary_text, min_wait_final

    # ---------------------------------------------------------------------
    # Уведомления: новые сообщения / заказы / закрытие заказа / отзывы
    # ---------------------------------------------------------------------
    #
    # ВАЖНО про "непрочитанное" (оранжевое) сообщение:
    # Runner.listen() внутри дергает тот же самый long-polling запрос
    # ("runner/"), которым сам сайт FunPay обновляет счетчики и всплывающие
    # уведомления в браузере. Это НЕ равнозначно открытию чата - открытие
    # чата (переход на страницу /chat/?node=...) это отдельный запрос,
    # которого мы здесь НЕ делаем. Поэтому текст нового сообщения мы получаем,
    # а сообщение при этом остается непрочитанным (оранжевым) на сайте и в
    # приложении, пока вы сами не откроете диалог.
    #
    # ВНИМАНИЕ: у библиотеки FunPayAPI нет официальной документации на все
    # поля событий, а установленная у вас версия может немного отличаться.
    # Если что-то из полей ниже не совпадет (например, AttributeError) -
    # смотрите комментарий в конце describe_event(), как быстро это
    # продиагностировать и поправить одну строчку.

    def init_runner(self):
        with self._account_lock:
            if self._runner_stop.is_set():
                raise _RunnerStopRequested()
            self._init_runner_locked()

    def _init_runner_locked(self):
        """
        Инициализирует аккаунт (если еще не инициализирован) и создает Runner.

        ВАЖНО: здесь же накладывается точечная заплатка на баг установленной
        версии библиотеки FunPayAPI. Внутри Runner.get_updates() (файл
        FunPayAPI/updater/runner.py) формируется payload вида:

            payload = {
                "objects": json.dumps([...]),
                "request": False,          # <-- Python bool
                "csrf_token": ...
            }

        При отправке формы requests сериализует Python False в строку "False"
        (с большой буквы). Судя по логам, backend FunPay ожидает ровно "false"
        (как отправляет браузер) и на "False" отвечает 400 Bad Request - из-за
        этого Runner постоянно падает с ошибкой "Произошла ошибка при
        получении событий".

        Мы НЕ трогаем сам файл в site-packages (он слетит при обновлении
        библиотеки), а подменяем account.method только для запросов к
        "runner/", на лету приводя "request": False -> "request": "false"
        прямо перед отправкой. Все остальные запросы (поднятие лотов,
        отправка сообщений и т.д.) эта заплатка не затрагивает.
        """
        if not self.account.is_initiated:
            self.account.get()

        if not getattr(self.account, "_runner_payload_patch_applied", False):
            original_method = self.account.method

            def patched_method(request_method, api_method, headers, payload, *args, **kwargs):
                if api_method == "runner/" and isinstance(payload, dict):
                    # Раньше патчился только ключ "request", но в вашей версии
                    # библиотеки булево поле может называться иначе - поэтому
                    # теперь приводим к "true"/"false" ЛЮБОЕ python bool-значение
                    # в payload запроса runner/, а не только "request".
                    fixed_payload = {}
                    for k, v in payload.items():
                        if v is False:
                            fixed_payload[k] = "false"
                        elif v is True:
                            fixed_payload[k] = "true"
                        else:
                            fixed_payload[k] = v
                    payload = fixed_payload

                # Все запросы к self.account (и от Runner'а, и от поднятия лотов,
                # и от ручных вызовов) идут строго по очереди - без этого гонка
                # потоков за общим CSRF-токеном/сессией валит запросы 400-й ошибкой.
                with self._account_lock:
                    try:
                        if (api_method == "runner/" and self._runner_stop.is_set()
                                and threading.current_thread() is self._runner_thread):
                            raise _RunnerStopRequested()
                        # original_method проверяет допуск autobump уже после захвата lock.
                        return original_method(request_method, api_method, headers, payload, *args, **kwargs)
                    except Exception as e:
                        # Логируем только операцию и тип ошибки: текст исключения,
                        # ответ сервера и состояние Account могут содержать секреты.
                        if api_method == "runner/":
                            logger.debug(f"Ошибка запроса runner/: {type(e).__name__}")
                        raise

            self.account.method = patched_method
            self.account._runner_payload_patch_applied = True

        # -------------------------------------------------------------
        # Отключаем внутри Runner'а построение NEW_MESSAGE
        # -------------------------------------------------------------
        # Мы в describe_event() уже не используем event_types.NEW_MESSAGE -
        # вместо него используется LAST_CHAT_MESSAGE_CHANGED (см. комментарий
        # там), которому НЕ нужен текст сообщения, только факт изменения +
        # ник + unread, и он собирается без доп. запросов из того же
        # long-poll ответа runner/.
        #
        # Но сам Runner (файл FunPayAPI/updater/runner.py) все равно ВСЕГДА
        # пытается построить NEW_MESSAGE - для этого он дергает
        # account.get_chat_history()/get_chats_histories() (это ОТДЕЛЬНЫЙ
        # POST на runner/, не связанный с long-poll'ом). Именно этот
        # отдельный запрос у вас стабильно падает и печатает "Не удалось
        # получить истории чатов [...]: превышено кол-во попыток" - это
        # печатает сама библиотека, не наш код, и патчить эти строки в
        # site-packages бессмысленно (слетит при обновлении).
        #
        # Раз результат этого запроса нам все равно не нужен - подменяем
        # оба метода на "пустышки": они больше не ходят в сеть и мгновенно
        # возвращают пустой результат. Runner получает "сообщений нет",
        # не публикует NEW_MESSAGE, и не пишет никаких ошибок - retry'ев
        # просто не происходит. LAST_CHAT_MESSAGE_CHANGED эти методы не
        # использует вообще, поэтому на реальные уведомления это не влияет.
        #
        # ВНИМАНИЕ: если в будущем понадобится текст сообщения (например,
        # для авто-ответов на конкретные фразы) - эту заплатку нужно будет
        # убрать и разбираться, почему сам запрос к get_chat_history падает.
        if not getattr(self.account, "_history_fetch_disabled", False):
            # The UI may request history explicitly; Runner polling keeps its no-op.
            self._manual_get_chat_history = self.account.get_chat_history
            def _no_op_get_chat_history(chat_id, *args, **kwargs):
                return []

            def _no_op_get_chats_histories(chats_data, *args, **kwargs):
                return {chat_id: [] for chat_id in chats_data}

            self.account.get_chat_history = _no_op_get_chat_history
            self.account.get_chats_histories = _no_op_get_chats_histories
            self.account._history_fetch_disabled = True

        self.runner = FunPayAPI.Runner(self.account)

    def start_runner(self):
        """Запускает единственный producer для этого Account."""
        with self._runner_start_lock:
            if self._runner_thread is not None:
                if self._runner_thread.is_alive():
                    return
                raise RuntimeError("Runner producer уже завершился.")
            if self._runner_stop.is_set():
                raise RuntimeError("Runner producer уже остановлен.")
            thread = threading.Thread(target=self._run_runner, name="funpay-runner", daemon=True)
            self._runner_thread = thread
            try:
                thread.start()
            except BaseException:
                self._runner_thread = None
                raise

    def _run_runner(self):
        try:
            if self._runner_stop.is_set():
                return
            if self.runner is None:
                self.init_runner()
            if not self._runner_stop.is_set():
                self.listen_events(is_cancelled=self._runner_stop.is_set)
            if not self._runner_stop.is_set():
                self._runner_failure_type = "UnexpectedStop"
                with self._runner_health_lock:
                    self._runner_health = "failed"
                    self._runner_last_failure_category = "FATAL"
                    self._runner_last_failure_type = "UnexpectedStop"
                logger.error("Runner producer неожиданно остановился.")
        except _RunnerStopRequested:
            if not self._runner_stop.is_set():
                self._runner_failure_type = "UnexpectedStop"
                with self._runner_health_lock:
                    self._runner_health = "failed"
                    self._runner_last_failure_category = "FATAL"
                    self._runner_last_failure_type = "UnexpectedStop"
                logger.error("Runner producer неожиданно остановился.")
        except Exception as e:
            self._runner_failure_type = type(e).__name__
            if not self._runner_stop.is_set():
                with self._runner_health_lock:
                    if self._runner_health != "failed":
                        self._runner_health = "failed"
                        self._runner_last_failure_category = self._classify_runner_error(e)
                        self._runner_last_failure_type = type(e).__name__
                logger.error(f"Runner producer завершился с ошибкой: {type(e).__name__}.")
        finally:
            if self._runner_stop.is_set():
                with self._runner_health_lock:
                    self._runner_health = "stopped"
            self._runner_finished.set()

    def stop_runner(self):
        """После возврата новые события producer не публикуются."""
        with self._runner_publish_lock:
            self._runner_stop.set()

    async def join_runner(self, timeout: float = 30.0) -> bool:
        """Ограниченно ожидает поток, не блокируя asyncio loop."""
        thread = self._runner_thread
        if thread is None:
            return True
        await asyncio.to_thread(thread.join, timeout)
        if thread.is_alive():
            logger.warning("Runner producer не завершился за время ожидания.")
            return False
        return True

    def runner_failed(self) -> bool:
        return self._runner_finished.is_set() and not self._runner_stop.is_set()

    def runner_stop_requested(self) -> bool:
        return self._runner_stop.is_set()

    def get_runner_health(self) -> dict:
        """Потокобезопасный снимок состояния producer без данных Account/response."""
        with self._runner_health_lock:
            return {
                "state": self._runner_health,
                "last_success_monotonic": self._runner_last_success_monotonic,
                "consecutive_errors": self._runner_consecutive_errors,
                "last_failure_category": self._runner_last_failure_category,
                "last_failure_type": self._runner_last_failure_type,
            }

    @staticmethod
    def _classify_runner_error(error: Exception) -> str:
        if isinstance(error, _EventQueueOverflow):
            return "FATAL"
        if isinstance(error, FunPayAPI.exceptions.UnauthorizedError):
            return "AUTH"
        if isinstance(error, FunPayAPI.exceptions.RequestFailedError):
            status = error.status_code
            if status in (401, 403):
                return "AUTH"
            if status in (408, 429, 500, 502, 503, 504):
                return "RECOVERABLE"
            return "UNKNOWN"
        if isinstance(error, requests.exceptions.SSLError):
            return "FATAL"
        if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return "RECOVERABLE"
        if isinstance(error, (ValueError, TypeError, KeyError, AttributeError, AssertionError)):
            return "FATAL"
        return "UNKNOWN"

    def listen_events(self, is_cancelled=lambda: False, requests_delay: float = 15.0):
        """
        БЛОКИРУЮЩИЙ polling. Запускать ТОЛЬКО в отдельном потоке (Thread),
        а не напрямую в asyncio-цикле - иначе он застопорит весь бот.

        Кладет каждое полученное от FunPay событие в self.event_queue,
        откуда его потом асинхронно забирает основной цикл в main.py.
        """
        if is_cancelled():
            return
        if self.runner is None:
            self.init_runner()

        delay = max(15.0, requests_delay)
        consecutive_errors = 0
        while not is_cancelled():
            try:
                with self._account_lock:
                    updates = self.runner.get_updates()
                    events = self.runner.parse_updates(updates)
                for event in events:
                    with self._runner_publish_lock:
                        if is_cancelled():
                            return
                        try:
                            self.event_queue.put_nowait(event)
                        except queue.Full:
                            logger.error("Event queue overflow.")
                            raise _EventQueueOverflow() from None
            except _RunnerStopRequested:
                raise
            except Exception as e:
                if is_cancelled():
                    return
                category = self._classify_runner_error(e)
                error_type = type(e).__name__
                if category == "RECOVERABLE":
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                with self._runner_health_lock:
                    self._runner_consecutive_errors = consecutive_errors
                    self._runner_last_failure_category = category
                    self._runner_last_failure_type = error_type
                    self._runner_health = "backoff" if category == "RECOVERABLE" and consecutive_errors < 8 else "failed"
                if category != "RECOVERABLE" or consecutive_errors >= 8:
                    logger.error(f"Runner polling остановлен: {category}, {error_type}.")
                    raise
                backoff = min(delay * 2 ** (consecutive_errors - 1), max(delay, 300.0))
                logger.warning(f"Runner polling retry: {category}, {error_type}, попытка {consecutive_errors}/8.")
                if self._runner_stop.wait(backoff):
                    return
                continue

            if is_cancelled():
                return
            consecutive_errors = 0
            with self._runner_health_lock:
                self._runner_last_success_monotonic = time.monotonic()
                self._runner_consecutive_errors = 0
                self._runner_health = "healthy"
            if self._runner_stop.wait(delay):
                return

    def describe_event(self, event):
        """
        Превращает "сырое" событие Runner'а в список уведомлений для Telegram.

        Возвращает список кортежей (ключ_настройки, html_текст).
        Ключи совпадают с ключами bot_settings из telegram.py:
        "notify_message", "notify_order", "notify_review".

        Если событие не требует уведомления (например, это наше собственное
        исходящее сообщение) - возвращается пустой список.
        """
        results = []
        try:
            event_types = FunPayAPI.enums.EventTypes
        except AttributeError:
            # На некоторых сборках EventTypes лежит в FunPayAPI.events
            event_types = FunPayAPI.events.EventTypes

        try:
            if event.type is event_types.LAST_CHAT_MESSAGE_CHANGED:
                # ВАЖНО: раньше уведомление строилось на event_types.NEW_MESSAGE.
                # Для NEW_MESSAGE библиотека внутри Runner'а ДОПОЛНИТЕЛЬНО дергает
                # account.get_chat_history()/get_chats_histories() (отдельный POST
                # на runner/), чтобы достать текст сообщения - и именно это у вас
                # стабильно падало с "Не удалось получить истории чатов [...]:
                # превышено кол-во попыток", из-за чего NEW_MESSAGE вообще не
                # долетал и уведомлений не было.
                #
                # LAST_CHAT_MESSAGE_CHANGED дополнительных запросов не делает -
                # он собирается из той же самой пачки runner/, которую Runner и
                # так постоянно опрашивает (это чат-панель/список диалогов сайта,
                # где FunPay уже присылает id чата, ник собеседника и признак
                # "непрочитано" одним куском). Поэтому это событие приходит
                # надежно и не зависит от бага с историей чатов.
                #
                # Правда, полного текста сообщения тут официально нет - только
                # факт "у чата изменилось последнее сообщение", ник и unread.
                # Вам как раз это и нужно (уведомление без текста/ссылки).
                chat = event.chat
                chat_id = getattr(chat, "id", None)

                # ВРЕМЕННЫЙ диагностический принт - если повторы все еще
                # будут проскакивать после этой правки, пришлите мне пару
                # таких строк подряд (для одного и того же чата, где было
                # 2+ сообщения) - по значению "unread" сразу станет видно,
                # обновляется ли этот флаг у FunPay вовремя или нет.
                logger.debug(f"chat_id={chat_id} unread={getattr(chat, 'unread', '<нет поля>')} "
                      f"last_message_type={getattr(chat, 'last_message_type', '<нет поля>')}")

                # Пропускаем системные записи в чате (создание/закрытие заказа
                # и т.п.) - под них есть отдельные события NEW_ORDER /
                # ORDER_STATUS_CHANGED выше и ниже по коду.
                message_types = None
                for candidate in (
                    getattr(FunPayAPI, "types", None),
                    getattr(FunPayAPI, "enums", None),
                    getattr(FunPayAPI, "common", None) and getattr(FunPayAPI.common, "enums", None),
                ):
                    if candidate is not None and hasattr(candidate, "MessageTypes"):
                        message_types = candidate.MessageTypes
                        break

                if message_types is not None:
                    non_system = getattr(message_types, "NON_SYSTEM", None)
                    msg_type = getattr(chat, "last_message_type", None)
                    if non_system is not None and msg_type is not None and msg_type is not non_system:
                        # Берём текст: Кардинал в types.py читает last_message_text
                        # напрямую (res.ORDER_ID.search(self.last_message_text)).
                        # У нашей версии библиотеки этот атрибут тоже есть.
                        msg_text = getattr(chat, "last_message_text", None) or (str(chat) if chat is not None else "")
                        new_fb = getattr(message_types, "NEW_FEEDBACK", None)
                        changed_fb = getattr(message_types, "FEEDBACK_CHANGED", None)
                        
                        if msg_type in (new_fb, changed_fb) and msg_type is not None:
                            # Точный regex Кардинала (utils.py): r"#[A-Z0-9]{8}"
                            # Номер заказа — ровно 8 символов в верхнем регистре.
                            match = re.search(r'#([A-Z0-9]{8})', msg_text)
                            if match:
                                order_id = match.group(1)
                                results.append(("_review_check_immediate", order_id))
                        
                        # Остальные системные сообщения игнорируем
                        return results

                # LAST_CHAT_MESSAGE_CHANGED содержит ChatShortcut без author_id.
                # Подавляем эхо собственных ночных автоответов до нового маркера:
                # unread сам по себе не доказывает направление сообщения.
                author_id = getattr(chat, "last_message_author_id", None)
                account_id = getattr(self.account, "id", None)
                if (type(author_id) is int and type(account_id) is int
                        and author_id == account_id):
                    return results
                from telegram import is_night_mode_reply_text
                if is_night_mode_reply_text(getattr(chat, "last_message_text", None)):
                    return results
                if (type(chat_id) is int and self._is_recent_outgoing_echo(
                        chat_id, getattr(chat, "last_message_text", None))):
                    return results

                # Системное событие отзыва не обязано делать чат непрочитанным.
                # Дедуп обычных сообщений не должен скрывать NEW_FEEDBACK.
                if not getattr(chat, "unread", False):
                    if chat_id is not None:
                        self._notified_unread_chats.pop(chat_id, None)
                    return results

                now = time.time()
                last_notified = self._notified_unread_chats.get(chat_id) if chat_id is not None else None
                if last_notified is not None and (now - last_notified) < self._NOTIFY_COOLDOWN_SECONDS:
                    return results

                author = escape(str(getattr(chat, "name", None) or "Покупатель"))
                chat_link = f"https://funpay.com/chat/?node={chat_id}" if chat_id is not None else None

                if chat_link:
                    who_line = f'<a href="{chat_link}">{author}</a>: {chat_link}'
                else:
                    who_line = author

                if chat_id is not None:
                    self._notified_unread_chats[chat_id] = now

                text = (
                    f"На аккаунте {escape(str(self.account.username))} есть непрочитанные сообщения.\n"
                    f"{who_line}"
                )
                results.append(("notify_message", text))
                if chat_id is not None:
                    results.append(("_night_mode_reply_message", str(chat_id)))

            elif event.type is event_types.NEW_ORDER:
                order = event.order
                buyer = getattr(order, "buyer_username", None) or "—"
                amount = getattr(order, "price", None) or getattr(order, "sum", None) or "—"
                descr = getattr(order, "description", None) or getattr(order, "title", None) or "—"

                text = (
                    "💰 <b>Оплачен новый заказ</b>\n"
                    f"Покупатель: <b>{escape(str(buyer))}</b>\n"
                    f"Сумма: {escape(str(amount))}\n"
                    f"Описание: {escape(str(descr))}"
                )
                results.append(("notify_order", text))
                direct_chat_id = getattr(order, "chat_id", None)
                if (isinstance(buyer, str) and buyer != "—") or type(direct_chat_id) is int:
                    results.append(("_night_mode_reply_order", (buyer, direct_chat_id)))

            elif event.type is event_types.ORDER_STATUS_CHANGED:
                order = event.order
                status = getattr(order, "status", None)
                buyer = getattr(order, "buyer_username", None) or "—"
                order_id = getattr(order, "id", "—")

                closed_status = getattr(FunPayAPI.types.OrderStatuses, "CLOSED", None)
                refunded_status = getattr(FunPayAPI.types.OrderStatuses, "REFUNDED", None)

                if closed_status is not None and status == closed_status:
                    text = (
                        "✅ <b>Заказ закрыт покупателем</b>\n"
                        f"Покупатель: <b>{escape(str(buyer))}</b>\n"
                        f"№ заказа: {escape(str(order_id))}"
                    )
                    results.append(("notify_order", text))

                    # ВТОРОЙ, независимый способ поймать отзыв (в дополнение к
                    # LAST_CHAT_MESSAGE_CHANGED выше). Судя по скриншоту, "Покупатель
                    # написал отзыв" приходит через отдельный системный канал
                    # "FunPay [оповещение]", а не обязательно через обычный чат с
                    # покупателем - поэтому парсинг чата иногда мог не поймать
                    # событие вовсе. При ЛЮБОМ закрытии заказа (отзыв к этому
                    # моменту мог уже быть оставлен - как раз видно на скриншоте,
                    # где "написал отзыв" пришло раньше, чем "закрыл заказ")
                    # дополнительно дергаем сам заказ и проверяем order.review
                    # напрямую - без всякого парсинга текста чата. Дублирование
                    # с чат-путем не страшно: main.py посылает уведомление только
                    # если review реально найден, а по каждому order_id/чату это
                    # сработает максимум пару раз за жизнь заказа.
                    if order_id and order_id != "—":
                        results.append(("_review_check", order_id))

                elif refunded_status is not None and status == refunded_status:
                    text = (
                        "↩️ <b>Оформлен возврат по заказу</b>\n"
                        f"Покупатель: <b>{escape(str(buyer))}</b>\n"
                        f"№ заказа: {escape(str(order_id))}"
                    )
                    results.append(("notify_order", text))

            # Обработка события NEW_REVIEW — присутствует в некоторых версиях
            # FunPayAPI. Проверяем защищённо через getattr, чтобы не ломаться
            # на старых версиях библиотеки, где его нет.
            new_review_et = getattr(event_types, "NEW_REVIEW", None)
            if new_review_et is not None and event.type is new_review_et:
                review = getattr(event, "review", None)
                order_id = getattr(review, "order_id", None)
                if isinstance(order_id, str) and re.fullmatch(r"[A-Z0-9]{8}", order_id):
                    results.append(("_review_check_immediate", order_id))
                else:
                    logger.notify("Событие отзыва пропущено: нет корректного ID заказа.")

        except Exception as e:
            # Если тут вылетает AttributeError - значит установленная версия
            # FunPayAPI называет поля чуть иначе. Быстрая диагностика:
            # print(vars(event)) и print(vars(event.message)) / print(vars(event.order))
            # покажут реальные имена полей - останется поправить строчку выше.
            logger.error(f"Ошибка разбора события: {type(e).__name__}.")

        return results
