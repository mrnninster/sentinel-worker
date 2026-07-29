"""Orchestrate URL → dedicated parser and/or LLM schedule extraction."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import pytz
from dateutil.parser import parse as parse_dt

from app.config import Settings, get_settings
from app.dedicated import parse_with_dedicated_scraper
from app.llm.schedule_extractor import ScheduleExtractor
from app.models import (
    Meeting,
    ScrapeMeta,
    ScrapeScheduleRequest,
    ScrapeScheduleResponse,
    YoutubeFallbackConfig,
)
from app.scraper.llm_page import scrape_to_llm_markdown
from app.scraper.page_fetcher import WaitUntil
from utils.meeting_title_filter import filter_meetings_by_category

log = logging.getLogger(__name__)


class ScheduleScrapeService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.extractor = ScheduleExtractor(
            api_key=self.settings.openai_api_key,
            model=self.settings.schedule_extraction_model,
            max_attempts=self.settings.schedule_extraction_max_attempts,
            timeout=self.settings.schedule_extraction_timeout,
            max_chars=self.settings.schedule_extraction_max_chars,
        )

    async def scrape(self, request: ScrapeScheduleRequest) -> ScrapeScheduleResponse:
        from schedule.library.youtube import clear_youtube_page_cache

        clear_youtube_page_cache()
        try:
            mode = self._resolve_mode(request)
            warnings: list[str] = []

            if mode == "dedicated":
                return await self._scrape_dedicated(request, warnings)
            return await self._scrape_llm(request, warnings)
        finally:
            clear_youtube_page_cache()

    def _resolve_mode(self, request: ScrapeScheduleRequest) -> str:
        if request.mode == "llm":
            return "llm"
        if request.mode == "dedicated":
            if not request.schedule_type:
                raise ValueError("schedule_type is required when mode=dedicated")
            return "dedicated"
        # auto
        if request.schedule_type:
            return "dedicated"
        return "llm"

    async def _scrape_dedicated(
        self,
        request: ScrapeScheduleRequest,
        warnings: list[str],
    ) -> ScrapeScheduleResponse:
        assert request.schedule_type
        url = str(request.url)
        agenda = str(request.agenda_url) if request.agenda_url else None

        primary_ok = True
        primary_error: Optional[str] = None
        raw_meetings: list[dict[str, Any]] = []

        try:
            raw_meetings = await parse_with_dedicated_scraper(
                url=url,
                schedule_type=request.schedule_type,
                timezone=request.timezone,
                agenda_url=agenda,
            )
        except Exception as exc:
            primary_ok = False
            primary_error = str(exc)
            warnings.append(f"Primary dedicated scrape failed: {exc}")
            log.warning("Primary dedicated scrape failed for %s: %s", url, exc)

        if not request.include_past:
            raw_meetings = self._filter_past(raw_meetings, request.timezone)

        # Empty dedicated result is a soft failure for YouTube fallback purposes
        if primary_ok and not raw_meetings:
            primary_ok = False
            primary_error = primary_error or "Primary scrape returned no meetings"
            warnings.append(primary_error)

        mode_used = "dedicated"
        youtube_used = False
        youtube_channel_url: Optional[str] = None

        if request.youtube_fallback:
            raw_meetings, yt_meta = await self._apply_youtube_fallback(
                raw_meetings,
                request.youtube_fallback,
                timezone=request.timezone,
                primary_empty=not bool(raw_meetings),
                warnings=warnings,
            )
            youtube_used = yt_meta["youtube_used"]
            youtube_channel_url = yt_meta["channel_url"]
            if youtube_used:
                mode_used = "dedicated+youtube_fallback"

        raw_meetings = self._apply_category_filter(request, raw_meetings, warnings)

        meetings = [Meeting.model_validate(m) for m in raw_meetings]
        return ScrapeScheduleResponse(
            meetings=meetings,
            meta=ScrapeMeta(
                url=url,
                timezone=request.timezone,
                mode_used=mode_used,
                schedule_type=request.schedule_type,
                meeting_count=len(meetings),
                primary_ok=primary_ok,
                primary_error=primary_error,
                youtube_used=youtube_used,
                youtube_channel_url=youtube_channel_url,
                warnings=warnings,
            ),
        )

    async def _scrape_llm(
        self,
        request: ScrapeScheduleRequest,
        warnings: list[str],
    ) -> ScrapeScheduleResponse:
        url = str(request.url)
        wait = (
            request.wait
            if request.wait is not None
            else self.settings.scrape_wait_seconds
        )
        wait_until: WaitUntil = (  # type: ignore[assignment]
            request.wait_until or self.settings.scrape_wait_until
        )

        primary_ok = True
        primary_error: Optional[str] = None
        cleaned = None
        raw_meetings: list[dict[str, Any]] = []
        extraction_model = None
        extraction_attempts = None

        try:
            cleaned = await scrape_to_llm_markdown(
                url,
                wait=wait,
                wait_for_selector=request.wait_for_selector,
                wait_until=wait_until,
                navigation_timeout_ms=self.settings.scrape_navigation_timeout_ms,
                keep_links=request.keep_links,
                keep_images=False,
            )

            if not cleaned.markdown.strip():
                warnings.append("Cleaned page Markdown was empty")

            extraction = await self.extractor.extract(
                cleaned.markdown,
                page_url=cleaned.final_url or url,
                timezone_name=request.timezone,
            )
            if extraction.errors:
                warnings.extend(extraction.errors)
                primary_ok = False
                primary_error = "; ".join(extraction.errors)

            raw_meetings = extraction.meetings
            extraction_model = extraction.model_used
            extraction_attempts = extraction.attempts
        except Exception as exc:
            primary_ok = False
            primary_error = str(exc)
            warnings.append(f"Primary LLM scrape failed: {exc}")
            log.warning("Primary LLM scrape failed for %s: %s", url, exc)

        if not request.include_past:
            raw_meetings = self._filter_past(raw_meetings, request.timezone)

        if primary_ok and not raw_meetings:
            primary_ok = False
            primary_error = primary_error or "Primary scrape returned no meetings"
            warnings.append(primary_error)

        mode_used = "llm"
        youtube_used = False
        youtube_channel_url: Optional[str] = None

        if request.youtube_fallback:
            raw_meetings, yt_meta = await self._apply_youtube_fallback(
                raw_meetings,
                request.youtube_fallback,
                timezone=request.timezone,
                primary_empty=not bool(raw_meetings),
                warnings=warnings,
            )
            youtube_used = yt_meta["youtube_used"]
            youtube_channel_url = yt_meta["channel_url"]
            if youtube_used:
                mode_used = "llm+youtube_fallback"

        raw_meetings = self._apply_category_filter(request, raw_meetings, warnings)

        meetings = [Meeting.model_validate(m) for m in raw_meetings]
        return ScrapeScheduleResponse(
            meetings=meetings,
            meta=ScrapeMeta(
                url=url,
                timezone=request.timezone,
                mode_used=mode_used,
                schedule_type=request.schedule_type,
                meeting_count=len(meetings),
                raw_html_bytes=cleaned.raw_bytes if cleaned else None,
                cleaned_markdown_bytes=(
                    cleaned.cleaned_markdown_bytes if cleaned else None
                ),
                token_reduction_pct=cleaned.token_reduction_pct if cleaned else None,
                model_used=extraction_model,
                extraction_attempts=extraction_attempts,
                primary_ok=primary_ok,
                primary_error=primary_error,
                youtube_used=youtube_used,
                youtube_channel_url=youtube_channel_url,
                warnings=warnings,
            ),
            page_markdown=(
                cleaned.markdown
                if cleaned and request.include_page_markdown
                else None
            ),
        )

    async def _apply_youtube_fallback(
        self,
        meetings: list[dict[str, Any]],
        fallback: YoutubeFallbackConfig,
        *,
        timezone: str,
        primary_empty: bool,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from schedule.library.youtube import Youtube

        channel_url = str(fallback.channel_url)
        yt = Youtube()

        def _run():
            return yt.apply_schedule_fallback(
                meetings,
                channel_url=channel_url,
                timezone=timezone,
                on_primary_failure=fallback.on_primary_failure,
                match=fallback.match,
                primary_empty=primary_empty,
            )

        try:
            merged = await asyncio.to_thread(_run)
        except Exception as exc:
            warnings.append(f"YouTube fallback failed: {exc}")
            log.exception("YouTube fallback failed for %s", channel_url)
            return meetings, {
                "youtube_used": False,
                "channel_url": channel_url,
            }

        for note in merged.get("notes") or []:
            warnings.append(note)

        return merged["meetings"], {
            "youtube_used": merged.get("youtube_used", False),
            "channel_url": channel_url,
        }

    @staticmethod
    def _apply_category_filter(
        request: ScrapeScheduleRequest,
        meetings: list[dict[str, Any]],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        kept, notes = filter_meetings_by_category(
            meetings,
            threshold=request.category_match_threshold,
            enabled=request.filter_by_categories,
        )
        # Keep summary notes; omit per-title drop spam beyond a few samples
        for note in notes:
            if note.startswith("category_filter") or note.startswith("filtered title"):
                if note.startswith("filtered title") and sum(
                    1 for w in warnings if w.startswith("filtered title")
                ) >= 5:
                    continue
                warnings.append(note)
            else:
                warnings.append(note)
        return kept

    @staticmethod
    def _filter_past(meetings: list[dict], timezone_name: str) -> list[dict]:
        try:
            tz = pytz.timezone(timezone_name)
        except Exception:
            return meetings

        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        kept: list[dict] = []
        for meeting in meetings:
            raw = meeting.get("Scheduled time")
            if not raw:
                continue
            try:
                when = parse_dt(raw)
                if when.tzinfo is None:
                    when = tz.localize(when)
                if when >= start:
                    kept.append(meeting)
            except Exception:
                kept.append(meeting)
        return kept
