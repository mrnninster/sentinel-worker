"""One-off/thread entrypoint: scrape, stream-status, or transcript."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import httpx

from dispatch.lifecycle import is_shutting_down, sleep_unless_shutdown

log = logging.getLogger("jobs.runner")


def _setup_logging() -> None:
    level = (os.environ.get("LOG_LEVEL") or "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _post_callback(url: str, token: str, body: dict[str, Any], *, fail: bool = False) -> None:
    if not url:
        log.error("ARG_CALLBACK_URL missing; cannot report result")
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    target = url
    if fail and url.rstrip("/").endswith("/result"):
        target = url.rstrip("/")[: -len("/result")] + "/fail"

    # Retry: dropping this callback would also strand the job's pool slot until
    # the middleman restarts, so a transient blip must not be fatal.
    attempts = 3
    delay = 5.0
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(target, json=body, headers=headers)
            if resp.status_code < 400:
                log.info("Callback ok %s status=%s", target, resp.status_code)
                return
            log.error(
                "Callback %s → %s %s (attempt %s/%s)",
                target,
                resp.status_code,
                resp.text[:400],
                attempt,
                attempts,
            )
            if resp.status_code < 500:
                return
        except Exception as exc:
            log.warning(
                "Callback failed url=%s (attempt %s/%s): %s",
                target,
                attempt,
                attempts,
                exc,
            )
        if attempt < attempts and sleep_unless_shutdown(delay):
            delay *= 2
        else:
            break
    log.error("Callback giving up url=%s", target)


def _run_scrape(payload: dict[str, Any], worker_id: str) -> dict[str, Any]:
    from scraper_bridge import run_scrape

    job_id = payload["job_id"]
    scrape_request = payload.get("scrape_request") or {}
    result = asyncio.run(run_scrape(scrape_request))
    meetings = result.get("meetings") or []
    return {
        "worker_id": worker_id,
        "ok": True,
        "load_type": "scrape",
        "job_id": job_id,
        "source_id": payload.get("source_id"),
        "meetings": meetings,
        "meta": result.get("meta") or {},
    }


def _run_stream_status(
    payload: dict[str, Any],
    worker_id: str,
    *,
    callback_url: str,
    callback_token: str,
) -> None:
    """Poll until terminal status; POST each update to callback."""
    from scraper_bridge import run_stream_status

    job_id = payload["job_id"]
    meeting_id = payload.get("meeting_id")
    channel_url = payload["channel_url"]
    video_id = payload.get("video_id")
    video_url = payload.get("video_url")
    timezone = payload.get("timezone") or "America/New_York"
    poll_interval = max(15, int(payload.get("poll_interval_seconds") or 60))
    max_duration = int(payload.get("max_duration_seconds") or 28800)

    started = time.time()
    while True:
        if is_shutting_down():
            # Report a retryable failure so Command re-dispatches immediately
            # instead of waiting for the monitor lease to expire.
            log.info("stream-status stopping for shutdown job_id=%s", job_id)
            _post_callback(
                callback_url,
                callback_token,
                {
                    "worker_id": worker_id,
                    "ok": False,
                    "load_type": "stream_status",
                    "job_id": job_id,
                    "meeting_id": meeting_id,
                    "error": "worker shutting down",
                    "reason": "worker_shutdown",
                    "retryable": True,
                    "terminal": True,
                },
                fail=True,
            )
            return

        elapsed = time.time() - started
        if elapsed > max_duration:
            body = {
                "worker_id": worker_id,
                "ok": True,
                "load_type": "stream_status",
                "job_id": job_id,
                "meeting_id": meeting_id,
                "channel_url": channel_url,
                "timezone": timezone,
                "status": "concluded",
                "video_id": video_id,
                "video_url": video_url,
                "note": "max_duration_reached",
                "terminal": True,
            }
            _post_callback(callback_url, callback_token, body)
            return

        try:
            status = asyncio.run(
                run_stream_status(
                    {
                        "channel_url": channel_url,
                        "video_id": video_id,
                        "video_url": video_url,
                        "timezone": timezone,
                    }
                )
            )
        except Exception as exc:
            log.exception("stream-status failed job_id=%s", job_id)
            _post_callback(
                callback_url,
                callback_token,
                {
                    "worker_id": worker_id,
                    "ok": False,
                    "load_type": "stream_status",
                    "job_id": job_id,
                    "meeting_id": meeting_id,
                    "error": str(exc),
                    "terminal": True,
                },
                fail=True,
            )
            return

        mapped = (status.get("status") or "").lower()
        # fetch_failed / unknown are non-terminal: keep polling so a blocked or
        # empty YouTube response cannot falsely conclude the meeting.
        terminal = mapped in {"concluded", "adjourned", "skipped"}
        body = {
            "worker_id": worker_id,
            "ok": True,
            "load_type": "stream_status",
            "job_id": job_id,
            "meeting_id": meeting_id,
            "channel_url": status.get("channel_url") or channel_url,
            "timezone": timezone,
            "status": mapped,
            "video_id": status.get("video_id") or video_id,
            "video_url": video_url,
            "video_title": status.get("video_title"),
            "meeting_link": status.get("meeting_link"),
            "scheduled_time": status.get("scheduled_time"),
            "started_streaming_on": status.get("started_streaming_on"),
            "published_time": status.get("published_time"),
            "note": status.get("note"),
            "live_videos": status.get("live_videos") or [],
            "upcoming_videos": status.get("upcoming_videos") or [],
            "concluded_on_page": status.get("concluded_on_page") or [],
            "skipped_videos": status.get("skipped_videos") or [],
            "match_diagnostics": status.get("match_diagnostics"),
            "terminal": terminal,
        }
        _post_callback(callback_url, callback_token, body)
        if terminal:
            return
        sleep_unless_shutdown(poll_interval)


def run_job_from_env(env: Optional[dict[str, str]] = None) -> int:
    """
    Execute one job using ARG_* from env mapping (or os.environ).

    When `env` is passed (local threads), ARG_* are read from that dict only so
    concurrent jobs do not clobber each other via os.environ.
    """
    _setup_logging()
    source = env if env is not None else dict(os.environ)

    # One-off dynos: env is already process-wide. Local threads: only fill missing
    # non-ARG tooling keys so concurrent ARG_* payloads stay isolated.
    if env is not None:
        for k, v in env.items():
            if k.startswith("ARG_"):
                continue
            if k not in os.environ:
                os.environ[k] = v

    job_type = (source.get("ARG_JOB_TYPE") or "").strip()
    raw = source.get("ARG_JOB_JSON") or "{}"
    callback_url = (source.get("ARG_CALLBACK_URL") or "").strip()
    callback_token = (source.get("ARG_CALLBACK_TOKEN") or "").strip()
    worker_id = (source.get("ARG_WORKER_ID") or "scrape-worker-1").strip()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Invalid ARG_JOB_JSON")
        return 1

    job_id = payload.get("job_id") or "unknown"
    log.info("Job start type=%s job_id=%s", job_type, job_id)

    try:
        if job_type == "scrape":
            body = _run_scrape(payload, worker_id)
            body["terminal"] = True
            _post_callback(callback_url, callback_token, body)
            return 0
        if job_type == "stream_status":
            _run_stream_status(
                payload,
                worker_id,
                callback_url=callback_url,
                callback_token=callback_token,
            )
            return 0
        if job_type == "transcript":
            from jobs.transcript import run_transcript_job

            body = run_transcript_job(payload, source, worker_id)
            _post_callback(callback_url, callback_token, body)
            return 0
        raise ValueError(f"Unknown ARG_JOB_TYPE={job_type!r}")
    except Exception as exc:
        coordinator_relayed = False
        rate_limited = bool(getattr(exc, "rate_limited", False))
        reason = getattr(exc, "reason", None)
        # Expected business outcomes (no captions) are warnings; real errors get full tracebacks.
        if reason == "no_captions":
            log.warning(
                "Transcript unavailable (no captions) type=%s job_id=%s video_id=%s: %s",
                job_type,
                job_id,
                payload.get("video_id"),
                exc,
            )
        else:
            log.exception("Job failed type=%s job_id=%s", job_type, job_id)
        if job_type == "transcript":
            from jobs.transcript import post_failure

            post_failure(
                url=str(payload.get("fail_url") or ""),
                token=source.get("WORKER_SHARED_TOKEN") or source.get("WORKER_TOKEN") or "",
                worker_id=worker_id,
                meeting_id=payload.get("meeting_id"),
                video_id=payload.get("video_id"),
                error=str(exc),
                rate_limited=rate_limited,
                reason=reason,
            )
            coordinator_relayed = True
        _post_callback(
            callback_url,
            callback_token,
            {
                "worker_id": worker_id,
                "ok": False,
                "load_type": job_type or "unknown",
                "job_id": job_id,
                "error": str(exc),
                "rate_limited": rate_limited,
                "reason": reason,
                "coordinator_relayed": coordinator_relayed,
                "terminal": True,
            },
            fail=True,
        )
        return 1


def main() -> None:
    raise SystemExit(run_job_from_env())


if __name__ == "__main__":
    main()
