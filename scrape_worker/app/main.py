"""
FastAPI entrypoint for the schedule scraper.

Modes:
  - llm: Playwright → HtmlCleaner → OpenAI → meetings
  - dedicated: vendored schedule.library parsers (e.g. swagit_table)
  - auto: dedicated when schedule_type is set, else llm
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings, reload_settings
from app.dedicated import list_schedule_types
from app.models import (
    HealthResponse,
    ScrapeScheduleRequest,
    ScrapeScheduleResponse,
    StreamStatusRequest,
    StreamStatusResponse,
    StreamVideoInfo,
)
from app.service import ScheduleScrapeService
from app.youtube_status import check_youtube_stream_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = reload_settings()
    app.state.service = ScheduleScrapeService(settings)
    key_suffix = settings.openai_api_key[-4:] if settings.openai_api_key else "none"
    log.info(
        "schedule scraper v%s started (openai_configured=%s, key_suffix=…%s)",
        __version__,
        bool(settings.openai_api_key),
        key_suffix,
    )
    yield


app = FastAPI(
    title="Schedule Scraper",
    description=(
        "Extract meeting schedules from a public calendar URL, optionally "
        "merged with a YouTube /streams fallback (live / concluded overlays "
        "and same-day stubs). Also exposes /stream-status for metadata-only "
        "YouTube live checks. Supports LLM extraction and dedicated platform "
        "parsers (Granicus, Swagit, Legistar, WordPress, YouTube, …)."
    ),
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    try:
        parsers_ok = True
        import schedule.schedule_scraper  # noqa: F401
    except Exception:
        parsers_ok = False
    return HealthResponse(
        status="ok",
        version=__version__,
        openai_configured=bool(settings.openai_api_key),
        dedicated_parsers_available=parsers_ok,
    )


@app.get("/schedule-types")
async def schedule_types() -> dict:
    """List known dedicated parser method names."""
    return {"schedule_types": list_schedule_types()}


@app.post("/scrape", response_model=ScrapeScheduleResponse)
async def scrape_schedule(body: ScrapeScheduleRequest) -> ScrapeScheduleResponse:
    """
    Scrape a schedule calendar page and return structured meetings.

    - **dedicated** / **auto+schedule_type**: platform parser
      (example: `swagit_table` for `*.swagit.com` pages)
    - **llm**: Playwright + HtmlCleaner + OpenAI
    """
    service: ScheduleScrapeService = app.state.service
    try:
        return await service.scrape(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Scrape failed for %s", body.url)
        raise HTTPException(status_code=500, detail=f"Scrape failed: {exc}") from exc


@app.post("/scrape/raw")
async def scrape_schedule_raw(body: ScrapeScheduleRequest) -> JSONResponse:
    """Same as /scrape but meetings use display-name keys in a plain list."""
    service: ScheduleScrapeService = app.state.service
    try:
        result = await service.scrape(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Scrape failed for %s", body.url)
        raise HTTPException(status_code=500, detail=f"Scrape failed: {exc}") from exc

    return JSONResponse(
        {
            "meetings": [m.to_display_dict() for m in result.meetings],
            "meta": result.meta.model_dump(),
            "page_markdown": result.page_markdown,
        }
    )


def _map_stream_videos(items: list[dict]) -> list[StreamVideoInfo]:
    mapped: list[StreamVideoInfo] = []
    for item in items:
        mapped.append(
            StreamVideoInfo(
                status=item.get("status"),
                video_id=item["video_id"],
                video_title=item.get("video_title") or item.get("Meeting name") or "",
                meeting_link=item.get("meeting_link") or item.get("Meeting link"),
                scheduled_time=item.get("scheduled_time") or item.get("Scheduled time"),
                published_time=item.get("published_time"),
                started_streaming_on=item.get("started_streaming_on"),
                youtube_date_text=item.get("youtube_date_text"),
                note=item.get("note"),
                match_type=item.get("match_type"),
                match_confidence=item.get("match_confidence"),
            )
        )
    return mapped


@app.post("/stream-status", response_model=StreamStatusResponse)
async def stream_status(body: StreamStatusRequest) -> StreamStatusResponse:
    """
    Check whether a YouTube meeting is **live**, **upcoming**, or **concluded**.

    Metadata only (same signal as WallFly `get_live_videos` /
    `DetectEnd.ts_youtube`): scrapes the channel `/streams` page and reads
    Live-tab badges. Does **not** download or probe stream media.

    ``fetch_failed`` / ``unknown`` mean the page could not be read — callers
    must not treat those as a concluded meeting.
    """
    import asyncio

    try:
        raw = await asyncio.to_thread(
            check_youtube_stream_status,
            channel_url=str(body.channel_url),
            video_url=str(body.video_url) if body.video_url else None,
            video_id=body.video_id,
            timezone=body.timezone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Stream status check failed for %s", body.channel_url)
        raise HTTPException(
            status_code=500, detail=f"Stream status check failed: {exc}"
        ) from exc

    return StreamStatusResponse(
        status=raw["status"],
        video_id=raw.get("video_id"),
        video_title=raw.get("video_title"),
        meeting_link=raw.get("meeting_link"),
        scheduled_time=raw.get("scheduled_time"),
        published_time=raw.get("published_time"),
        started_streaming_on=raw.get("started_streaming_on"),
        live_videos=_map_stream_videos(raw.get("live_videos") or []),
        upcoming_videos=_map_stream_videos(raw.get("upcoming_videos") or []),
        concluded_on_page=_map_stream_videos(raw.get("concluded_on_page") or []),
        skipped_videos=_map_stream_videos(raw.get("skipped_videos") or []),
        channel_url=raw.get("channel_url") or str(body.channel_url),
        note=raw.get("note"),
        match_diagnostics=raw.get("match_diagnostics"),
    )
