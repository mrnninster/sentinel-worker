"""Minimal logging helpers used by vendored schedule parsers / utils."""

from __future__ import annotations

import logging
import os
import re
import traceback
from logging.config import dictConfig


class CustomFormatter(logging.Formatter):
    def format(self, record):
        self._style._fmt = "[%(levelname)s] %(filename)s: %(message)s"
        return super().format(record)


LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

LOG_LEVEL_STR = os.environ.get("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL_STR not in LEVELS:
    LOG_LEVEL_STR = "INFO"
LOG_LEVEL = LEVELS[LOG_LEVEL_STR]

dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"custom": {"()": CustomFormatter}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "level": LOG_LEVEL,
                "formatter": "custom",
            }
        },
        "root": {"handlers": ["console"], "level": LOG_LEVEL},
    }
)


def get_dedicated_debug_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    if not log.hasHandlers():
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(CustomFormatter())
        log.addHandler(handler)
    try:
        from log_buffer import attach_to_logger

        attach_to_logger(log)
    except Exception:
        pass
    return log


class CustomAsyncioWarningFilter(logging.Filter):
    def filter(self, record):
        if "asyncio" in record.name:
            try:
                actual_message = record.msg % record.args
                match = re.search(r"took (\d+(\.\d+)?) seconds", actual_message)
                if match and float(match.group(1)) < 120:
                    return False
            except Exception:
                traceback.print_exc()
        return True


logging.getLogger("asyncio").addFilter(CustomAsyncioWarningFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
