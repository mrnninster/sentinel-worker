"""Local equivalent of a Heroku one-off: run the job in a daemon thread."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def start_local_thread(
    *,
    job_id: str,
    target: Callable[..., None],
    kwargs: Optional[dict[str, Any]] = None,
) -> threading.Thread:
    thread = threading.Thread(
        target=target,
        kwargs=kwargs or {},
        name=f"job-{job_id}",
        daemon=True,
    )
    thread.start()
    log.info("Started local thread for job_id=%s name=%s", job_id, thread.name)
    return thread
