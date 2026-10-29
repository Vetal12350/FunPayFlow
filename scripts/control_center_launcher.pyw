"""Optional no-console Windows entry point for the read-only Control Center UI.

Do not import or launch the Codex worker. Fail visibly if the GUI cannot start.
The existing .bat launcher remains the diagnostic path for technical details.
"""

from __future__ import annotations

import sys


def show_failure() -> None:
    # A fixed message is deliberate: never show a traceback, task body, or token.
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        "Не удалось открыть Control Center.\n"
        "Для диагностики запустите scripts\\start_control_center.bat.\n"
        "Для установки интерфейса см. docs\\CONTROL_CENTER.md.",
        "Control Center — ошибка запуска",
        0x10,
    )


def main() -> int:
    try:
        import control_center

        result = control_center.main()
        if result != 0:
            show_failure()
            return 1
        return 0
    except Exception:
        show_failure()
        return 1


if __name__ == "__main__":
    sys.exit(main())
