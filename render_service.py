"""Render the tracked systemd template with validated, quoted local paths."""

import argparse
import os
from pathlib import Path
import re

from runtime_paths import CODE_DIR


TEMPLATE = CODE_DIR / "systemd" / "funpayflow.service.in"
if not TEMPLATE.is_file():  # Release ZIP keeps Linux helpers outside app/.
    TEMPLATE = CODE_DIR.parent / "linux" / "systemd" / "funpayflow.service.in"


def _unit_word(value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid systemd value.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _exec_word(value: str) -> str:
    # systemd expands $NAME in ExecStart even inside quoted arguments.
    return _unit_word(value).replace("$", "$$")


def _scalar_path(value: str) -> str:
    """WorkingDirectory is one scalar path; quotes become literal characters."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid systemd path.")
    return value.replace("\\", "\\\\").replace("%", "%%")


def render(code_dir: Path, data_dir: Path, user: str) -> str:
    if not Path(code_dir).is_absolute() or not Path(data_dir).is_absolute():
        raise ValueError("Absolute paths required.")
    code, data = Path(code_dir).resolve(), Path(data_dir).resolve()
    if re.fullmatch(r"[a-z_][a-z0-9_-]*", user) is None:
        raise ValueError("Invalid service user.")
    values = {
        "@USER@": user,
        "@CODE_DIR@": _scalar_path(str(code)),
        "@ENV_FILE@": _unit_word(str(data / ".env")),
        "@DATA_VALUE@": str(data).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%"),
        "@PYTHON@": _exec_word(str(code / ".venv" / "bin" / "python")),
        "@MAIN@": _exec_word(str(code / "main.py")),
    }
    result = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        result = result.replace(key, value)
    if re.search(r"@[A-Z_]+@", result):
        raise ValueError("Unresolved service template placeholder.")
    return result


def main() -> None:
    import pwd
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    user = pwd.getpwuid(os.getuid()).pw_name
    args.output.write_text(render(args.code_dir, args.data_dir, user), encoding="utf-8")


if __name__ == "__main__":
    main()
