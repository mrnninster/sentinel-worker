"""Cooperative shutdown for job threads plus reaping of leaked child processes.

Local dispatch runs jobs as daemon threads inside the web process, and those
jobs start real OS children (the Playwright node driver and Chromium). Daemon
threads are killed at interpreter exit without unwinding, so `browser.close()`
never runs and the browsers are reparented to init. Long monitor loops also
sleep for up to `max_duration_seconds`, which would keep a shutdown waiting.

Jobs poll `is_shutting_down()` / `sleep_unless_shutdown()` so they can stop at
the next safe point, and `terminate_children()` kills whatever survived.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

log = logging.getLogger(__name__)

_shutdown = threading.Event()


def request_shutdown() -> None:
    _shutdown.set()


def clear_shutdown() -> None:
    """Test/reload helper: allow a fresh run in the same interpreter."""
    _shutdown.clear()


def is_shutting_down() -> bool:
    return _shutdown.is_set()


def sleep_unless_shutdown(seconds: float) -> bool:
    """Sleep up to `seconds`. Returns False when shutdown was requested."""
    return not _shutdown.wait(timeout=max(0.0, seconds))


def _descendant_pids(root: int) -> list[int]:
    """Descendants of `root`, deepest last. Linux /proc; empty elsewhere."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        # "pid (comm) state ppid ..." — comm can contain spaces and parens.
        close_paren = raw.rfind(")")
        if close_paren < 0:
            continue
        fields = raw[close_paren + 2 :].split()
        if len(fields) < 2:
            continue
        try:
            parent = int(fields[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(int(entry))

    ordered: list[int] = []
    frontier = [root]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            ordered.append(child)
            frontier.append(child)
    return ordered


def terminate_children(*, grace: float = 5.0) -> int:
    """SIGTERM then SIGKILL every surviving descendant. Returns count signalled."""
    pids = _descendant_pids(os.getpid())
    if not pids:
        return 0

    log.info("Shutdown: terminating %s child process(es) %s", len(pids), pids)
    # Deepest first so a parent cannot respawn or re-adopt while we work.
    for pid in reversed(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            log.info("Shutdown: all child processes exited on SIGTERM")
            return len(pids)
        time.sleep(0.1)

    for pid in reversed(pids):
        if _alive(pid):
            log.warning("Shutdown: SIGKILL child pid=%s (ignored SIGTERM)", pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return len(pids)


def _reap() -> None:
    """Collect exited direct children so they do not linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid == 0:
            return


def _alive(pid: int) -> bool:
    """True only for a process still executing; a zombie counts as exited."""
    _reap()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return False
    close_paren = raw.rfind(")")
    if close_paren < 0:
        return True
    fields = raw[close_paren + 2 :].split()
    return bool(fields) and fields[0] != "Z"
