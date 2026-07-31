"""File-backed worker logs for the admin home page.

The daily rotated log file is the source of truth. The live UI / SSE stream
reads and tails that file — nothing is kept in an in-memory ring buffer, so
logs survive process reloads.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Optional

# Default live lookback window when callers omit ``since``.
RETENTION_SECONDS = 6 * 60 * 60
# On-disk retention / download window: today + previous 6 calendar days.
DOWNLOAD_DAYS = 7

_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"\[(?P<logger>[^\]]+)\]\s+"
    r"(?P<message>.*)$"
)

_lock = threading.RLock()
_cv = threading.Condition(_lock)
_file_handler: Optional[TimedRotatingFileHandler] = None
_notify_handler: Optional["NotifyHandler"] = None
_installed = False
# Monotonic write generation — wakes SSE waiters without storing messages.
_generation = 0


@dataclass(frozen=True)
class LogEntry:
    ts: float
    level: str
    logger: str
    message: str
    pathname: str = ""
    lineno: int = 0
    seq: int = 0  # byte offset of the line start in its source file (stable cursor)

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["iso"] = self.iso
        return d


def log_dir() -> Path:
    raw = (os.environ.get("WORKER_LOG_DIR") or "").strip()
    if raw:
        path = Path(raw)
    else:
        path = Path(__file__).resolve().parent / "data" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file_path() -> Path:
    return log_dir() / "worker.log"


def _flush_file_handler() -> None:
    if _file_handler is not None:
        try:
            _file_handler.flush()
        except Exception:
            pass


def _parse_ts(text: str) -> float | None:
    now = time.time()
    local_tz = datetime.now().astimezone().tzinfo
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        utc_ts = naive.replace(tzinfo=timezone.utc).timestamp()
        # New writes are UTC. Older local-time lines can look "in the future"
        # when misread as UTC (e.g. UTC+1 hosts) — fall back to local tz.
        if utc_ts - now > 1800:
            return naive.replace(tzinfo=local_tz).timestamp()
        return utc_ts
    return None


def parse_log_line(line: str, *, seq: int = 0) -> LogEntry | None:
    raw = line.rstrip("\n")
    if not raw.strip():
        return None
    match = _LOG_LINE_RE.match(raw)
    if not match:
        return None
    ts = _parse_ts(match.group("ts"))
    if ts is None:
        return None
    return LogEntry(
        ts=ts,
        level=match.group("level"),
        logger=match.group("logger"),
        message=match.group("message"),
        seq=seq,
    )


def _entry_matches(
    entry: LogEntry,
    *,
    q: str | None = None,
    level: str | None = None,
) -> bool:
    needle = (q or "").strip().lower()
    level_name = (level or "").strip().upper()
    level_no = getattr(logging, level_name, None) if level_name else None
    if level_no is not None:
        entry_no = getattr(logging, entry.level, 0)
        if entry_no < level_no:
            return False
    if needle:
        hay = f"{entry.iso} {entry.level} {entry.logger} {entry.message}".lower()
        if needle not in hay:
            return False
    return True


def _dated_log_files(days: int = DOWNLOAD_DAYS) -> list[Path]:
    """
    TimedRotatingFileHandler names:
      worker.log              (current day)
      worker.log.YYYY-MM-DD   (rotated prior days)
    """
    base = log_file_path()
    today = datetime.now(timezone.utc).date()
    wanted = {today - timedelta(days=i) for i in range(max(1, days))}
    found: dict[date, Path] = {}

    if base.exists():
        found[today] = base

    prefix = base.name + "."
    for path in sorted(base.parent.glob(base.name + ".*")):
        suffix = path.name[len(prefix) :]
        try:
            day = datetime.strptime(suffix, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day in wanted:
            found[day] = path

    return [found[day] for day in sorted(found)]


def _read_file_entries(path: Path) -> list[LogEntry]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    entries: list[LogEntry] = []
    offset = 0
    text = data.decode("utf-8", errors="replace")
    # Walk with byte offsets from the original bytes where possible.
    for line in text.splitlines(keepends=True):
        line_bytes = line.encode("utf-8", errors="replace")
        body = line.rstrip("\r\n")
        entry = parse_log_line(body, seq=offset)
        if entry is not None:
            entries.append(entry)
        offset += len(line_bytes)
    return entries


def current_file_size() -> int:
    path = log_file_path()
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def read_new_entries_from_offset(
    from_offset: int,
    *,
    q: str | None = None,
    level: str | None = None,
) -> tuple[list[LogEntry], int]:
    """
    Read complete new lines from ``worker.log`` starting at ``from_offset``.

    Returns (matching_entries, new_offset). ``new_offset`` always advances past
    complete lines even when filters drop them.
    """
    _flush_file_handler()
    path = log_file_path()
    if not path.exists():
        return [], 0

    try:
        size = path.stat().st_size
    except OSError:
        return [], from_offset

    # Rotation / truncate: start over at the beginning of the new file.
    offset = 0 if from_offset > size else max(0, from_offset)

    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return [], offset

    if not raw:
        return [], offset

    text = raw.decode("utf-8", errors="replace")
    # Keep a partial trailing line for the next read.
    if text.endswith("\n"):
        complete, remainder = text, ""
    else:
        idx = text.rfind("\n")
        if idx == -1:
            return [], offset
        complete, remainder = text[: idx + 1], text[idx + 1 :]

    matched: list[LogEntry] = []
    cursor = offset
    for line in complete.splitlines(keepends=True):
        line_bytes = line.encode("utf-8", errors="replace")
        entry = parse_log_line(line.rstrip("\r\n"), seq=cursor)
        cursor += len(line_bytes)
        if entry is not None and _entry_matches(entry, q=q, level=level):
            matched.append(entry)

    # Do not advance past the incomplete remainder.
    return matched, cursor


def wait_for_entries_after(
    after_offset: int,
    *,
    timeout: float = 1.5,
    q: str | None = None,
    level: str | None = None,
) -> tuple[list[LogEntry], int]:
    """
    Block until ``worker.log`` grows past ``after_offset``, or timeout.

    Cursor is a byte offset into the current log file (not an in-memory seq).
    """
    deadline = time.time() + max(0.05, float(timeout))
    start_gen = _generation
    while True:
        matched, new_offset = read_new_entries_from_offset(
            after_offset, q=q, level=level
        )
        if matched or new_offset > after_offset:
            # Advance cursor even when filters matched nothing.
            return matched, new_offset

        remaining = deadline - time.time()
        if remaining <= 0:
            return [], after_offset

        with _cv:
            if _generation != start_gen:
                start_gen = _generation
                continue
            _cv.wait(timeout=min(remaining, 0.5))


def query_logs(
    *,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    level: str | None = None,
    limit: int = 2000,
) -> list[LogEntry]:
    """Read matching lines from on-disk daily logs (survives reloads)."""
    _flush_file_handler()
    now = time.time()
    # Allow a small skew so last-second lines are not clipped.
    until_ts = (until if until is not None else now) + 2.0
    since_ts = since if since is not None else (now - RETENTION_SECONDS)

    lookback_days = max(
        1,
        min(
            DOWNLOAD_DAYS,
            int(max(0.0, now - since_ts) // 86400) + 2,
        ),
    )
    matched: list[LogEntry] = []
    for path in _dated_log_files(days=lookback_days):
        for entry in _read_file_entries(path):
            if entry.ts < since_ts or entry.ts > until_ts:
                continue
            if not _entry_matches(entry, q=q, level=level):
                continue
            matched.append(entry)

    matched.sort(key=lambda e: (e.ts, e.seq))
    if limit > 0 and len(matched) > limit:
        matched = matched[-limit:]
    return matched


def stats() -> dict[str, Any]:
    _flush_file_handler()
    path = log_file_path()
    size = current_file_size()
    files = _dated_log_files(DOWNLOAD_DAYS)
    return {
        "count": None,  # not counted in memory; UI uses on-screen + disk_bytes
        "seq": size,  # stream cursor tip = current file size
        "disk_bytes": size,
        "files": len(files),
        "source": "file",
        "retention_hours": RETENTION_SECONDS / 3600,
        "download_days": DOWNLOAD_DAYS,
        "log_dir": str(log_dir()),
        "log_file": str(path),
        "oldest": None,
        "newest": None,
        "generation": _generation,
    }


def export_logs_text(
    *,
    days: int = DOWNLOAD_DAYS,
    q: str | None = None,
    level: str | None = None,
) -> tuple[str, list[str]]:
    """Concatenate on-disk daily logs for the last ``days`` calendar days."""
    _flush_file_handler()
    needle = (q or "").strip().lower()
    level_name = (level or "").strip().upper()
    files = _dated_log_files(days=days)
    chunks: list[str] = []
    used: list[str] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        used.append(path.name)
        if not needle and not level_name:
            chunks.append(text if text.endswith("\n") or not text else text + "\n")
            continue
        filtered_lines: list[str] = []
        for line in text.splitlines():
            entry = parse_log_line(line)
            if entry is None:
                if needle and needle in line.lower():
                    filtered_lines.append(line)
                continue
            if _entry_matches(entry, q=q, level=level):
                filtered_lines.append(line)
        if filtered_lines:
            chunks.append("\n".join(filtered_lines) + "\n")

    header = (
        f"# worker logs export days={days} "
        f"from={files[0].name if files else 'none'} "
        f"to={files[-1].name if files else 'none'} "
        f"generated={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )
    return header + "".join(chunks), used


class NotifyHandler(logging.Handler):
    """Wake SSE waiters when a log record is written (stores nothing)."""

    def emit(self, record: logging.LogRecord) -> None:
        global _generation
        try:
            with _cv:
                _generation += 1
                _cv.notify_all()
        except Exception:
            self.handleError(record)


def get_file_handler() -> TimedRotatingFileHandler:
    global _file_handler
    with _cv:
        if _file_handler is None:
            path = log_file_path()
            handler = TimedRotatingFileHandler(
                filename=str(path),
                when="midnight",
                interval=1,
                backupCount=DOWNLOAD_DAYS - 1,
                encoding="utf-8",
                utc=True,
            )
            handler.suffix = "%Y-%m-%d"
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"
            )
            formatter.converter = time.gmtime  # UTC in the file for stable parsing
            handler.setFormatter(formatter)
            _file_handler = handler
        return _file_handler


def get_notify_handler() -> NotifyHandler:
    global _notify_handler
    with _cv:
        if _notify_handler is None:
            _notify_handler = NotifyHandler(level=logging.DEBUG)
        return _notify_handler


def install_log_buffer() -> TimedRotatingFileHandler:
    """Attach file + notify handlers to the root logger (idempotent)."""
    global _installed
    file_handler = get_file_handler()
    if file_handler.formatter is not None:
        file_handler.formatter.converter = time.gmtime
    notify = get_notify_handler()
    with _cv:
        if _installed:
            return file_handler
        root = logging.getLogger()
        if file_handler not in root.handlers:
            root.addHandler(file_handler)
        if notify not in root.handlers:
            root.addHandler(notify)
        _installed = True
    return file_handler


def attach_to_logger(logger: logging.Logger) -> None:
    """Attach file + notify handlers to a non-propagating dedicated logger."""
    file_handler = get_file_handler()
    notify = get_notify_handler()
    if file_handler not in logger.handlers:
        logger.addHandler(file_handler)
    if notify not in logger.handlers:
        logger.addHandler(notify)
