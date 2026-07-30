"""Sentinel scrape worker middleman: accept commands, dispatch one-offs/threads, relay results."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from dispatch.pool import (
    LOAD_TYPE_SCRAPE,
    LOAD_TYPE_STREAM_STATUS,
    LOAD_TYPE_TRANSCRIPT,
    QueuedJob,
    pool,
)
from dispatch.lifecycle import clear_shutdown, request_shutdown, terminate_children
from dispatch.spawner import (
    enqueue_and_drain,
    release_and_drain,
    shutdown_dispatch,
    use_heroku,
)
from scraper_bridge import scraper_mode

__version__ = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scrape_worker")

_allowed_sources: list[str] = ["*"]
_heartbeat_task: asyncio.Task | None = None
# Results Command could not receive yet, retried on the heartbeat tick.
_pending_relays: deque[tuple[str, dict[str, Any]]] = deque()
_coordinator_online = True


def _worker_id() -> str:
    return (os.environ.get("WORKER_ID") or "scrape-worker-1").strip()


def _worker_token() -> str:
    return (
        os.environ.get("WORKER_SHARED_TOKEN") or os.environ.get("WORKER_TOKEN") or ""
    ).strip()


def _coordinator_url() -> str:
    return (os.environ.get("COORDINATOR_URL") or "").rstrip("/")


def _public_base_url() -> str:
    return (
        os.environ.get("WORKER_PUBLIC_URL") or os.environ.get("PUBLIC_BASE_URL") or ""
    ).rstrip("/")


def _internal_token() -> str:
    return (
        os.environ.get("INTERNAL_CALLBACK_TOKEN")
        or os.environ.get("WORKER_SHARED_TOKEN")
        or os.environ.get("WORKER_TOKEN")
        or "dev-internal"
    ).strip()


def _shutdown_grace() -> float:
    try:
        return max(0.0, float(os.environ.get("SHUTDOWN_GRACE_SECONDS") or "5"))
    except ValueError:
        return 5.0


def _worker_capacity() -> int:
    # Prefer explicit HEROKU_ONEOFF_LIMIT; fall back to WORKER_CAPACITY; default 50.
    for key in ("HEROKU_ONEOFF_LIMIT", "WORKER_CAPACITY"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
    return 50


def require_worker_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = _worker_token()
    if not expected:
        raise HTTPException(503, "WORKER_SHARED_TOKEN not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(401, "Invalid token")


def require_internal_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = _internal_token()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(401, "Invalid internal token")


class CommandSourcesIn(BaseModel):
    source_ids: list[str] = Field(default_factory=lambda: ["*"])


class ScrapeCommand(BaseModel):
    job_id: str
    source_id: str
    scrape_request: dict[str, Any]
    callback_url: str | None = None


class StreamStatusCommand(BaseModel):
    job_id: str
    meeting_id: str
    channel_url: str
    video_id: str | None = None
    video_url: str | None = None
    timezone: str = "America/New_York"
    callback_url: str | None = None
    poll_interval_seconds: int = 60
    max_duration_seconds: int = 28800


class TranscriptCommand(BaseModel):
    job_id: str | None = None
    meeting_id: str | None = None
    video_id: str | None = None
    video_url: str | None = None
    title: str | None = None
    source_id: str | None = None
    language: str = "en"
    timezone: str = "America/New_York"
    callback_url: str
    fail_url: str

    def pool_job_id(self) -> str:
        if self.job_id:
            return self.job_id
        identity = self.meeting_id or self.video_id
        if not identity and self.video_url:
            identity = hashlib.sha256(self.video_url.encode("utf-8")).hexdigest()[:20]
        if not identity:
            raise ValueError("meeting_id, video_id, video_url, or job_id is required")
        return f"transcript:{identity}"


class CancelCommand(BaseModel):
    job_id: str


def _source_allowed(source_id: str) -> bool:
    if "*" in _allowed_sources:
        return True
    return source_id in _allowed_sources


def _load_snapshot() -> dict[str, Any]:
    snap = pool.snapshot()
    # Keep pool max in sync with env capacity.
    snap["capacity"] = _worker_capacity()
    snap["oneoff_limit"] = pool.max_oneoff
    return snap


def _relay_attempts() -> int:
    try:
        return max(1, int(os.environ.get("COORDINATOR_RETRY_ATTEMPTS") or "3"))
    except ValueError:
        return 3


def _pending_relay_max() -> int:
    try:
        return max(0, int(os.environ.get("COORDINATOR_PENDING_MAX") or "500"))
    except ValueError:
        return 500


async def _post_coordinator(
    path: str,
    body: dict[str, Any],
    *,
    attempts: int = 1,
) -> bool:
    """POST to Command. Returns True on 2xx/3xx. Never raises.

    Retries connection errors and 5xx with backoff. A 4xx is not retried: the
    coordinator understood us and repeating the call cannot change the outcome.
    """
    base = _coordinator_url()
    if not base:
        log.warning("COORDINATOR_URL unset; skip callback %s", path)
        return False
    headers = {
        "Authorization": f"Bearer {_worker_token()}",
        "X-Worker-Id": _worker_id(),
        "Content-Type": "application/json",
    }
    delay = 5.0
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"{base}{path}", json=body, headers=headers)
            if resp.status_code < 400:
                return True
            if resp.status_code < 500:
                log.error(
                    "Coordinator %s → %s %s (not retrying)",
                    path,
                    resp.status_code,
                    resp.text[:400],
                )
                return False
            log.warning(
                "Coordinator %s → %s (attempt %s/%s)",
                path,
                resp.status_code,
                attempt,
                attempts,
            )
        except Exception as exc:
            log.warning(
                "Coordinator %s unreachable (attempt %s/%s): %s",
                path,
                attempt,
                attempts,
                exc,
            )
        if attempt < attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
    return False


async def _notify_load(*, reason: str = "heartbeat") -> None:
    global _coordinator_online
    snap = _load_snapshot()
    if reason == "idle" and snap["load"] > 0:
        reason = "job_finished"
    ok = await _post_coordinator(
        "/v1/workers/heartbeat",
        {
            "worker_id": _worker_id(),
            "reason": reason,
            **snap,
        },
    )
    if ok:
        # Command may have forgotten us while it was down (restart / empty state).
        if not _coordinator_online:
            log.info("Coordinator reachable again; re-registering and flushing relays")
            _coordinator_online = True
            await _register_with_coordinator()
            await _flush_pending_relays()
        log.info(
            "Load notify reason=%s load=%s by_type=%s queued=%s",
            reason,
            snap["load"],
            snap["load_by_type"],
            snap["queued_by_type"],
        )
    else:
        if _coordinator_online:
            log.error("Coordinator unreachable; worker keeps queueing and running jobs")
        _coordinator_online = False


async def _heartbeat_loop() -> None:
    interval = int(os.environ.get("WORKER_HEARTBEAT_SECONDS") or "30")
    while True:
        await _notify_load(reason="heartbeat")
        await _flush_pending_relays()
        await asyncio.sleep(interval)


async def _register_with_coordinator() -> None:
    base = _coordinator_url()
    token = _worker_token()
    public = _public_base_url()
    if not base or not token or not public:
        log.info(
            "Skip auto-register (need COORDINATOR_URL, WORKER_SHARED_TOKEN, WORKER_PUBLIC_URL)"
        )
        return
    open_reg = (os.environ.get("WORKER_REGISTRATION_OPEN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not open_reg:
        log.info("WORKER_REGISTRATION_OPEN not set; register worker via admin API")
        return
    body = {
        "worker_id": _worker_id(),
        "base_url": public,
        "token": token,
        "capacity": _worker_capacity(),
        "oneoff_limit": _worker_capacity(),
        "allowed_sources": _allowed_sources,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base}/v1/workers/register", json=body)
            log.info("Self-register → %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        # Not fatal: heartbeats keep retrying and re-register once Command answers.
        log.warning("Self-register failed (will retry on heartbeat): %s", exc)


async def _relay_to_coordinator(job_id: str, body: dict[str, Any], *, fail: bool) -> bool:
    """Forward a job result/failure. Buffers for retry if Command is unreachable."""
    path = (
        f"/v1/workers/jobs/{job_id}/fail"
        if fail
        else f"/v1/workers/jobs/{job_id}/result"
    )
    # Strip middleman-only flags before forwarding
    forward = {k: v for k, v in body.items() if k != "terminal"}
    if "worker_id" not in forward:
        forward["worker_id"] = _worker_id()
    if await _post_coordinator(path, forward, attempts=_relay_attempts()):
        return True

    limit = _pending_relay_max()
    if limit:
        if len(_pending_relays) >= limit:
            dropped_path, _ = _pending_relays.popleft()
            log.error("Pending relay buffer full; dropped oldest %s", dropped_path)
        _pending_relays.append((path, forward))
        log.warning(
            "Buffered relay for retry path=%s pending=%s", path, len(_pending_relays)
        )
    return False


async def _flush_pending_relays() -> None:
    """Drain buffered results once Command answers again."""
    while _pending_relays:
        path, body = _pending_relays[0]
        if not await _post_coordinator(path, body):
            return
        _pending_relays.popleft()
        log.info("Flushed buffered relay path=%s pending=%s", path, len(_pending_relays))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _heartbeat_task, _allowed_sources
    raw = (os.environ.get("WORKER_ALLOWED_SOURCES") or "*").strip()
    if raw == "*":
        _allowed_sources = ["*"]
    else:
        _allowed_sources = [s.strip() for s in raw.split(",") if s.strip()]

    # Keep pool limit aligned with capacity (default 50).
    pool.max_oneoff = _worker_capacity()
    # A --reload restart reuses the interpreter; clear any stale shutdown flag.
    clear_shutdown()

    await _register_with_coordinator()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="worker-heartbeat")
    log.info(
        "scrape-worker middleman %s id=%s dispatch=%s capacity=%s scraper_mode=%s",
        __version__,
        _worker_id(),
        "heroku" if use_heroku() else "local",
        pool.max_oneoff,
        scraper_mode(),
    )
    try:
        yield
    finally:
        # Tell monitor loops to stop at their next poll boundary, then kill the
        # Playwright driver/Chromium children that daemon threads leave behind.
        request_shutdown()
        if _heartbeat_task:
            _heartbeat_task.cancel()
        dispatch_summary = await shutdown_dispatch()
        if dispatch_summary["running"]:
            log.info("Shutdown dispatch summary: %s", dispatch_summary)
        if _pending_relays:
            log.warning(
                "Shutting down with %s unrelayed result(s); Command will re-dispatch "
                "after lease expiry",
                len(_pending_relays),
            )
        await asyncio.to_thread(terminate_children, grace=_shutdown_grace())


app = FastAPI(
    title="Sentinel Scrape Worker (middleman)",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "sentinel-scrape-worker",
        "version": __version__,
        "worker_id": _worker_id(),
        "dispatch_mode": "heroku" if use_heroku() else "local",
        "scraper_mode": scraper_mode(),
        "allowed_sources": _allowed_sources,
        **_load_snapshot(),
    }


@app.post("/v1/command-sources")
def set_command_sources(
    body: CommandSourcesIn, _: None = Depends(require_worker_auth)
) -> dict:
    global _allowed_sources
    _allowed_sources = body.source_ids or ["*"]
    return {"ok": True, "allowed_sources": _allowed_sources}


@app.post("/v1/commands/scrape", status_code=202)
async def command_scrape(
    body: ScrapeCommand, _: None = Depends(require_worker_auth)
) -> dict:
    if not _source_allowed(body.source_id):
        raise HTTPException(403, f"source {body.source_id} not in command sources")
    job = QueuedJob(
        job_id=body.job_id,
        load_type=LOAD_TYPE_SCRAPE,
        payload=body.model_dump(),
    )
    result = await enqueue_and_drain(job)
    await _notify_load(reason="heartbeat")
    return result


@app.post("/v1/commands/stream-status", status_code=202)
async def command_stream_status(
    body: StreamStatusCommand, _: None = Depends(require_worker_auth)
) -> dict:
    job = QueuedJob(
        job_id=body.job_id,
        load_type=LOAD_TYPE_STREAM_STATUS,
        payload=body.model_dump(),
    )
    result = await enqueue_and_drain(job)
    await _notify_load(reason="heartbeat")
    return result


@app.post("/v1/commands/transcript", status_code=202)
async def command_transcript(
    body: TranscriptCommand, _: None = Depends(require_worker_auth)
) -> dict:
    if not body.meeting_id and not body.video_id and not body.video_url:
        raise HTTPException(422, "meeting_id, video_id, or video_url is required")
    if body.source_id and not _source_allowed(body.source_id):
        raise HTTPException(403, f"source {body.source_id} not in command sources")
    try:
        job_id = body.pool_job_id()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    payload = body.model_dump()
    payload["job_id"] = job_id
    job = QueuedJob(
        job_id=job_id,
        load_type=LOAD_TYPE_TRANSCRIPT,
        payload=payload,
    )
    result = await enqueue_and_drain(job)
    await _notify_load(reason="heartbeat")
    return result


@app.post("/v1/commands/{job_id}/cancel")
async def command_cancel(job_id: str, _: None = Depends(require_worker_auth)) -> dict:
    return await _cancel_job(job_id)


@app.post("/v1/commands/cancel")
async def command_cancel_body(
    body: CancelCommand, _: None = Depends(require_worker_auth)
) -> dict:
    return await _cancel_job(body.job_id)


async def _cancel_job(job_id: str) -> dict:
    if pool.cancel_queued(job_id):
        await _notify_load(reason="idle")
        return {"ok": True, "cancelled": True, "where": "queued"}
    if job_id in pool.running:
        await release_and_drain(job_id, kill_dyno=True)
        await _notify_load(reason="idle")
        return {"ok": True, "cancelled": True, "where": "running"}
    return {"ok": True, "cancelled": False}


@app.post("/v1/internal/jobs/{job_id}/result")
async def internal_job_result(
    job_id: str,
    body: dict[str, Any],
    _: None = Depends(require_internal_auth),
) -> dict:
    """One-off / local thread reports progress or terminal result."""
    running = pool.running.get(job_id)
    load_type = body.get("load_type") or (running.load_type if running else None)
    status = str(body.get("status") or "").lower()

    if body.get("ok") is False:
        terminal = True
    elif body.get("terminal") is True or load_type == LOAD_TYPE_SCRAPE:
        terminal = True
    elif load_type == LOAD_TYPE_STREAM_STATUS:
        terminal = status in {"concluded", "adjourned", "skipped"}
    else:
        terminal = bool(body.get("terminal"))

    relayed = True
    try:
        # Transcript jobs upload the PDF/failure directly to their dedicated Command
        # endpoints; this internal callback exists only to release the pool slot.
        if not body.get("coordinator_relayed"):
            relayed = await _relay_to_coordinator(job_id, body, fail=False)
    finally:
        # Always free the slot: a job that finished has stopped consuming capacity
        # whether or not Command could be told about it.
        if terminal:
            await release_and_drain(job_id, kill_dyno=False)
            await _notify_load(reason="idle")
    return {"ok": True, "job_id": job_id, "terminal": terminal, "relayed": relayed}


@app.post("/v1/internal/jobs/{job_id}/fail")
async def internal_job_fail(
    job_id: str,
    body: dict[str, Any],
    _: None = Depends(require_internal_auth),
) -> dict:
    relayed = True
    try:
        if not body.get("coordinator_relayed"):
            relayed = await _relay_to_coordinator(job_id, body, fail=True)
    finally:
        await release_and_drain(job_id, kill_dyno=False)
        await _notify_load(reason="idle")
    return {"ok": True, "job_id": job_id, "failed": True, "relayed": relayed}
