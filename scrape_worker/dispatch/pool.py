"""Priority job pool with scrape/transcript reservations (max 50 one-offs)."""

from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

log = logging.getLogger(__name__)

LOAD_TYPE_SCRAPE = "scrape"
LOAD_TYPE_STREAM_STATUS = "stream_status"
LOAD_TYPE_TRANSCRIPT = "transcript"

# Drain order: monitors first, then scrape, then transcript.
PRIORITY_ORDER = (
    LOAD_TYPE_STREAM_STATUS,
    LOAD_TYPE_SCRAPE,
    LOAD_TYPE_TRANSCRIPT,
)


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


@dataclass
class QueuedJob:
    job_id: str
    load_type: str
    payload: dict[str, Any]


@dataclass
class RunningJob:
    job_id: str
    load_type: str
    payload: dict[str, Any]
    handle: Any = None  # thread | heroku dyno meta


@dataclass
class DynoPool:
    """In-memory queue + running set with reserved capacity."""

    max_oneoff: int = field(default_factory=lambda: _int_env("HEROKU_ONEOFF_LIMIT", 50))
    reserve_scrape: int = field(default_factory=lambda: _int_env("RESERVE_SCRAPE", 1))
    reserve_transcript: int = field(
        default_factory=lambda: _int_env("RESERVE_TRANSCRIPT", 1)
    )

    queues: dict[str, Deque[QueuedJob]] = field(default_factory=dict)
    running: dict[str, RunningJob] = field(default_factory=dict)
    queued_ids: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for t in PRIORITY_ORDER:
            self.queues.setdefault(t, deque())

    @property
    def flex_slots(self) -> int:
        return max(0, self.max_oneoff - self.reserve_scrape - self.reserve_transcript)

    def snapshot(self) -> dict[str, Any]:
        queued_by_type = {t: len(self.queues[t]) for t in PRIORITY_ORDER}
        # `load` / `load_by_type` represent all accepted work. Command uses
        # oneoff_running and queued_by_type separately for pool-fill math.
        load_by_type = dict(queued_by_type)
        running_jobs: list[dict[str, str]] = []
        for job in self.running.values():
            load_by_type[job.load_type] = load_by_type.get(job.load_type, 0) + 1
            running_jobs.append({"job_id": job.job_id, "load_type": job.load_type})
        return {
            "load": len(self.running) + sum(queued_by_type.values()),
            "capacity": self.max_oneoff,
            "oneoff_limit": self.max_oneoff,
            "oneoff_running": len(self.running),
            "load_by_type": load_by_type,
            "queued_by_type": queued_by_type,
            "running_job_ids": list(self.running.keys()),
            "running_jobs": running_jobs,
            "reserve_scrape": self.reserve_scrape,
            "reserve_transcript": self.reserve_transcript,
        }

    def has_job(self, job_id: str) -> bool:
        return job_id in self.running or job_id in self.queued_ids

    def enqueue(self, job: QueuedJob) -> bool:
        """Queue a job. Returns False if duplicate."""
        if self.has_job(job.job_id):
            return False
        if job.load_type not in self.queues:
            self.queues[job.load_type] = deque()
        self.queues[job.load_type].append(job)
        self.queued_ids[job.job_id] = job.load_type
        log.info(
            "Enqueued job_id=%s type=%s queue_depth=%s",
            job.job_id,
            job.load_type,
            len(self.queues[job.load_type]),
        )
        return True

    def _running_count(self, load_type: str) -> int:
        return sum(1 for j in self.running.values() if j.load_type == load_type)

    def _unused_reserve(self, load_type: str, reserve: int) -> int:
        return max(0, reserve - self._running_count(load_type))

    def can_start(self, load_type: str) -> bool:
        """Whether a new job of this type may claim a slot now."""
        used = len(self.running)
        if used >= self.max_oneoff:
            return False

        unused_scrape = self._unused_reserve(LOAD_TYPE_SCRAPE, self.reserve_scrape)
        unused_tx = self._unused_reserve(LOAD_TYPE_TRANSCRIPT, self.reserve_transcript)

        if load_type == LOAD_TYPE_STREAM_STATUS:
            # Monitors must leave unused scrape/transcript reserves free.
            return (used + 1) <= (self.max_oneoff - unused_scrape - unused_tx)

        if load_type == LOAD_TYPE_SCRAPE:
            if self._running_count(LOAD_TYPE_SCRAPE) < self.reserve_scrape:
                return True
            return (used + 1) <= (self.max_oneoff - unused_tx)

        if load_type == LOAD_TYPE_TRANSCRIPT:
            if self._running_count(LOAD_TYPE_TRANSCRIPT) < self.reserve_transcript:
                return True
            return (used + 1) <= (self.max_oneoff - unused_scrape)

        return used < self.max_oneoff

    def pop_next_runnable(self) -> Optional[QueuedJob]:
        """Pop highest-priority queued job that may start now."""
        for load_type in PRIORITY_ORDER:
            q = self.queues.get(load_type)
            if not q:
                continue
            if not self.can_start(load_type):
                continue
            job = q.popleft()
            self.queued_ids.pop(job.job_id, None)
            return job
        return None

    def mark_running(self, job: QueuedJob, handle: Any = None) -> RunningJob:
        running = RunningJob(
            job_id=job.job_id,
            load_type=job.load_type,
            payload=job.payload,
            handle=handle,
        )
        self.running[job.job_id] = running
        return running

    def release(self, job_id: str) -> Optional[RunningJob]:
        job = self.running.pop(job_id, None)
        if job:
            log.info("Released slot job_id=%s type=%s", job_id, job.load_type)
        lt = self.queued_ids.pop(job_id, None)
        if lt:
            q = self.queues.get(lt)
            if q:
                self.queues[lt] = deque(j for j in q if j.job_id != job_id)
        return job

    def cancel_queued(self, job_id: str) -> bool:
        lt = self.queued_ids.pop(job_id, None)
        if not lt:
            return False
        q = self.queues.get(lt)
        if not q:
            return False
        before = len(q)
        self.queues[lt] = deque(j for j in q if j.job_id != job_id)
        return len(self.queues[lt]) < before


pool = DynoPool()
