"""Enqueue jobs and spawn local threads or Heroku one-offs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from dispatch.heroku_oneoff import create_oneoff_dyno, heroku_configured, kill_oneoff_dyno
from dispatch.lifecycle import is_shutting_down
from dispatch.local_thread import start_local_thread
from dispatch.pool import QueuedJob, pool

log = logging.getLogger(__name__)

# Sliding window of spawn start times (monotonic) for SPAWN_RATE_* pacing.
_spawn_times: deque[float] = deque()
_deferred_drain_task: asyncio.Task | None = None


def _spawn_rate_max() -> int:
    """Max spawns per window. 0 = unlimited (legacy behavior)."""
    raw = (os.environ.get("SPAWN_RATE_MAX") or "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _spawn_rate_window() -> float:
    raw = (os.environ.get("SPAWN_RATE_WINDOW_SECONDS") or "15").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 15.0


def _prune_spawn_window(now: float) -> None:
    window = _spawn_rate_window()
    while _spawn_times and now - _spawn_times[0] >= window:
        _spawn_times.popleft()


def _spawns_allowed_now() -> int:
    """How many new jobs may start immediately under the rate limit."""
    max_n = _spawn_rate_max()
    if max_n <= 0:
        return 10**9
    now = time.monotonic()
    _prune_spawn_window(now)
    return max(0, max_n - len(_spawn_times))


def _seconds_until_spawn_slot() -> float:
    max_n = _spawn_rate_max()
    if max_n <= 0 or not _spawn_times:
        return 0.0
    now = time.monotonic()
    _prune_spawn_window(now)
    if len(_spawn_times) < max_n:
        return 0.0
    return max(0.05, _spawn_rate_window() - (now - _spawn_times[0]))


def _queue_has_work() -> bool:
    return any(pool.queues.values())


def _schedule_deferred_drain(*, delay: float | None = None) -> None:
    """Wake drain_pool again after rate window / capacity frees a slot."""
    global _deferred_drain_task
    if not _queue_has_work() or is_shutting_down():
        return
    if _deferred_drain_task and not _deferred_drain_task.done():
        return

    if delay is None:
        if _spawn_rate_max() > 0 and _spawns_allowed_now() <= 0:
            delay = _seconds_until_spawn_slot()
        else:
            delay = 2.0
    delay = max(0.05, float(delay))

    async def _run() -> None:
        try:
            await asyncio.sleep(delay)
            if not is_shutting_down():
                await drain_pool()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Deferred drain failed")

    _deferred_drain_task = asyncio.create_task(_run(), name="spawn-rate-drain")


def _dispatch_mode() -> str:
    mode = (os.environ.get("DISPATCH_MODE") or "auto").strip().lower()
    if mode in {"local", "heroku", "auto"}:
        return mode
    return "auto"


def use_heroku() -> bool:
    mode = _dispatch_mode()
    if mode == "local":
        return False
    if mode == "heroku":
        return True
    return heroku_configured()


def _public_base_url() -> str:
    return (
        os.environ.get("WORKER_PUBLIC_URL")
        or os.environ.get("PUBLIC_BASE_URL")
        or ""
    ).rstrip("/")


def _internal_token() -> str:
    return (
        os.environ.get("INTERNAL_CALLBACK_TOKEN")
        or os.environ.get("WORKER_SHARED_TOKEN")
        or os.environ.get("WORKER_TOKEN")
        or "dev-internal"
    ).strip()


def build_job_env(job: QueuedJob) -> dict[str, str]:
    """Everything the one-off/thread needs — no reliance on large shared config."""
    base = _public_base_url() or "http://127.0.0.1:8100"
    callback = f"{base}/v1/internal/jobs/{job.job_id}/result"
    # Prefer configured root; fall back to this package dir (not /app) for local.
    default_root = str(Path(__file__).resolve().parent.parent)
    scraper_root = (os.environ.get("SCRAPER_ROOT") or default_root).strip()
    pw_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()

    env: dict[str, str] = {
        "ARG_JOB_TYPE": job.load_type,
        "ARG_JOB_ID": job.job_id,
        "ARG_JOB_JSON": json.dumps(job.payload, default=str),
        "ARG_CALLBACK_URL": callback,
        "ARG_CALLBACK_TOKEN": _internal_token(),
        "ARG_WORKER_ID": (os.environ.get("WORKER_ID") or "scrape-worker-1").strip(),
        "SCRAPER_MODE": "embedded",
        "SCRAPER_ROOT": scraper_root,
        "YOUTUBE_MAX_LIVE_AGE_HOURS": (
            os.environ.get("YOUTUBE_MAX_LIVE_AGE_HOURS") or "24"
        ).strip(),
        "LOG_LEVEL": (os.environ.get("LOG_LEVEL") or "info").strip(),
        "PYTHONUNBUFFERED": "1",
    }
    worker_token = (
        os.environ.get("WORKER_SHARED_TOKEN")
        or os.environ.get("WORKER_TOKEN")
        or ""
    ).strip()
    if worker_token:
        env["WORKER_SHARED_TOKEN"] = worker_token
    if pw_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = pw_path
    openai = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if openai:
        env["OPENAI_API_KEY"] = openai
    transcript_api_key = (
        os.environ.get("TRANSCRIPTAPI_API_KEY")
        or os.environ.get("TRANSCRIPTAPI_KEY")
        or ""
    ).strip()
    if transcript_api_key:
        env["TRANSCRIPTAPI_API_KEY"] = transcript_api_key
    return env


def _run_job_in_thread(env: dict[str, str]) -> None:
    """Thread target: apply ARG_* into os.environ for this thread's job run."""
    # Isolate: copy env into process for this run (same process as middleman locally).
    # Jobs.runner reads os.environ — set temporarily per-thread is not fully isolated
    # in CPython for os.environ, so pass env dict directly.
    from jobs.runner import run_job_from_env

    run_job_from_env(env)


async def spawn_job(job: QueuedJob) -> Any:
    """Start execution; returns handle (thread or dyno dict). Raises on hard failure."""
    env = build_job_env(job)
    if use_heroku():
        dyno = await create_oneoff_dyno(
            command="cd scrape_worker && python -m jobs.runner",
            env=env,
        )
        if not dyno:
            raise RuntimeError("Failed to start Heroku one-off dyno (capacity or API)")
        return dyno

    thread = start_local_thread(
        job_id=job.job_id,
        target=_run_job_in_thread,
        kwargs={"env": env},
    )
    return thread


async def drain_pool() -> int:
    """Start queued jobs up to capacity and SPAWN_RATE_* pacing.

    Returns number started. When rate-limited with work still queued, schedules
    another drain when the next window slot opens.
    """
    # A late job callback can arrive while lifespan shutdown is killing dynos.
    # Never let its release-and-drain path start replacement work.
    if is_shutting_down():
        return 0
    started = 0
    allowed = _spawns_allowed_now()
    while allowed > 0:
        job = pool.pop_next_runnable()
        if not job:
            break
        try:
            handle = await spawn_job(job)
        except Exception:
            log.exception("Spawn failed job_id=%s — re-queue", job.job_id)
            # Put back at front of its queue
            pool.queues[job.load_type].appendleft(job)
            pool.queued_ids[job.job_id] = job.load_type
            break
        pool.mark_running(job, handle=handle)
        _spawn_times.append(time.monotonic())
        started += 1
        allowed -= 1
        log.info(
            "Dispatched job_id=%s type=%s mode=%s",
            job.job_id,
            job.load_type,
            "heroku" if use_heroku() else "local",
        )
    if _queue_has_work():
        if _spawn_rate_max() > 0 and _spawns_allowed_now() <= 0:
            wait = _seconds_until_spawn_slot()
            log.info(
                "Spawn rate limit reached (max=%s / %ss); %s queued — next drain in %.1fs",
                _spawn_rate_max(),
                _spawn_rate_window(),
                sum(len(q) for q in pool.queues.values()),
                wait,
            )
            _schedule_deferred_drain(delay=wait)
        else:
            # Still queued but capacity/can_start blocked — retry after slots free.
            _schedule_deferred_drain(delay=2.0)
    return started


def cancel_deferred_drain() -> None:
    global _deferred_drain_task
    if _deferred_drain_task and not _deferred_drain_task.done():
        _deferred_drain_task.cancel()
    _deferred_drain_task = None


async def enqueue_and_drain(job: QueuedJob) -> dict[str, Any]:
    """Enqueue then try to dispatch. Returns accept metadata."""
    if not pool.enqueue(job):
        return {
            "ok": True,
            "accepted": False,
            "reason": "already_running_or_queued",
            "job_id": job.job_id,
            "load_type": job.load_type,
        }
    started = await drain_pool()
    snap = pool.snapshot()
    return {
        "ok": True,
        "accepted": True,
        "job_id": job.job_id,
        "load_type": job.load_type,
        "dispatched_now": started > 0 and job.job_id in pool.running,
        "oneoff_running": snap["oneoff_running"],
        "queued_by_type": snap["queued_by_type"],
    }


async def release_and_drain(job_id: str, *, kill_dyno: bool = False) -> None:
    released = pool.release(job_id)
    if kill_dyno and released and use_heroku():
        handle = released.handle
        if isinstance(handle, dict) and handle.get("id"):
            await kill_oneoff_dyno(str(handle["id"]))
    await drain_pool()


async def shutdown_dispatch() -> dict[str, int]:
    """Stop tracked work without draining queues or starting replacements."""
    cancel_deferred_drain()
    running = list(pool.running.values())
    dyno_ids = [
        str(job.handle["id"])
        for job in running
        if isinstance(job.handle, dict) and job.handle.get("id")
    ]

    killed = 0
    failed = 0
    if use_heroku() and dyno_ids:
        log.info("Shutdown: stopping %s Heroku one-off dyno(s)", len(dyno_ids))
        results = await asyncio.gather(
            *(kill_oneoff_dyno(dyno_id) for dyno_id in dyno_ids),
            return_exceptions=True,
        )
        killed = sum(result is True for result in results)
        failed = len(results) - killed
        if failed:
            log.error(
                "Shutdown: failed to stop %s/%s Heroku one-off dyno(s); "
                "they may run until their commands exit",
                failed,
                len(dyno_ids),
            )

    # Clear in-memory state for local --reload and prevent stale capacity.
    for job in running:
        pool.release(job.job_id)
    for queue in pool.queues.values():
        queue.clear()
    pool.queued_ids.clear()

    return {
        "running": len(running),
        "dynos_found": len(dyno_ids),
        "dynos_killed": killed,
        "dynos_failed": failed,
    }
