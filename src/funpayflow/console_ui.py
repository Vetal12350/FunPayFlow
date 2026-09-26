"""Presentation for the public installer and Windows launcher only.

No credentials are read here. The setup and runtime modules own all state and
network behavior; this module renders their local status in RU or EN.
"""

import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Callable, TextIO, TypeVar

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.text import Text
except ImportError:  # Manual setup before dependency installation stays readable.
    Console = None


_RESULT = TypeVar("_RESULT")
_STEP = re.compile(r"^\[(\d)/4\] (.+)$")
_COPY = {
    "ru": {
        "tagline": "Автоматизация • Аналитика • Управление",
        "seller_line": "для продавцов FunPay",
        "welcome": "ДОБРО ПОЖАЛОВАТЬ",
        "welcome_detail": "Настройка займёт несколько минут",
        "step": "ШАГ {number} ИЗ 4",
        "hidden": "Ctrl+V поддерживается · значение скрыто",
        "saving": "Сохранение конфигурации...",
        "ready": "Настройка завершена",
        "saved": "Конфигурация сохранена.",
        "kept": "Текущая конфигурация сохранена без изменений.",
        "data_ready": "Каталог данных готов.",
        "deps_ready": "Зависимости установлены.",
        "data_dir": "Каталог данных:",
        "next": "Следующий шаг: запустите Start.bat",
        "config_found": "Конфигурация найдена.",
        "starting": "Запуск бота...",
        "missing_config": "Конфигурация не найдена. Запустите Setup.bat.",
        "lock": "Другой экземпляр FunPayFlow уже запущен.\nЗакройте существующий экземпляр перед повторным запуском.",
        "runtime_error": "Бот остановлен с ошибкой. Проверьте личный каталог логов.",
    },
    "en": {
        "tagline": "Automation • Analytics • Control",
        "seller_line": "for FunPay sellers",
        "welcome": "WELCOME",
        "welcome_detail": "Setup takes a few minutes",
        "step": "STEP {number} OF 4",
        "hidden": "Ctrl+V supported · value hidden",
        "saving": "Saving configuration...",
        "ready": "Setup completed",
        "saved": "Configuration saved.",
        "kept": "Current configuration kept unchanged.",
        "data_ready": "Data directory ready.",
        "deps_ready": "Dependencies installed.",
        "data_dir": "Data directory:",
        "next": "Next step: Run Start.bat",
        "config_found": "Configuration found.",
        "starting": "Starting bot...",
        "missing_config": "Private configuration is missing. Run Setup.bat first.",
        "lock": "Another FunPayFlow instance is already running.\nClose the existing instance before starting another one.",
        "runtime_error": "Bot stopped with an error. Check the private logs folder.",
    },
}


def project_version(code_dir: Path | None = None) -> str:
    """Read the authoritative project version; never maintain a UI copy."""
    root = code_dir or Path(__file__).resolve().parents[2]
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            return str(tomllib.load(stream)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def use_utf8_console() -> None:
    """Match the UTF-8 code page set by the Windows batch entry points."""
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")


def _secret_mask(has_value: bool) -> str:
    """A fixed mask avoids revealing the entered secret's length."""
    return "●" * 12 if has_value else ""


class InstallerConsole:
    """Rich when the terminal supports it, compact plain text otherwise."""

    def __init__(self, language: str = "ru", *, stream: TextIO | None = None,
                 width: int | None = None, force_rich: bool | None = None) -> None:
        self.language = language if language in _COPY else "ru"
        self.stream = stream or sys.stdout
        self.width = width if width is not None else shutil.get_terminal_size((80, 24)).columns
        terminal = self.stream.isatty() if force_rich is None else force_rich
        self.rich = bool(Console is not None and terminal and self.width >= 42
                         and os.environ.get("NO_COLOR") is None
                         and os.environ.get("TERM") != "dumb")
        self.console = (Console(file=self.stream, width=min(self.width, 76), safe_box=True,
                                force_terminal=True if force_rich is True else None,
                                highlight=False)
                        if self.rich else None)
        if self.console is not None and not self.console.is_terminal:
            self.console = None
            self.rich = False

    @property
    def copy(self) -> dict[str, str]:
        return _COPY[self.language]

    def _plain(self, value: str = "") -> None:
        print(value, file=self.stream)

    def banner(self, version: str | None = None) -> None:
        name = f"FunPayFlow v{version or project_version()}"
        if not self.rich:
            if self.width >= 38:
                self._plain("+------------------------------------+")
                self._plain("| FUNPAYFLOW                         |")
                self._plain(f"| {name:<34} |")
                self._plain("+------------------------------------+")
            else:
                self._plain(name)
            self._plain(self.copy["tagline"])
            self._plain(self.copy["seller_line"])
            return
        content = Group(
            Align.center(Text("FUNPAYFLOW", style="bold bright_cyan")),
            Align.center(Text(f"v{version or project_version()}", style="bold white")),
            Align.center(Text(self.copy["tagline"], style="bright_magenta")),
            Align.center(Text(self.copy["seller_line"], style="white")),
        )
        self.console.print(Panel(content, box=box.ROUNDED, border_style="bright_cyan",
                                 width=min(self.width - 2, 62), padding=(1, 2)))

    def landing(self) -> None:
        """First Python setup screen; language was chosen by the batch bootstrap."""
        version = project_version()
        copy = self.copy
        if not self.rich:
            self._plain(f"FunPayFlow v{version}")
            self._plain(copy["tagline"])
            self._plain(copy["seller_line"])
            self._plain(copy["welcome"])
            self._plain(copy["welcome_detail"])
            self._plain("[1] Русский" + (" ✓" if self.language == "ru" else ""))
            self._plain("[2] English" + (" ✓" if self.language == "en" else ""))
            return
        language_options = Group(
            Align.center(Text("[1] Русский" + ("  ✓" if self.language == "ru" else ""),
                              style="bold green" if self.language == "ru" else "bright_cyan")),
            Align.center(Text("[2] English" + ("  ✓" if self.language == "en" else ""),
                              style="bold green" if self.language == "en" else "bright_cyan")),
        )
        content = Group(
            Align.center(Text("FUNPAYFLOW", style="bold bright_cyan")),
            Align.center(Text(f"v{version}", style="bold white")),
            Align.center(Text(copy["tagline"], style="bright_magenta")),
            Align.center(Text(copy["seller_line"], style="white")),
            Text(""),
            Align.center(Text(copy["welcome"], style="bold green")),
            Align.center(Text(copy["welcome_detail"], style="white")),
            Text(""),
            language_options,
        )
        self.console.print(Panel(content, box=box.ROUNDED, border_style="bright_cyan",
                                 width=min(self.width - 2, 62), padding=(1, 2)))

    def step(self, number: int, heading: str) -> None:
        label = self.copy["step"].format(number=number)
        percent = number * 25
        if not self.rich:
            self._plain(f"{label}  ·  {percent}%")
            self._plain(heading)
            self._plain(f"[{'#' * number}{'.' * (4 - number)}] {percent}%")
            return
        progress = ProgressBar(total=4, completed=number,
                               width=min(28, max(10, self.width - 25)),
                               complete_style="bright_magenta", style="grey37")
        content = Group(Text(label, style="bold bright_magenta"),
                        Text(heading, style="bold bright_cyan"),
                        progress, Text(f"{percent}%", style="dim"))
        self.console.print(Panel(content, box=box.ROUNDED, border_style="bright_cyan",
                                 width=min(self.width - 2, 62)))

    def write(self, message: str) -> None:
        if message.startswith("FunPayFlow v"):
            self.banner(message.removeprefix("FunPayFlow v"))
            return
        match = _STEP.match(message)
        if match:
            self.step(int(match.group(1)), match.group(2))
            return
        if not self.rich:
            self._plain(message)
            return
        if message.startswith("[ERROR]"):
            self.console.print(Panel(Text(message, style="bold red"), border_style="red",
                                     box=box.ROUNDED, width=min(self.width - 2, 62)))
        elif message.startswith("[!]"):
            self.console.print(Panel(Text(message, style="yellow"), border_style="yellow",
                                     box=box.ROUNDED, width=min(self.width - 2, 62)))
        elif message.startswith("[OK]"):
            self.console.print(Text(message, style="bold green"))
        elif message.startswith("[") and "]" in message[:4]:
            self.console.print(Text(message, style="bright_magenta"))
        else:
            self.console.print(Text(message, style="dim"))

    def read_text(self, prompt: str) -> str:
        if not self.rich:
            return input(prompt)
        self.console.print(Text(prompt.rstrip(": "), style="bold bright_cyan"))
        return input("› ")

    def _secret_title_renderable(self, prompt: str) -> "Text":
        return Text(prompt.rstrip(": "), style="bold bright_cyan")

    def _secret_field_renderable(self, has_value: bool) -> "Panel":
        return Panel(Text("› " + _secret_mask(has_value), style="bright_magenta"),
                     border_style="bright_magenta", box=box.ROUNDED,
                     width=min(self.width - 2, 52), padding=(0, 1))

    def _secret_help_renderable(self) -> "Text":
        return Text(self.copy["hidden"], style="dim")

    def read_secret(self, prompt: str,
                    reader: Callable[[str, Callable[[bool], None] | None], str]) -> str:
        if not self.rich or os.name != "nt":
            return reader(prompt, None)
        self.console.print(self._secret_title_renderable(prompt))
        with Live(self._secret_field_renderable(False), console=self.console, auto_refresh=False,
                  redirect_stdout=False, redirect_stderr=False) as live:
            def changed(has_value: bool) -> None:
                live.update(self._secret_field_renderable(has_value), refresh=True)

            value = reader("", changed)
            changed(bool(value))
        self.console.print(self._secret_help_renderable())
        return value

    def run_saving(self, action: Callable[[], _RESULT]) -> _RESULT:
        if not self.rich:
            return action()
        with self.console.status(self.copy["saving"], spinner="dots",
                                 spinner_style="bright_magenta"):
            return action()

    def finish(self, data_directory: Path, *, written: bool,
               dependencies_ready: bool = False) -> None:
        copy = self.copy
        lines = [f"[OK] {copy['ready']}",
                 f"[OK] {copy['saved'] if written else copy['kept']}",
                 f"[OK] {copy['data_ready']}"]
        if dependencies_ready:
            lines.append(f"[OK] {copy['deps_ready']}")
        lines.extend((copy["data_dir"], str(data_directory), copy["next"]))
        if not self.rich:
            self._plain()
            for line in lines:
                self._plain(line)
            return
        body = Group(*(Text(line, style=("bold green" if line.startswith("[OK]") else "white"))
                       for line in lines))
        self.console.print(Panel(body, title=Text(copy["ready"], style="bold green"),
                                 border_style="bright_cyan", box=box.ROUNDED,
                                 width=min(self.width - 2, 66), padding=(1, 2)))

    def start_ready(self, data_directory: Path) -> None:
        self.banner()
        self.write(f"[OK] {self.copy['config_found']}")
        self.write(f"[OK] {self.copy['data_ready']}")
        self.write(f"{self.copy['data_dir']} {data_directory}")
        self.write(self.copy["starting"])

    def start_error(self, kind: str, *, show_banner: bool = True) -> None:
        if show_banner:
            self.banner()
        self.write(f"[ERROR] {self.copy[kind]}")


def main(argv: list[str] | None = None) -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="Public launch presentation")
    parser.add_argument("action", choices=("start-ready", "start-error"))
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--kind", choices=("missing_config", "lock", "runtime_error"))
    parser.add_argument("--no-banner", action="store_true")
    args = parser.parse_args(argv)
    ui = InstallerConsole(args.language)
    if args.action == "start-ready":
        if args.data_dir is None:
            parser.error("--data-dir is required")
        ui.start_ready(args.data_dir)
    else:
        if args.kind is None:
            parser.error("--kind is required")
        ui.start_error(args.kind, show_banner=not args.no_banner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
