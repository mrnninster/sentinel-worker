"""Sentinel scrape worker middleman: accept commands, dispatch one-offs/threads, relay results."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
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
from dispatch.spawner import enqueue_and_drain, release_and_drain, use_heroku
from scraper_bridge import scraper_mode

__version__ = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scrape_worker")

_allowed_sources: list[str] = ["*"]
_heartbeat_task: asyncio.Task | None = None


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


async def _post_coordinator(path: str, body: dict[str, Any]) -> None:
    base = _coordinator_url()
    if not base:
        log.warning("COORDINATOR_URL unset; skip callback %s", path)
        return
    token = _worker_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Worker-Id": _worker_id(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{base}{path}", json=body, headers=headers)
        if resp.status_code >= 400:
            log.error("Coordinator %s → %s %s", path, resp.status_code, resp.text[:400])


async def _notify_load(*, reason: str = "heartbeat") -> None:
    snap = _load_snapshot()
    if reason == "idle" and snap["load"] > 0:
        reason = "job_finished"
    try:
        await _post_coordinator(
            "/v1/workers/heartbeat",
            {
                "worker_id": _worker_id(),
                "reason": reason,
                **snap,
            },
        )
        log.info(
            "Load notify reason=%s load=%s by_type=%s queued=%s",
            reason,
            snap["load"],
            snap["load_by_type"],
            snap["queued_by_type"],
        )
    except Exception:
        log.exception("Load notify failed reason=%s", reason)


async def _heartbeat_loop() -> None:
    interval = int(os.environ.get("WORKER_HEARTBEAT_SECONDS") or "30")
    while True:
        await _notify_load(reason="heartbeat")
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
    except Exception:
        log.exception("Self-register failed")


async def _relay_to_coordinator(job_id: str, body: dict[str, Any], *, fail: bool) -> None:
    path = (
        f"/v1/workers/jobs/{job_id}/fail"
        if fail
        else f"/v1/workers/jobs/{job_id}/result"
    )
    # Strip middleman-only flags before forwarding
    forward = {k: v for k, v in body.items() if k != "terminal"}
    if "worker_id" not in forward:
        forward["worker_id"] = _worker_id()
    await _post_coordinator(path, forward)


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
        if _heartbeat_task:
            _heartbeat_task.cancel()


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

    # Transcript jobs upload the PDF/failure directly to their dedicated Command
    # endpoints; this internal callback exists only to release the pool slot.
    if not body.get("coordinator_relayed"):
        await _relay_to_coordinator(job_id, body, fail=False)

    if terminal:
        await release_and_drain(job_id, kill_dyno=False)
        await _notify_load(reason="idle")
    return {"ok": True, "job_id": job_id, "terminal": terminal}


@app.post("/v1/internal/jobs/{job_id}/fail")
async def internal_job_fail(
    job_id: str,
    body: dict[str, Any],
    _: None = Depends(require_internal_auth),
) -> dict:
    if not body.get("coordinator_relayed"):
        await _relay_to_coordinator(job_id, body, fail=True)
    await release_and_drain(job_id, kill_dyno=False)
    await _notify_load(reason="idle")
    return {"ok": True, "job_id": job_id, "failed": True}
