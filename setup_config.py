"""Local first-run configuration; never validates credentials over the network."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from console_ui import InstallerConsole, project_version, use_utf8_console
from runtime_paths import CODE_DIR, DATA_DIR_ENV


FIELDS = ("FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "ADMIN_ID", "FUNPAY_USER_ID",
          "BOT_PASSWORD", "DEBUG")
SECRET_FIELDS = frozenset({"FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "BOT_PASSWORD"})
POINTER = CODE_DIR / ".install-data-dir"
LANGUAGE_FILE = "installer_language.txt"

TEXT = {
    "ru": {
        "initial": "Первоначальная настройка",
        "existing": "Найдена существующая конфигурация.",
        "keep": "[1] Оставить текущую",
        "edit": "[2] Изменить",
        "cancel_option": "[3] Отмена",
        "choose": "Выберите [1]: ",
        "choose_error": "Выберите 1, 2 или 3.",
        "kept": "Существующая конфигурация сохранена без изменений.",
        "accepted": "Значение принято.",
        "save_heading": "[4/4] Сохранение конфигурации",
        "saved": "Конфигурация сохранена.",
        "empty": "Значение не может быть пустым.",
        "control": "Вставленное значение содержит недопустимые символы.",
        "telegram_id": "Telegram ID должен содержать только положительные цифры.",
        "funpay_id": "FunPay ID должен содержать только положительные цифры.",
        "debug": "Для режима диагностики введите 0 или 1.",
        "clipboard": "Текст буфера обмена недоступен. Вставьте или введите значение снова.",
        "absolute": "Каталог данных должен быть абсолютным путём.",
        "data_prompt": "Каталог приватных данных",
        "completed": "Настройка завершена",
        "data_dir": "Каталог данных:",
        "next": "Следующий шаг:\nЗапустите Start.bat",
        "canceled": "Настройка отменена. Существующая конфигурация не изменена.",
        "interrupted": "Настройка прервана. Конфигурация не изменена.",
        "save_error": "Не удалось сохранить конфигурацию. Проверьте каталог данных и права доступа.",
    },
    "en": {
        "initial": "Initial setup",
        "existing": "Existing configuration found.",
        "keep": "[1] Keep current configuration",
        "edit": "[2] Edit configuration",
        "cancel_option": "[3] Cancel",
        "choose": "Choose [1]: ",
        "choose_error": "Choose 1, 2, or 3.",
        "kept": "Existing configuration kept unchanged.",
        "accepted": "Value accepted.",
        "save_heading": "[4/4] Save configuration",
        "saved": "Configuration saved.",
        "empty": "The value is empty.",
        "control": "Pasted value contains unsupported control characters.",
        "telegram_id": "Telegram ID must contain a positive number of digits only.",
        "funpay_id": "FunPay account ID must contain a positive number of digits only.",
        "debug": "Enter 0 or 1 for debug mode.",
        "clipboard": "Clipboard text is unavailable. Paste or type the value again.",
        "absolute": "The data directory must be an absolute path.",
        "data_prompt": "Private data directory",
        "completed": "Setup completed",
        "data_dir": "Data directory:",
        "next": "Next step: Run Start.bat",
        "canceled": "Setup canceled. Existing configuration unchanged.",
        "interrupted": "Setup interrupted. Configuration was not changed.",
        "save_error": "Could not save configuration. Check the data directory and permissions.",
    },
}

FIELD_UI = {
    "ru": (
        ("FUNPAY_GOLDEN_KEY", "[1/4] Авторизация FunPay",
         "Ключ авторизации FunPay. Хранится только на этом компьютере.",
         "Golden Key (скрытый ввод): "),
        ("BOT_TOKEN", "[2/4] Telegram-бот",
         "Токен Telegram-бота от BotFather.", "Bot Token (скрытый ввод): "),
        ("ADMIN_ID", "[3/4] Аккаунт владельца",
         "Ваш числовой Telegram ID.", "Telegram ID: "),
        ("FUNPAY_USER_ID", None,
         "Ваш числовой ID аккаунта FunPay.", "FunPay ID: "),
        ("BOT_PASSWORD", None,
         "Пароль бота. Необязательно; оставьте пустым, если не нужен.",
         "Пароль бота (скрытый ввод): "),
        ("DEBUG", None,
         "Режим диагностики: 0 — обычный режим, 1 — диагностика.",
         "Режим диагностики [0]: "),
    ),
    "en": (
        ("FUNPAY_GOLDEN_KEY", "[1/4] FunPay authorization",
         "FunPay authorization key. It stays only on this computer.",
         "Golden Key (hidden): "),
        ("BOT_TOKEN", "[2/4] Telegram bot",
         "Telegram bot token from BotFather.", "Bot Token (hidden): "),
        ("ADMIN_ID", "[3/4] Owner account",
         "Your numeric Telegram user ID.", "Telegram ID: "),
        ("FUNPAY_USER_ID", None,
         "Your numeric FunPay account ID.", "FunPay ID: "),
        ("BOT_PASSWORD", None,
         "Optional bot password. Leave blank if unused.",
         "Bot password (hidden, optional): "),
        ("DEBUG", None,
         "Diagnostic mode: 0 for normal use, 1 for diagnostics.",
         "Diagnostic mode [0]: "),
    ),
}


def load_language(data_directory: Path) -> str:
    """Unknown or missing preferences safely fall back to Russian."""
    try:
        saved = (data_directory / LANGUAGE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "ru"
    return saved if saved in TEXT else "ru"


class SetupCancelled(Exception):
    """The owner chose to leave the existing configuration untouched."""


def _version() -> str:
    return project_version(CODE_DIR)


def _windows_clipboard_text() -> str | None:
    """Read Unicode text only when Ctrl+V is pressed; never display it."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _windows_secret_input(prompt: str, *, language: str = "ru", getch=None,
                          clipboard=None, stream=None, on_change=None) -> str:
    """Hidden Windows input supporting both terminal paste and Ctrl+V clipboard paste."""
    if getch is None:
        import msvcrt
        getch = msvcrt.getwch
    if clipboard is None:
        clipboard = _windows_clipboard_text
    if stream is None:
        stream = sys.stdout
    if prompt:
        stream.write(prompt)
        stream.flush()
    chars: list[str] = []
    try:
        while True:
            char = getch()
            if char in {"\r", "\n"}:
                return "".join(chars)
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1a":
                raise EOFError
            if char in {"\x00", "\xe0"}:
                getch()  # Ignore the second code of a navigation/function key.
            elif char == "\b":
                if chars:
                    chars.pop()
                    if on_change is not None:
                        on_change(bool(chars))
            elif char == "\x16":
                pasted = clipboard()
                if pasted is None:
                    raise ValueError(TEXT[language]["clipboard"])
                chars.extend(pasted)
                if on_change is not None:
                    on_change(bool(chars))
            else:
                chars.append(char)
                if on_change is not None:
                    on_change(True)
    finally:
        if prompt:
            stream.write("\n")
            stream.flush()


def _secret_input(prompt: str, *, language: str = "ru", on_change=None) -> str:
    if os.name == "nt":
        if not sys.stdin.isatty():
            raise EOFError("Hidden input requires an interactive terminal.")
        return _windows_secret_input(prompt, language=language, on_change=on_change)
    return getpass.getpass(prompt)


def _absolute_dir(raw: str, *, language: str = "ru") -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(TEXT[language]["absolute"])
    return path.resolve()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp",
                                                  dir=path.parent)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _validate(field: str, value: str, *, language: str = "ru") -> str:
    messages = TEXT[language]
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(messages["control"])
    if field in FIELDS[:4] and not value.strip():
        raise ValueError(messages["empty"])
    if field in {"ADMIN_ID", "FUNPAY_USER_ID"}:
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError(messages["telegram_id" if field == "ADMIN_ID" else "funpay_id"])
    if field == "DEBUG" and value not in {"0", "1"}:
        raise ValueError(messages["debug"])
    return value


def configure(data_directory: Path, *, language: str = "ru", input_fn=input,
              secret_fn=None, output_fn=print, ui: InstallerConsole | None = None) -> bool:
    """Return True if a config was written; never print entered values."""
    messages = TEXT[language]
    if ui is not None:
        input_fn = ui.read_text
        secret_fn = lambda prompt: ui.read_secret(
            prompt, lambda hidden_prompt, changed: _secret_input(
                hidden_prompt, language=language, on_change=changed))
        output_fn = ui.write
    elif secret_fn is None:
        secret_fn = lambda prompt: _secret_input(prompt, language=language)
    if ui is None:
        output_fn(f"FunPayFlow v{_version()}")
    else:
        ui.landing()
    output_fn(messages["initial"])
    output_fn("")
    destination = data_directory / ".env"
    if destination.exists():
        for key in ("existing", "keep", "edit", "cancel_option"):
            output_fn(messages[key])
        while True:
            answer = input_fn(messages["choose"]).strip()
            if answer in {"", "1"}:
                output_fn(f"[OK] {messages['kept']}")
                return False
            if answer == "2":
                break
            if answer == "3":
                raise SetupCancelled
            output_fn(f"[!] {messages['choose_error']}")

    values = {}
    for field, heading, help_text, prompt in FIELD_UI[language]:
        if heading:
            output_fn("")
            output_fn(heading)
        output_fn(help_text)
        while True:
            try:
                raw = secret_fn(prompt) if field in SECRET_FIELDS else input_fn(prompt)
                values[field] = _validate(field, raw or ("0" if field == "DEBUG" else ""),
                                          language=language)
            except ValueError as error:
                output_fn(f"[ERROR] {error}")
                continue
            output_fn(f"[OK] {messages['accepted']}")
            break
    output_fn("")
    output_fn(messages["save_heading"])
    content = "".join(f"{field}={json.dumps(values[field], ensure_ascii=False)}\n"
                      for field in FIELDS)
    if ui is None:
        _atomic_text(destination, content)
    else:
        ui.run_saving(lambda: _atomic_text(destination, content))
    output_fn(f"[OK] {messages['saved']}")
    return True


def main(argv: list[str] | None = None) -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="FunPayFlow local setup")
    parser.add_argument("--data-dir", type=str)
    parser.add_argument("--choose-data-dir", action="store_true")
    parser.add_argument("--write-pointer", action="store_true")
    parser.add_argument("--language", choices=("ru", "en"))
    parser.add_argument("--dependencies-ready", action="store_true")
    args = parser.parse_args(argv)
    language = args.language or "ru"
    ui = InstallerConsole(language)
    try:
        if args.choose_data_dir:
            default = (POINTER.read_text(encoding="utf-8").strip() if POINTER.exists()
                       else str(Path.home() / "FunPayFlow"))
            chosen = input(f"{TEXT[language]['data_prompt']} [{default}]: ").strip() or default
        else:
            chosen = args.data_dir or os.environ.get(DATA_DIR_ENV) or str(CODE_DIR)
        data_directory = _absolute_dir(chosen, language=language)
        if args.language is None:
            language = load_language(data_directory)
            ui.language = language
        written = configure(data_directory, language=language, ui=ui)
        _atomic_text(data_directory / LANGUAGE_FILE, language + "\n")
        if args.write_pointer:
            _atomic_text(POINTER, str(data_directory) + "\n")
        ui.finish(data_directory, written=written,
                  dependencies_ready=args.dependencies_ready)
    except SetupCancelled:
        ui.write(f"[!] {TEXT[language]['canceled']}")
        return 2
    except (EOFError, KeyboardInterrupt):
        ui.write(f"[!] {TEXT[language]['interrupted']}")
        return 2
    except ValueError as error:
        ui.write(f"[ERROR] {error}")
        return 1
    except OSError:
        ui.write(f"[ERROR] {TEXT[language]['save_error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
