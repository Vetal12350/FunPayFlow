"""One source of truth for private runtime locations.

Without FUNPAY_BOT_DATA_DIR the historical code-directory layout is retained.
An explicit directory moves *all* runtime files together; nothing is migrated.
"""

import os
from pathlib import Path


CODE_DIR = Path(__file__).resolve().parent
DATA_DIR_ENV = "FUNPAY_BOT_DATA_DIR"


def data_dir() -> Path:
    configured = os.environ.get(DATA_DIR_ENV)
    if configured is None:
        return CODE_DIR
    if not configured.strip():
        raise ValueError(f"{DATA_DIR_ENV} must be a non-empty absolute path.")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{DATA_DIR_ENV} must be an absolute path.")
    return path.resolve()


def runtime_file(name: str) -> Path:
    if name not in {".env", "bot_settings.json", "state.sqlite3",
                    "stats_log.json", "bot.lock"}:
        raise ValueError("Unknown runtime file.")
    return data_dir() / name


def logs_dir() -> Path:
    return data_dir() / "logs"


def imports_dir() -> Path:
    return data_dir() / "imports"
