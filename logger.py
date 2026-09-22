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
    logger.success("Сессия FunPay инициализирована: Vitas1975")
    logger.bump("ARC Raiders — поднята")
    logger.notify("Новое сообщение от buyer123")
    logger.warning("Кулдаун, повтор через 7200 сек")
    logger.error("Ошибка соединения")
    logger.debug("Тело ответа: ...")  # только при DEBUG=1
"""

import os
from datetime import datetime

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


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _line(color: str, icon: str, tag: str, msg: str) -> None:
    print(f"{_GR}[{_ts()}]{_R} {color}{_B}{icon} {tag}{_R}  {msg}")


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
    acc_line = f"   Аккаунт FunPay: {username}" if username else ""
    pad = 40 - len(acc_line) if acc_line else 0

    print(f"\n{_C}{_B}  ╔══════════════════════════════════════╗{_R}")
    print(f"{_C}{_B}  ║       🚀  FunPay SecureBot           ║{_R}")
    if acc_line:
        print(f"{_C}{_B}  ║{_R}{acc_line}{' ' * pad}{_C}{_B}║{_R}")
    print(f"{_C}{_B}  ╚══════════════════════════════════════╝{_R}\n")
