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
                        logger.debug(f"Получена новая доп. кука от FunPay: {name}")
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
                        logger.debug(f"Получена новая доп. кука от FunPay: {name}")
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
        # почему-то говорит "непрочитано" на каждое новое сообщение.
        self._NOTIFY_COOLDOWN_SECONDS = 90  # 60-90 сек - разумный компромисс, без привязки к недоказанному 5-минутному интервалу

        # Отзыв теперь пытаются поймать ДВА независимых пути (через чат и
        # через закрытие заказа) - без этого набора один и тот же отзыв мог
        # бы улететь в Telegram дважды.
        self._notified_reviews: set[str] = set()
        self._review_notification_lock = asyncio.Lock()

    async def get_dashboard(self):
        try:
            await asyncio.to_thread(self.account.get)
            # Реальный баланс берем из атрибутов аккаунта (если библиотека их предоставляет).
            # Если поле недоступно в текущей версии FunPayAPI — возвращаем прочерк.
            balance = getattr(self.account, "balance", None) or "—"
            return True, {
                "balance": balance,
                "messages": "0"
            }
        except Exception as e:
            return False, str(e)

    # ВАЖНО: специально НЕ создаем здесь отдельную requests.Session с тем же
    # golden_key (как было раньше в _init_session/_raise_lot_safe). У self.account
    # уже есть своя авторизованная сессия, через которую параллельно работает
    # Runner (слушатель уведомлений). Если поднимать лоты через ВТОРУЮ, независимую
    # сессию с тем же golden_key - FunPay начинает "путать" сессии между собой,
    # и именно это было причиной постоянных "Произошла ошибка при получении
    # событий" в Runner'е. Поэтому поднятие лотов теперь идет через официальный
    # self.account.raise_lots(...) - он использует ТУ ЖЕ сессию, что и Runner,
    # и сам поднимает все разделы (node_id), относящиеся к переданной категории.
    # Он же кидает FunPayAPI.exceptions.RaiseError с уже готовым wait_time в
    # секундах - больше не нужно парсить текст ошибки регулярками (что раньше
    # ломалось на ответах вида "Подождите 4 секунды" и ставило кулдаун 7200 сек).

    async def bump_lots(self, user_id: int, is_cancelled=None):
        abandoned = threading.Event()

        def _raise_if_active(game_id):
            # to_thread может начать работу уже после отмены ожидающей coroutine.
            if abandoned.is_set() or (is_cancelled and is_cancelled()):
                return False
            self.account.raise_lots(game_id)
            return True

        def _prepare_profile():
            self.account.get()
            user_profile = self.account.get_user(user_id)
            sorted_lots = user_profile.get_sorted_lots(2)
            return sorted_lots

        try:
            sorted_lots = await asyncio.to_thread(_prepare_profile)
        except Exception as e:
            logger.warning(f"Ошибка чтения профиля (парсинг лотов): {e}")
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
                if not await asyncio.to_thread(_raise_if_active, game_id):
                    return False, "Автоподнятие остановлено.", 0

                wait_time = 7200
                next_time = time.time() + wait_time
                self.raise_time[game_id] = next_time
                # Храним кортеж: (имя игры, кол-во разделов, время кулдауна) — для красивого Telegram-сообщения
                raised_cats.append((game_name, node_count, wait_time))
                logger.success(f"[BUMP] ✅ {display_name}: поднята")

            except asyncio.CancelledError:
                abandoned.set()
                raise

            except FunPayAPI.exceptions.RaiseError as e:
                # У этой версии библиотеки error_message часто пустой, а str(e) -
                # некрасивый полный дамп запроса/ответа вместо человеческого текста.
                # Достаем настоящее сообщение FunPay напрямую из тела ответа.
                raw_msg = None
                for attr in ("error_message", "msg", "message"):
                    val = getattr(e, attr, None)
                    if val:
                        raw_msg = str(val)
                        break
                if not raw_msg:
                    resp = getattr(e, "response", None)
                    if resp is not None:
                        try:
                            raw_msg = resp.json().get("msg")
                        except Exception:
                            pass
                error_msg = raw_msg or "Кулдаун (сервер не прислал текст ошибки)"

                # wait_time от библиотеки тоже не всегда парсится верно (иногда None
                # даже когда текст ошибки содержит конкретное время) - на этот случай
                # парсим сами: часы/минуты/секунды в тексте FunPay.
                wait_time = None
                lib_wait = getattr(e, "wait_time", None)
                if lib_wait:
                    wait_time = int(lib_wait)
                else:
                    text_lower = error_msg.lower()
                    num_match = re.search(r'(\d+)', text_lower)
                    if num_match:
                        if "час" in text_lower:
                            wait_time = int(num_match.group(1)) * 3600
                        elif "минут" in text_lower:
                            wait_time = int(num_match.group(1)) * 60
                        elif "секунд" in text_lower:
                            wait_time = int(num_match.group(1)) + 2

                if not wait_time:
                    wait_time = 7200
                wait_time = max(wait_time, 1)

                next_time = time.time() + wait_time
                self.raise_time[game_id] = next_time
                min_wait = min(min_wait, wait_time)
                cooldown_count += 1
                # Было logger.debug() - без DEBUG=1 в .env эта строка не
                # печаталась ВООБЩЕ, из-за чего снаружи казалось, что бот
                # ничего не делает, хотя он честно ставил лоты на кулдаун.
                # Это обычная штатная работа, а не ошибка - поэтому bump(),
                # а не warning()/error().
                logger.bump(f"[{display_name}] на кулдауне. Ответ: '{error_msg}' (~{wait_time} сек)")

            except Exception as e:
                # Сетевые/непредвиденные ошибки - короткий повтор, а не 2 часа простоя
                wait_time = random.randint(30, 60)
                logger.error(f"[{display_name}] непредвиденная ошибка при поднятии: {e} (~{wait_time} сек)")
                next_time = time.time() + wait_time
                self.raise_time[game_id] = next_time
                min_wait = min(min_wait, wait_time)
                cooldown_count += 1

        min_wait_final = max(min_wait, 60)

        # Формируем Telegram-уведомление только по успешно поднятым лотам.
        # Если ничего не поднялось (всё на кулдауне) — сообщение не отправляется.
        if raised_cats:
            msg_lines = ["🚀 <b>Лоты подняты!</b>"]
            for g_name, n_count, w_time in raised_cats:
                msg_lines.append("")
                time_str = _format_seconds(w_time)
                logger.success(f"Лот поднят: {g_name}")
                msg_lines.append(f"🎮 <b>{g_name}</b>")
                msg_lines.append(f"⏰ Следующий подъём через {time_str}")
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
            def _no_op_get_chat_history(chat_id, *args, **kwargs):
                return []

            def _no_op_get_chats_histories(chats_data, *args, **kwargs):
                return {chat_id: [] for chat_id in chats_data}

            self.account.get_chat_history = _no_op_get_chat_history
            self.account.get_chats_histories = _no_op_get_chats_histories
            self.account._history_fetch_disabled = True

        self.runner = FunPayAPI.Runner(self.account)

    def listen_events(self, is_cancelled=lambda: False, requests_delay: float = 15.0):
        """
        БЛОКИРУЮЩИЙ генератор. Запускать ТОЛЬКО в отдельном потоке (Thread),
        а не напрямую в asyncio-цикле - иначе он застопорит весь бот.

        requests_delay увеличен с 6 до 15 сек, чтобы при повторяющихся сбоях
        сообщения об ошибке не сыпались в Telegram/консоль слишком часто.

        Кладет каждое полученное от FunPay событие в self.event_queue,
        откуда его потом асинхронно забирает основной цикл в main.py.
        """
        if self.runner is None:
            self.init_runner()

        for event in self.runner.listen(requests_delay=requests_delay):
            if is_cancelled():
                break
            self.event_queue.put(event)

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

                # unread=False - либо уже прочитано на сайте, либо это НАШЕ
                # исходящее сообщение (учитывается автоматически, доп. проверка
                # author_id тут не нужна). Если чат стал прочитанным - убираем
                # его из "уже уведомили", чтобы СЛЕДУЮЩАЯ новая непрочитанная
                # серия сообщений снова дала уведомление.
                if not getattr(chat, "unread", False):
                    if chat_id is not None:
                        self._notified_unread_chats.pop(chat_id, None)
                    return results

                # Уведомление по этому чату уже отправлялось недавно -
                # пропускаем повтор. Раньше это проверялось ТОЛЬКО через
                # chat.unread (пока не станет False), но если этот флаг
                # у FunPay обновляется с задержкой или "мигает" - повторные
                # уведомления все равно проскакивали, даже если вы читали
                # сообщение мгновенно. Теперь дополнительно действует
                # страховочный таймер: если чат уже уведомлялся
                # < _NOTIFY_COOLDOWN_SECONDS секунд назад - не шлем снова,
                # независимо от того, что говорит chat.unread.
                now = time.time()
                last_notified = self._notified_unread_chats.get(chat_id) if chat_id is not None else None
                if last_notified is not None and (now - last_notified) < self._NOTIFY_COOLDOWN_SECONDS:
                    return results

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
                                if chat_id is not None:
                                    self._notified_unread_chats[chat_id] = now
                        
                        # Остальные системные сообщения игнорируем
                        return results

                author = getattr(chat, "name", None) or "Покупатель"
                chat_link = f"https://funpay.com/chat/?node={chat_id}" if chat_id is not None else None

                if chat_link:
                    who_line = f'<a href="{chat_link}">{author}</a>: {chat_link}'
                else:
                    who_line = author

                if chat_id is not None:
                    self._notified_unread_chats[chat_id] = now

                text = (
                    f"На аккаунте {self.account.username} есть непрочитанные сообщения.\n"
                    f"{who_line}"
                )
                results.append(("notify_message", text))

            elif event.type is event_types.NEW_ORDER:
                order = event.order
                buyer = getattr(order, "buyer_username", None) or "—"
                amount = getattr(order, "price", None) or getattr(order, "sum", None) or "—"
                descr = getattr(order, "description", None) or getattr(order, "title", None) or "—"

                text = (
                    "💰 <b>Оплачен новый заказ</b>\n"
                    f"Покупатель: <b>{buyer}</b>\n"
                    f"Сумма: {amount}\n"
                    f"Описание: {descr}"
                )
                results.append(("notify_order", text))

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
                        f"Покупатель: <b>{buyer}</b>\n"
                        f"№ заказа: {order_id}"
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
                        f"Покупатель: <b>{buyer}</b>\n"
                        f"№ заказа: {order_id}"
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
            logger.error(f"Ошибка разбора события {getattr(event, 'type', '?')}: {e}")

        return results
