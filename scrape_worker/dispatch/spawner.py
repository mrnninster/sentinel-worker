"""Enqueue jobs and spawn local threads or Heroku one-offs."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from dispatch.heroku_oneoff import create_oneoff_dyno, heroku_configured, kill_oneoff_dyno
from dispatch.local_thread import start_local_thread
from dispatch.pool import QueuedJob, pool

log = logging.getLogger(__name__)


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
        dyno = await create_oneoff_dyno(command="python -m jobs.runner", env=env)
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
    """Start as many queued jobs as capacity allows. Returns number started."""
    started = 0
    while True:
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
        started += 1
        log.info(
            "Dispatched job_id=%s type=%s mode=%s",
            job.job_id,
            job.load_type,
            "heroku" if use_heroku() else "local",
        )
    return started


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
