"""Local first-run configuration; never validates credentials over the network."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from runtime_paths import CODE_DIR, DATA_DIR_ENV


FIELDS = ("FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "ADMIN_ID", "FUNPAY_USER_ID",
          "BOT_PASSWORD", "DEBUG")
SECRET_FIELDS = frozenset({"FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "BOT_PASSWORD"})
POINTER = CODE_DIR / ".install-data-dir"


def _absolute_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Каталог данных должен быть абсолютным путём.")
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


def _validate(field: str, value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field}: недопустимые управляющие символы.")
    if field in FIELDS[:4] and not value.strip():
        raise ValueError(f"{field}: обязательное значение пусто.")
    if field in {"ADMIN_ID", "FUNPAY_USER_ID"}:
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError(f"{field}: нужен положительный числовой ID.")
    if field == "DEBUG" and value not in {"0", "1"}:
        raise ValueError("DEBUG: введите 0 или 1.")
    return value


def configure(data_directory: Path, *, input_fn=input, secret_fn=getpass.getpass,
              output_fn=print) -> bool:
    """Return True if a config was written; never print entered values."""
    destination = data_directory / ".env"
    if destination.exists():
        answer = input_fn("Конфигурация уже есть. Изменить её? [y/N]: ")
        if answer.strip().lower() not in {"y", "yes"}:
            output_fn("Существующая конфигурация сохранена без изменений.")
            return False
    values = {}
    for field in FIELDS:
        prompt = f"{field}{' (необязательно)' if field in FIELDS[4:] else ''}: "
        raw = secret_fn(prompt) if field in SECRET_FIELDS else input_fn(prompt)
        values[field] = _validate(field, raw or ("0" if field == "DEBUG" else ""))
    content = "".join(f"{field}={json.dumps(values[field], ensure_ascii=False)}\n"
                      for field in FIELDS)
    _atomic_text(destination, content)
    output_fn("Конфигурация сохранена. Значения ключей не выводятся.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальная конфигурация FunPay Seller Bot")
    parser.add_argument("--data-dir", type=str)
    parser.add_argument("--choose-data-dir", action="store_true")
    parser.add_argument("--write-pointer", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.choose_data_dir:
            default = (POINTER.read_text(encoding="utf-8").strip() if POINTER.exists()
                       else str(Path.home() / "FunPaySellerBot"))
            chosen = input(f"Каталог приватных данных [{default}]: ").strip() or default
        else:
            chosen = args.data_dir or os.environ.get(DATA_DIR_ENV) or str(CODE_DIR)
        data_directory = _absolute_dir(chosen)
        configure(data_directory)
        if args.write_pointer:
            _atomic_text(POINTER, str(data_directory) + "\n")
    except (EOFError, KeyboardInterrupt):
        print("Настройка прервана; конфигурация не изменена.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Ошибка настройки: {type(error).__name__}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
