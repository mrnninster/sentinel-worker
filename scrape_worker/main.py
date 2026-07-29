"""FastAPI scrape worker: accepts push commands from Sentinel coordinator."""

from __future__ import annotations

import asyncio
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

# Load scrape_worker/.env before reading os.environ (real env wins).
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from scraper_bridge import run_scrape, run_stream_status, scraper_mode

__version__ = "1.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scrape_worker")

# job_id -> asyncio.Task
_running: dict[str, asyncio.Task] = {}
# job_id -> "scrape" | "stream_status"
_running_load_type: dict[str, str] = {}
_allowed_sources: list[str] = ["*"]
_heartbeat_task: asyncio.Task | None = None
_scrape_lock = asyncio.Lock()  # one Playwright scrape at a time

LOAD_TYPE_SCRAPE = "scrape"
LOAD_TYPE_STREAM_STATUS = "stream_status"


def _worker_id() -> str:
    return (os.environ.get("WORKER_ID") or "scrape-worker-1").strip()


def _worker_token() -> str:
    return (os.environ.get("WORKER_SHARED_TOKEN") or os.environ.get("WORKER_TOKEN") or "").strip()


def _coordinator_url() -> str:
    return (os.environ.get("COORDINATOR_URL") or "").rstrip("/")


def _public_base_url() -> str:
    return (os.environ.get("WORKER_PUBLIC_URL") or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


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


def _source_allowed(source_id: str) -> bool:
    if "*" in _allowed_sources:
        return True
    return source_id in _allowed_sources


def _load_snapshot() -> dict[str, Any]:
    """Build load fields for heartbeat / health."""
    by_type: dict[str, int] = {
        LOAD_TYPE_SCRAPE: 0,
        LOAD_TYPE_STREAM_STATUS: 0,
    }
    running_jobs: list[dict[str, str]] = []
    for job_id, load_type in _running_load_type.items():
        by_type[load_type] = by_type.get(load_type, 0) + 1
        running_jobs.append({"job_id": job_id, "load_type": load_type})
    return {
        "load": len(_running),
        "load_by_type": by_type,
        "running_job_ids": list(_running.keys()),
        "running_jobs": running_jobs,
    }


def _track_job(job_id: str, task: asyncio.Task, load_type: str) -> None:
    _running[job_id] = task
    _running_load_type[job_id] = load_type


def _untrack_job(job_id: str) -> asyncio.Task | None:
    _running_load_type.pop(job_id, None)
    return _running.pop(job_id, None)


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
    """Push current load to the coordinator (idle = load 0)."""
    snap = _load_snapshot()
    # Don't claim idle if another job type is still running.
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
            "Load notify reason=%s load=%s by_type=%s jobs=%s",
            reason,
            snap["load"],
            snap["load_by_type"],
            snap["running_jobs"],
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
        log.info("Skip auto-register (need COORDINATOR_URL, WORKER_SHARED_TOKEN, WORKER_PUBLIC_URL)")
        return
    open_reg = (os.environ.get("WORKER_REGISTRATION_OPEN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    path = "/v1/workers/register" if open_reg else None
    if not path:
        log.info("WORKER_REGISTRATION_OPEN not set; register worker via admin API")
        return
    body = {
        "worker_id": _worker_id(),
        "base_url": public,
        "token": token,
        "capacity": int(os.environ.get("WORKER_CAPACITY") or "1"),
        "allowed_sources": _allowed_sources,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base}{path}", json=body)
            log.info("Self-register → %s %s", resp.status_code, resp.text[:200])
    except Exception:
        log.exception("Self-register failed")


async def _execute_scrape(cmd: ScrapeCommand) -> None:
    try:
        async with _scrape_lock:
            result = await run_scrape(cmd.scrape_request)
        meetings = result.get("meetings") or []
        callback = cmd.callback_url or f"/v1/workers/jobs/{cmd.job_id}/result"
        if callback.startswith("http"):
            # absolute URL provided — still go through coordinator helper path
            path = callback[len(_coordinator_url()) :] if _coordinator_url() and callback.startswith(
                _coordinator_url()
            ) else f"/v1/workers/jobs/{cmd.job_id}/result"
        else:
            path = callback if callback.startswith("/") else f"/v1/workers/jobs/{cmd.job_id}/result"
        await _post_coordinator(
            path,
            {
                "worker_id": _worker_id(),
                "ok": True,
                "meetings": meetings,
                "meta": result.get("meta") or {},
            },
        )
    except Exception as exc:
        log.exception("Scrape job %s failed", cmd.job_id)
        await _post_coordinator(
            f"/v1/workers/jobs/{cmd.job_id}/result",
            {"worker_id": _worker_id(), "ok": False, "error": str(exc)},
        )
    finally:
        _untrack_job(cmd.job_id)
        await _notify_load(reason="idle")


async def _execute_monitor(cmd: StreamStatusCommand) -> None:
    started = asyncio.get_event_loop().time()
    try:
        while True:
            if cmd.job_id not in _running:
                break
            elapsed = asyncio.get_event_loop().time() - started
            if elapsed > cmd.max_duration_seconds:
                await _post_coordinator(
                    f"/v1/workers/jobs/{cmd.job_id}/result",
                    {
                        "worker_id": _worker_id(),
                        "ok": True,
                        "load_type": LOAD_TYPE_STREAM_STATUS,
                        "job_id": cmd.job_id,
                        "meeting_id": cmd.meeting_id,
                        "channel_url": cmd.channel_url,
                        "timezone": cmd.timezone,
                        "status": "concluded",
                        "video_id": cmd.video_id,
                        "video_url": cmd.video_url,
                        "note": "max_duration_reached",
                    },
                )
                break
            try:
                status = await run_stream_status(
                    {
                        "channel_url": cmd.channel_url,
                        "video_id": cmd.video_id,
                        "video_url": cmd.video_url,
                        "timezone": cmd.timezone,
                    }
                )
            except Exception as exc:
                log.exception("stream-status failed for %s", cmd.job_id)
                await _post_coordinator(
                    f"/v1/workers/jobs/{cmd.job_id}/fail",
                    {
                        "worker_id": _worker_id(),
                        "ok": False,
                        "load_type": LOAD_TYPE_STREAM_STATUS,
                        "job_id": cmd.job_id,
                        "meeting_id": cmd.meeting_id,
                        "error": str(exc),
                    },
                )
                break

            mapped = (status.get("status") or "").lower()
            await _post_coordinator(
                f"/v1/workers/jobs/{cmd.job_id}/result",
                {
                    "worker_id": _worker_id(),
                    "ok": True,
                    "load_type": LOAD_TYPE_STREAM_STATUS,
                    "job_id": cmd.job_id,
                    "meeting_id": cmd.meeting_id,
                    "channel_url": status.get("channel_url") or cmd.channel_url,
                    "timezone": cmd.timezone,
                    "status": mapped,
                    "video_id": status.get("video_id") or cmd.video_id,
                    "video_url": cmd.video_url,
                    "video_title": status.get("video_title"),
                    "meeting_link": status.get("meeting_link"),
                    "scheduled_time": status.get("scheduled_time"),
                    "started_streaming_on": status.get("started_streaming_on"),
                    "note": status.get("note"),
                    "live_videos": status.get("live_videos") or [],
                    "upcoming_videos": status.get("upcoming_videos") or [],
                    "concluded_on_page": status.get("concluded_on_page") or [],
                    "skipped_videos": status.get("skipped_videos") or [],
                },
            )
            if mapped in {"concluded", "adjourned", "skipped"}:
                break
            await asyncio.sleep(max(15, int(cmd.poll_interval_seconds)))
    finally:
        _untrack_job(cmd.job_id)
        await _notify_load(reason="idle")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _heartbeat_task, _allowed_sources
    raw = (os.environ.get("WORKER_ALLOWED_SOURCES") or "*").strip()
    if raw == "*":
        _allowed_sources = ["*"]
    else:
        _allowed_sources = [s.strip() for s in raw.split(",") if s.strip()]
    await _register_with_coordinator()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="worker-heartbeat")
    log.info(
        "scrape-worker %s started id=%s mode=%s allowed=%s",
        __version__,
        _worker_id(),
        scraper_mode(),
        _allowed_sources,
    )
    try:
        yield
    finally:
        if _heartbeat_task:
            _heartbeat_task.cancel()
        for task in list(_running.values()):
            task.cancel()
        _running.clear()
        _running_load_type.clear()


app = FastAPI(
    title="Sentinel Scrape Worker",
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
    if body.job_id in _running:
        return {"ok": True, "accepted": False, "reason": "already_running"}
    task = asyncio.create_task(_execute_scrape(body), name=f"scrape-{body.job_id}")
    _track_job(body.job_id, task, LOAD_TYPE_SCRAPE)
    return {"ok": True, "accepted": True, "job_id": body.job_id, "load_type": LOAD_TYPE_SCRAPE}


@app.post("/v1/commands/stream-status", status_code=202)
async def command_stream_status(
    body: StreamStatusCommand, _: None = Depends(require_worker_auth)
) -> dict:
    if body.job_id in _running:
        return {"ok": True, "accepted": False, "reason": "already_running"}
    task = asyncio.create_task(_execute_monitor(body), name=f"monitor-{body.job_id}")
    _track_job(body.job_id, task, LOAD_TYPE_STREAM_STATUS)
    return {
        "ok": True,
        "accepted": True,
        "job_id": body.job_id,
        "load_type": LOAD_TYPE_STREAM_STATUS,
    }


@app.post("/v1/commands/{job_id}/cancel")
async def command_cancel(job_id: str, _: None = Depends(require_worker_auth)) -> dict:
    task = _untrack_job(job_id)
    if task:
        task.cancel()
        await _notify_load(reason="idle")
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}
