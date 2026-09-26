"""One-process restart request and late gates for automatic HTTP actions."""

import asyncio
import threading
from contextlib import contextmanager
from enum import Enum, auto


class RuntimeAction(Enum):
    NONE = auto()
    RESTART = auto()


_action_lock = threading.RLock()
_restart_requested = threading.Event()
_restart_signal: asyncio.Event | None = None


def begin_runtime_cycle() -> None:
    """Reset only after the previous runtime has fully stopped."""
    global _restart_signal
    with _action_lock:
        _restart_requested.clear()
        _restart_signal = asyncio.Event()


def restart_requested() -> bool:
    return _restart_requested.is_set()


def claim_restart() -> bool:
    """Wait for already-started automatic HTTP actions, then claim once."""
    with _action_lock:
        if _restart_requested.is_set():
            return False
        _restart_requested.set()
        return True


def signal_restart() -> None:
    if _restart_signal is None or not _restart_requested.is_set():
        raise RuntimeError("Restart was not claimed.")
    _restart_signal.set()


async def wait_for_restart() -> None:
    if _restart_signal is None:
        raise RuntimeError("Runtime cycle was not started.")
    await _restart_signal.wait()


@contextmanager
def automatic_action_gate():
    """Hold the claim boundary through an in-flight automatic HTTP action."""
    with _action_lock:
        yield not _restart_requested.is_set()
