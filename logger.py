"""
logger.py — Красивый консольный логгер в стиле FunPay Cardinal.

Уровни:
  success / ok  — зелёный   ✔  — успешная операция
  info          — белый     ·  — информационное сообщение
  warning       — жёлтый    !  — некритичная ошибка / предупреждение
  error         — красный   ✘  — критичная ошибка
  bump          — голубой   ▲  — события цикла автоподнятия
  notify        — фиолетов  ●  — входящие события FunPay
  debug         — серый     ·  — технические детали (только при DEBUG=1)

Использование:
    import logger
    logger.success("Сессия FunPay инициализирована: ExampleSeller")
    logger.bump("ARC Raiders — поднята")
    logger.notify("Новое сообщение от buyer123")
    logger.warning("Кулдаун, повтор через 7200 сек")
    logger.error("Ошибка соединения")
    logger.debug("Ошибка запроса: RuntimeError")  # только при DEBUG=1

Файл logs/bot_ГГГГ-ММ-ДД.log создаётся при первой записи за день. В него
попадают только сообщения, выведенные application logger в консоль; скрытые
debug-сообщения туда не копируются.
"""

import logging
import os
import re
import threading
from datetime import datetime


class _SafeFunPayApiFilter(logging.Filter):
    """Пропускает только известные фиксированные сообщения библиотеки."""

    def __init__(self, allowed: frozenset[str]):
        super().__init__()
        self.allowed = allowed

    def filter(self, record: logging.LogRecord) -> bool:
        return (record.levelno >= logging.WARNING and type(record.msg) is str
                and record.msg in self.allowed and not record.args
                and record.exc_info is None and record.stack_info is None)


def configure_funpayapi_logging() -> None:
    # Эти два logger'а используются установленной FunPayAPI 1.1.0.
    # Отбрасываем raw response/exception records до попадания в root handlers.
    account = logging.getLogger("FunPayAPI.account")
    runner = logging.getLogger("FunPayAPI.runner")
    account.setLevel(logging.WARNING)
    runner.setLevel(logging.WARNING)
    account.addFilter(_SafeFunPayApiFilter(frozenset()))
    runner.addFilter(_SafeFunPayApiFilter(frozenset({
        "Не удалось обновить список заказов.",
        "Не удалось обновить список продаж: превышено кол-во попыток.",
        "Произошла ошибка при получении событий. (ничего страшного, если это сообщение появляется нечасто).",
    })))


configure_funpayapi_logging()

# Включаем ANSI-escape-коды на Windows (cmd / PowerShell / Windows Terminal)
if os.name == "nt":
    os.system("")

# ── ANSI-палитра ──────────────────────────────────────────────────────────────
_R  = "\033[0m"   # reset
_B  = "\033[1m"   # bold
_G  = "\033[92m"  # bright green
_Y  = "\033[93m"  # bright yellow
_RE = "\033[91m"  # bright red
_C  = "\033[96m"  # bright cyan
_M  = "\033[95m"  # bright magenta
_GR = "\033[90m"  # dark gray
_W  = "\033[97m"  # bright white
# ─────────────────────────────────────────────────────────────────────────────

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_file_lock = threading.Lock()
_ansi_escape = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_credential_label = re.compile(
    r"(?i)\b(?:golden_key|funpay_golden_key|bot_token|password|api_key|"
    r"cookie|phpsessid|csrf|csrf_token|authorization|proxy_password)\s*[:=]"
)
_telegram_token = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")


def _safe_message(msg: str) -> str:
    if type(msg) is not str:
        return "[нестроковое сообщение]"
    clean = _ansi_escape.sub("", msg)
    clean = " ".join("".join(ch if ch.isprintable() else " " for ch in clean).split())
    secret = _credential_label.search(clean)
    if secret:
        clean = clean[:secret.start()] + "[секрет скрыт]"
    return _telegram_token.sub("[секрет скрыт]", clean)


def _write_to_file(tag: str, msg: str) -> None:
    """Дублирует разрешённое консольное сообщение без ANSI и секретных значений."""
    try:
        clean = _safe_message(msg)
        with _file_lock:
            os.makedirs(LOGS_DIR, exist_ok=True)
            now = datetime.now()
            path = os.path.join(LOGS_DIR, f"bot_{now.strftime('%Y-%m-%d')}.log")
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(f"[{now.strftime('%H:%M:%S')}] {tag}  {clean}\n")
    except Exception:
        # Ошибка файловой системы не должна прерывать консольный лог или runtime.
        pass


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _line(color: str, icon: str, tag: str, msg: str) -> None:
    clean = _safe_message(msg)
    print(f"{_GR}[{_ts()}]{_R} {color}{_B}{icon} {tag}{_R}  {clean}")
    _write_to_file(tag, clean)


# ── Публичный API ─────────────────────────────────────────────────────────────

def success(msg: str) -> None:
    """Успешная операция (зелёный)."""
    _line(_G, "✔", "OK   ", msg)


def info(msg: str) -> None:
    """Информационное сообщение (белый)."""
    _line(_W, "·", "INFO ", msg)


def warning(msg: str) -> None:
    """Предупреждение / некритичная ошибка (жёлтый)."""
    _line(_Y, "!", "WARN ", msg)


def error(msg: str) -> None:
    """Критичная ошибка (красный)."""
    _line(_RE, "✘", "ERROR", msg)


def bump(msg: str) -> None:
    """Событие цикла автоподнятия лотов (голубой)."""
    _line(_C, "▲", "BUMP ", msg)


def notify(msg: str) -> None:
    """Входящее событие FunPay — сообщение / заказ / отзыв (фиолетовый)."""
    _line(_M, "●", "EVENT", msg)


def debug(msg: str) -> None:
    """Технический лог. Выводится только если задана переменная DEBUG=1."""
    if os.getenv("DEBUG", "0") == "1":
        _line(_GR, "·", "DEBUG", msg)


def banner(username: str = "") -> None:
    """Красивый заголовок при старте. Вызывается после подключения к FunPay."""
    username = _safe_message(username)
    acc_line = f"   Аккаунт FunPay: {username}" if username else ""
    pad = 40 - len(acc_line) if acc_line else 0

    print(f"\n{_C}{_B}  ╔══════════════════════════════════════╗{_R}")
    print(f"{_C}{_B}  ║       🚀  FunPay SecureBot           ║{_R}")
    if acc_line:
        print(f"{_C}{_B}  ║{_R}{acc_line}{' ' * pad}{_C}{_B}║{_R}")
    print(f"{_C}{_B}  ╚══════════════════════════════════════╝{_R}\n")
    _write_to_file("START", f"=== Запуск бота (аккаунт: {username or '?'}) ===")
