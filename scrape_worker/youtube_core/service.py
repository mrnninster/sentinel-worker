"""YouTube channel snapshot, calendar fallback, and stream-status service."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import pytz
from dateutil import parser

from youtube_core.client import (
    YtInitialDataClient,
    cache_key_for_url,
    channel_tab_url,
    clear_cache,
    get_classify_cache,
    get_page_cache_lock,
)
from youtube_core.matching import (
    card_local_date,
    match_meeting_to_card,
    title_match_details,
)
from youtube_core.models import ChannelSnapshot, StreamCard
from youtube_core.parser import StreamCardParser

log = logging.getLogger(__name__)

_DEFAULT_MAX_MEETING_AGE_HOURS = 24.0
_STATUS_LABELS = {
    "live": "Live",
    "upcoming": "Upcoming",
    "concluded": "Concluded",
}


def clear_youtube_page_cache() -> None:
    clear_cache()


class YouTubeService:
    """Unified YouTube scrape / fallback / status operations."""

    def __init__(self) -> None:
        self.client = YtInitialDataClient()
        self.parser = StreamCardParser()
        self.meetings: list = []
        self.scraper = None
        self.self_contained_parser = True

    # --- thin client proxies used by adapters / internal helpers ---
    def _fetch_youtube_initial_data(self, url: str):
        return self.client._fetch_youtube_initial_data(url)

    def _fetch_youtube_initial_data_many(self, urls: list[str]):
        return self.client._fetch_youtube_initial_data_many(urls)

    def _fetch_with_html_scraper(self, url: str):
        return self.client._fetch_with_html_scraper(url)

    def _live_tab_items(self, data):
        return self.client._live_tab_items(data)

    def _videos_tab_items(self, data):
        return self.client._videos_tab_items(data)

    @staticmethod
    def channel_tab_url(channel_url: str, tab: str) -> str:
        return channel_tab_url(channel_url, tab)

    @staticmethod
    def normalize_channel_base_url(channel_url: str) -> str:
        from youtube_core.client import normalize_channel_base_url
        return normalize_channel_base_url(channel_url)

    def _classify_rich_item(self, content, timezone="America/New_York"):
        return self.parser._classify_rich_item(content, timezone)

    def _extract_yt_initial_data(self, html: str):
        return self.client._extract_yt_initial_data(html)

    def get_live_started_at(self, video_id: str):
        if not video_id:
            return None
        url = f"https://www.youtube.com/watch?v={video_id}"
        yt_data = self._fetch_youtube_initial_data(url)
        started = self.parser._extract_started_streaming_from_yt_data(yt_data)
        if started:
            return started
        try:
            html = self._fetch_with_html_scraper(url)
            if html:
                return self.parser.parse_started_streaming_text(html)
        except Exception:
            log.debug("HTML fallback for started-streaming failed video_id=%s", video_id)
        return None

    def _filter_stale_live_items(self, live_items: list[dict]) -> tuple[list[dict], list[dict]]:
        kept: list[dict] = []
        skipped: list[dict] = []
        max_hours = self.parser._max_live_age_hours()
        for item in live_items:
            video_id = item.get("video_id")
            started = self.get_live_started_at(video_id) if video_id else None
            if started and self.parser.is_stale_live(started, max_age_hours=max_hours):
                note = (
                    f"Skipped live stream older than {max_hours:g}h "
                    f"(Started streaming on {started.astimezone(pytz.UTC).strftime('%b %d, %Y')})"
                )
                log.info(
                    "Skipping stale live video_id=%s started=%s age_limit_h=%s",
                    video_id,
                    started.isoformat(),
                    max_hours,
                )
                skipped.append(
                    {
                        **item,
                        "status": "skipped",
                        "started_streaming_on": started.astimezone(pytz.UTC).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "note": note,
                    }
                )
                continue
            if started:
                item = {
                    **item,
                    "started_streaming_on": started.astimezone(pytz.UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            kept.append(item)
        return kept, skipped

    @staticmethod
    def extract_video_id(value: str | None) -> str | None:
        """Extract an 11-char YouTube video id from a URL or bare id."""
        if not value:
            return None
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
            return value
        patterns = [
            r"[?&]v=([A-Za-z0-9_-]{11})",
            r"youtu\.be/([A-Za-z0-9_-]{11})",
            r"/live/([A-Za-z0-9_-]{11})",
            r"/shorts/([A-Za-z0-9_-]{11})",
            r"/embed/([A-Za-z0-9_-]{11})",
        ]
        for pat in patterns:
            m = re.search(pat, value)
            if m:
                return m.group(1)
        return None
    _STATUS_LABELS = {
        "live": "Live",
        "upcoming": "Upcoming",
        "concluded": "Concluded",
    }

    @classmethod
    def stream_item_local_date(cls, item: dict, timezone: str):
        """Local date from ytInitialData fields; never inferred from the title."""
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC

        for key in ("scheduled_time", "started_streaming_on", "published_time"):
            raw = item.get(key)
            if not raw:
                continue
            try:
                when = parser.parse(str(raw))
                if when.tzinfo is None:
                    when = tz.localize(when)
                return when.astimezone(tz).date()
            except Exception:
                continue
        return None

    @classmethod
    def stream_item_to_meeting(cls, item: dict, timezone: str) -> dict:
        """Convert a classified stream card into a Bubble-shaped meeting dict."""
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC

        status_key = (item.get("status") or "concluded").lower()
        status = cls._STATUS_LABELS.get(status_key, "Concluded")
        title = (item.get("video_title") or "Meeting").strip()
        link = item.get("meeting_link") or (
            f"https://www.youtube.com/watch?v={item['video_id']}"
            if item.get("video_id")
            else None
        )

        scheduled = (
            item.get("scheduled_time")
            or item.get("started_streaming_on")
            or item.get("published_time")
        )
        if not scheduled:
            local_date = cls.stream_item_local_date(item, timezone)
            if status_key == "live":
                scheduled = datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif local_date:
                # Unknown clock time — use local noon as a stable stub
                local_dt = tz.localize(
                    datetime(local_date.year, local_date.month, local_date.day, 12, 0)
                )
                scheduled = local_dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                # Do not fabricate a date when ytInitialData did not provide one.
                scheduled = None

        return {
            "Meeting name": title,
            "Scheduled time": scheduled,
            "Meeting link": link,
            "Agenda link": None,
            "Status": status,
            "Stream type": "ts_youtube",
        }
    def classify_channel_streams(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Scrape a channel /streams (Live) page and classify cards.

        Returns:
            {
              "live": [...],
              "upcoming": [...],
              "concluded": [...],
              "channel_url": str,
            }
        """
        streams_url = self.channel_tab_url(channel_url, "streams")
        cache_key = (cache_key_for_url(streams_url), "streams", timezone)
        with get_page_cache_lock():
            cached = get_classify_cache().get(cache_key)
            if cached is not None:
                log.info("classify cache hit tab=streams url=%s", streams_url)
                return cached

        yt_data = self._fetch_youtube_initial_data(streams_url)
        result = {
            "live": [],
            "upcoming": [],
            "concluded": [],
            "skipped": [],
            "channel_url": streams_url,
            "fetch_ok": True,
            "fetch_error": None,
        }
        if not yt_data:
            log.warning("No ytInitialData while classifying %s", streams_url)
            result["fetch_ok"] = False
            result["fetch_error"] = "ytInitialData missing or unreadable"
            # Do not cache failures — the next monitor poll must retry.
            return result

        for item in self._live_tab_items(yt_data):
            classified = self._classify_rich_item(item, timezone)
            if not classified:
                continue
            result[classified["status"]].append(classified)

        kept_live, skipped = self._filter_stale_live_items(result["live"])
        result["live"] = kept_live
        result["skipped"] = skipped

        log.info(
            "Channel stream status live=%d upcoming=%d concluded=%d skipped=%d url=%s",
            len(result["live"]),
            len(result["upcoming"]),
            len(result["concluded"]),
            len(result["skipped"]),
            streams_url,
        )
        with get_page_cache_lock():
            get_classify_cache()[cache_key] = result
        return result

    def classify_channel_videos(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Scrape a channel /videos page. Cards are treated as concluded VODs
        (duration badges, not Live/Upcoming).
        """
        videos_url = self.channel_tab_url(channel_url, "videos")
        cache_key = (cache_key_for_url(videos_url), "videos", timezone)
        with get_page_cache_lock():
            cached = get_classify_cache().get(cache_key)
            if cached is not None:
                log.info("classify cache hit tab=videos url=%s", videos_url)
                return cached

        yt_data = self._fetch_youtube_initial_data(videos_url)
        result = {
            "live": [],
            "upcoming": [],
            "concluded": [],
            "channel_url": videos_url,
        }
        if not yt_data:
            log.warning("No ytInitialData while classifying videos %s", videos_url)
            with get_page_cache_lock():
                get_classify_cache()[cache_key] = result
            return result

        for item in self._videos_tab_items(yt_data):
            classified = self._classify_rich_item(item, timezone)
            if not classified:
                continue
            # Videos tab: force concluded unless explicitly live/upcoming
            if classified["status"] not in ("live", "upcoming"):
                classified = {**classified, "status": "concluded", "source_tab": "videos"}
            else:
                classified = {**classified, "source_tab": "videos"}
            result[classified["status"]].append(classified)

        log.info(
            "Channel videos status live=%d upcoming=%d concluded=%d url=%s",
            len(result["live"]),
            len(result["upcoming"]),
            len(result["concluded"]),
            videos_url,
        )
        with get_page_cache_lock():
            get_classify_cache()[cache_key] = result
        return result

    def classify_channel_for_fallback(
        self, channel_url: str, timezone: str = "America/New_York"
    ) -> dict:
        """
        Merge Live (/streams) + Videos (/videos) tabs for schedule fallback.

        Each tab URL is fetched at most once (shared page cache; both tabs can
        load in one Playwright browser when neither is cached yet).
        """
        streams_url = self.channel_tab_url(channel_url, "streams")
        videos_url = self.channel_tab_url(channel_url, "videos")
        # Prefetch any missing tabs in a single browser session.
        self._fetch_youtube_initial_data_many([streams_url, videos_url])

        streams = self.classify_channel_streams(channel_url, timezone=timezone)
        videos = self.classify_channel_videos(channel_url, timezone=timezone)

        seen: set[str] = set()
        for bucket in ("live", "upcoming", "concluded"):
            for item in streams[bucket]:
                item.setdefault("source_tab", "streams")
                if item.get("video_id"):
                    seen.add(item["video_id"])

        merged = {
            "live": list(streams["live"]),
            "upcoming": list(streams["upcoming"]),
            "concluded": list(streams["concluded"]),
            "channel_url": streams.get("channel_url") or channel_url,
            "streams_url": streams.get("channel_url"),
            "videos_url": videos.get("channel_url"),
        }

        # Prefer streams live/upcoming if videos somehow has them
        for item in videos["live"] + videos["upcoming"]:
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            merged[item["status"]].append(item)
            seen.add(vid)

        for item in videos["concluded"]:
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            merged["concluded"].append(item)
            seen.add(vid)

        log.info(
            "Fallback channel merge live=%d upcoming=%d concluded=%d "
            "(streams=%s videos=%s)",
            len(merged["live"]),
            len(merged["upcoming"]),
            len(merged["concluded"]),
            merged.get("streams_url"),
            merged.get("videos_url"),
        )
        return merged
    def apply_schedule_fallback(
        self,
        meetings: list[dict],
        *,
        channel_url: str,
        timezone: str = "America/New_York",
        on_primary_failure: str = "same_day_stub",
        match: str = "title_date",
        require_title_match: bool = False,
        primary_empty: bool = False,
        title_match_threshold: float = 0.3,
        max_meeting_age_hours: float | None = _DEFAULT_MAX_MEETING_AGE_HOURS,
    ) -> dict:
        """
        Merge YouTube /streams + /videos cards into primary schedule meetings.

        Returns:
            {
              "meetings": [...],
              "youtube_used": bool,
              "notes": [str],
              "live_count": int,
              "upcoming_count": int,
              "concluded_count": int,
            }
        """
        notes: list[str] = []
        if on_primary_failure == "skip":
            return {
                "meetings": meetings,
                "youtube_used": False,
                "notes": ["youtube_fallback skipped (on_primary_failure=skip)"],
                "live_count": 0,
                "upcoming_count": 0,
                "concluded_count": 0,
            }

        classified = self.classify_channel_for_fallback(channel_url, timezone=timezone)
        all_items = (
            list(classified["live"])
            + list(classified["upcoming"])
            + list(classified["concluded"])
        )
        notes.append(
            f"youtube snapshot live={len(classified['live'])} "
            f"upcoming={len(classified['upcoming'])} "
            f"concluded={len(classified['concluded'])} "
            f"(streams+videos)"
        )
        if classified.get("streams_url"):
            notes.append(f"streams_url={classified['streams_url']}")
        if classified.get("videos_url"):
            notes.append(f"videos_url={classified['videos_url']}")
        if require_title_match:
            notes.append(
                f"require_title_match=true threshold={title_match_threshold}"
            )

        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC
        now_local = datetime.now(tz)
        today = now_local.date()
        max_age_hours = (
            None
            if max_meeting_age_hours is None or max_meeting_age_hours <= 0
            else float(max_meeting_age_hours)
        )
        if max_age_hours is not None:
            notes.append(f"max_meeting_age_hours={max_age_hours:g}")

        # Working copy
        result = [dict(m) for m in meetings]
        matched_video_ids: set[str] = set()

        def meeting_video_id(meeting: dict) -> str | None:
            for key in ("Meeting link", "meeting_link", "user_live_link", "user_archive_link"):
                vid = self.extract_video_id(meeting.get(key))
                if vid:
                    return vid
            raw_vid = meeting.get("video_id")
            if isinstance(raw_vid, str) and raw_vid.strip():
                return raw_vid.strip()
            return None

        def meeting_local_date(meeting: dict):
            raw = meeting.get("Scheduled time") or meeting.get("scheduled_time")
            if not raw:
                return None
            try:
                when = parser.parse(raw)
                if when.tzinfo is None:
                    when = tz.localize(when)
                return when.astimezone(tz).date()
            except Exception:
                return None

        def meeting_local_datetime(meeting: dict):
            raw = meeting.get("Scheduled time") or meeting.get("scheduled_time")
            if not raw:
                return None
            try:
                when = parser.parse(raw)
                if when.tzinfo is None:
                    when = tz.localize(when)
                return when.astimezone(tz)
            except Exception:
                return None

        def meeting_too_old(meeting: dict) -> bool:
            """Block date-based attach onto meetings that finished long ago.

            Exact video_id matches bypass this so a known video can still have
            its status refreshed.
            """
            if max_age_hours is None:
                return False
            when = meeting_local_datetime(meeting)
            if when is None:
                return False
            return when < now_local - timedelta(hours=max_age_hours)

        def meeting_title(meeting: dict) -> str:
            return str(
                meeting.get("Meeting name")
                or meeting.get("meeting_name")
                or meeting.get("title")
                or ""
            ).strip()

        def title_match(meeting: dict, item: dict) -> tuple[float, str | None]:
            return title_match_details(
                meeting_title(meeting),
                str(item.get("video_title") or ""),
                jaccard_threshold=title_match_threshold,
            )

        def titles_match(meeting: dict, item: dict) -> bool:
            if not require_title_match:
                return True
            _, match_type = title_match(meeting, item)
            return match_type is not None

        def apply_hit(meeting: dict, hit: dict, *, how: str) -> None:
            confidence, match_type = (
                title_match(meeting, hit) if require_title_match else (None, None)
            )
            matched_video_ids.add(hit["video_id"])
            label = self._STATUS_LABELS.get(hit["status"], "Concluded")
            meeting["Status"] = label
            meeting["Meeting link"] = hit.get("meeting_link") or meeting.get(
                "Meeting link"
            )
            meeting["video_id"] = hit["video_id"]
            meeting["Stream type"] = meeting.get("Stream type") or "ts_youtube"
            if hit.get("scheduled_time") and not meeting.get("Scheduled time"):
                meeting["Scheduled time"] = hit["scheduled_time"]
            conf_note = (
                f" title_match={match_type}:{confidence:.2f}"
                if confidence is not None
                else ""
            )
            notes.append(
                f"{how} video_id={hit['video_id']} → Status={label} "
                f"(tab={hit.get('source_tab', '?')}){conf_note}"
            )

        # Overlay onto existing meetings
        for meeting in result:
            vid = meeting_video_id(meeting)
            hit = None
            if vid:
                for item in all_items:
                    if item.get("video_id") == vid:
                        hit = item
                        break
            if hit is None and match == "title_date" and meeting_too_old(meeting):
                notes.append(
                    f"skip date attach for {meeting_title(meeting)!r} "
                    f"(older than {max_age_hours:g}h)"
                )
            elif hit is None and match == "title_date":
                mdate = meeting_local_date(meeting)
                if mdate:
                    # Prefer live > upcoming > concluded for same day
                    best_score = -1.0
                    for bucket in ("live", "upcoming", "concluded"):
                        for item in classified[bucket]:
                            if item.get("video_id") in matched_video_ids:
                                continue
                            if self.stream_item_local_date(item, timezone) != mdate:
                                continue
                            if not titles_match(meeting, item):
                                continue
                            if require_title_match:
                                score, _ = title_match(meeting, item)
                                # Prefer earlier buckets; within a bucket pick best title.
                                bucket_bonus = {
                                    "live": 3.0,
                                    "upcoming": 2.0,
                                    "concluded": 1.0,
                                }[bucket]
                                ranked = bucket_bonus + score
                                if ranked > best_score:
                                    best_score = ranked
                                    hit = item
                            else:
                                hit = item
                                break
                        if hit and not require_title_match:
                            break

            if not hit:
                continue

            apply_hit(meeting, hit, how="overlay")

        # Same-day stubs for live / upcoming / today's concluded VODs.
        # When require_title_match and the calendar already returned meetings,
        # the channel may only attach (title+date) — not invent rows for days
        # that already have calendar meetings. Stubs still fill empty days, and
        # when primary_empty the channel remains the meeting source.
        if on_primary_failure == "same_day_stub":
            stub_candidates = []
            stub_candidates.extend(classified["live"])
            stub_candidates.extend(classified["upcoming"])
            for item in classified["concluded"]:
                item_date = self.stream_item_local_date(item, timezone)
                if item_date == today:
                    stub_candidates.append(item)

            existing_dates = {
                d for d in (meeting_local_date(m) for m in result) if d is not None
            }
            existing_vids = {meeting_video_id(m) for m in result}
            existing_vids.discard(None)
            existing_vids |= matched_video_ids

            for item in stub_candidates:
                vid = item.get("video_id")
                if not vid or vid in existing_vids:
                    continue
                item_date = self.stream_item_local_date(item, timezone)
                # For live/upcoming without a date, treat as today
                if item_date is None and item.get("status") in ("live", "upcoming"):
                    item_date = today
                if item_date is not None and item_date in existing_dates:
                    # Already have a primary row for that day — overlay / attach
                    same_day = [
                        m
                        for m in result
                        if meeting_local_date(m) == item_date
                        and not meeting_too_old(m)
                    ]
                    if not same_day:
                        notes.append(
                            f"skip same-day attach video_id={vid} "
                            f"(meetings on {item_date} older than {max_age_hours:g}h)"
                        )
                        continue
                    if require_title_match:
                        best_meeting = None
                        best_score = title_match_threshold
                        for meeting in same_day:
                            # Skip meetings that already have a video
                            if meeting_video_id(meeting):
                                continue
                            score, match_type = title_match(meeting, item)
                            if match_type is not None and score >= best_score:
                                best_score = score
                                best_meeting = meeting
                        if best_meeting is None:
                            notes.append(
                                f"skip same-day attach video_id={vid} "
                                f"(no title match on {item_date})"
                            )
                            continue
                        apply_hit(
                            best_meeting,
                            item,
                            how="same-day attach",
                        )
                        existing_vids.add(vid)
                        continue

                    for meeting in same_day:
                        meeting["Status"] = self._STATUS_LABELS.get(
                            item["status"], "Concluded"
                        )
                        meeting["Meeting link"] = item.get("meeting_link")
                        meeting["video_id"] = vid
                        meeting["Stream type"] = (
                            meeting.get("Stream type") or "ts_youtube"
                        )
                        existing_vids.add(vid)
                        notes.append(
                            f"same-day attach video_id={vid} → "
                            f"{meeting.get('Meeting name')} "
                            f"(tab={item.get('source_tab', '?')})"
                        )
                        break
                    continue

                stub = self.stream_item_to_meeting(item, timezone)
                result.append(stub)
                existing_vids.add(vid)
                if item_date:
                    existing_dates.add(item_date)
                notes.append(
                    f"stub video_id={vid} status={stub['Status']} date={item_date} "
                    f"(tab={item.get('source_tab', '?')})"
                )

        elif on_primary_failure == "status_only" and primary_empty:
            notes.append(
                "status_only with empty primary — no meetings to overlay; "
                "no stubs created"
            )

        return {
            "meetings": result,
            "youtube_used": True,
            "notes": notes,
            "live_count": len(classified["live"]),
            "upcoming_count": len(classified["upcoming"]),
            "concluded_count": len(classified["concluded"]),
        }
    def get_live_videos(self, channel_url: str | None = None, soup=None) -> list[dict]:
        """
        WallFly-compatible live list (metadata only).

        Prefer ``channel_url`` (Playwright + modern UI). ``soup`` is accepted for
        callers that already fetched HTML (legacy path).
        """
        if channel_url:
            classified = self.classify_channel_streams(channel_url)
            return [
                {"video_id": v["video_id"], "video_title": v["video_title"]}
                for v in classified["live"]
            ]

        if soup is None:
            return []
        # Legacy soup → ytInitialData parse
        html = str(soup)
        yt_data = self._extract_yt_initial_data(html)
        if not yt_data:
            return []
        live = []
        for item in self._live_tab_items(yt_data):
            classified = self._classify_rich_item(item)
            if classified and classified["status"] == "live":
                live.append(
                    {
                        "video_id": classified["video_id"],
                        "video_title": classified["video_title"],
                    }
                )
        return live

    def check_stream_status(
        self,
        *,
        channel_url: str,
        video_id: str | None = None,
        video_url: str | None = None,
        timezone: str = "America/New_York",
    ) -> dict:
        """
        Check whether a YouTube stream is live, upcoming, or concluded.

        Same signal WallFly uses for DetectStart/DetectEnd.ts_youtube:
        presence of the video on the Live tab with a LIVE badge = live;
        absence of a previously-known live id = concluded.
        Does **not** download or probe HLS/media.
        """
        # Monitor polls must always re-fetch — a warm page/classify cache would
        # freeze status at the first snapshot and never reach "concluded".
        clear_youtube_page_cache()

        vid = video_id or self.extract_video_id(video_url)
        classified = self.classify_channel_streams(channel_url, timezone=timezone)

        if not classified.get("fetch_ok", True):
            return {
                "status": "fetch_failed",
                "video_id": vid,
                "video_title": None,
                "meeting_link": (
                    f"https://www.youtube.com/watch?v={vid}" if vid else None
                ),
                "live_videos": [],
                "upcoming_videos": [],
                "concluded_on_page": [],
                "skipped_videos": [],
                "channel_url": channel_url,
                "note": classified.get("fetch_error")
                or "Could not load YouTube channel page; status unknown",
            }

        live_ids = {v["video_id"]: v for v in classified["live"]}
        upcoming_ids = {v["video_id"]: v for v in classified["upcoming"]}
        concluded_ids = {v["video_id"]: v for v in classified["concluded"]}
        skipped_ids = {v["video_id"]: v for v in classified.get("skipped") or []}

        if not vid:
            return {
                "status": "channel_snapshot",
                "video_id": None,
                "video_title": None,
                "meeting_link": None,
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
            }

        if vid in skipped_ids:
            hit = skipped_ids[vid]
            return {
                "status": "skipped",
                "video_id": vid,
                "video_title": hit.get("video_title"),
                "meeting_link": hit.get("meeting_link"),
                "started_streaming_on": hit.get("started_streaming_on"),
                "published_time": hit.get("published_time"),
                "note": hit.get("note")
                or "Live stream has been running longer than 24 hours; skipped",
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
                "match_diagnostics": {
                    "mode": "video_id",
                    "looked_up": vid,
                    "found": True,
                    "bucket": "skipped",
                },
            }

        if vid in live_ids:
            hit = live_ids[vid]
            return {
                "status": "live",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "started_streaming_on": hit.get("started_streaming_on"),
                "published_time": hit.get("published_time"),
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
                "match_diagnostics": {
                    "mode": "video_id",
                    "looked_up": vid,
                    "found": True,
                    "bucket": "live",
                },
            }
        if vid in upcoming_ids:
            hit = upcoming_ids[vid]
            return {
                "status": "upcoming",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "scheduled_time": hit.get("scheduled_time"),
                "published_time": hit.get("published_time"),
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
                "match_diagnostics": {
                    "mode": "video_id",
                    "looked_up": vid,
                    "found": True,
                    "bucket": "upcoming",
                },
            }
        if vid in concluded_ids:
            hit = concluded_ids[vid]
            return {
                "status": "concluded",
                "video_id": vid,
                "video_title": hit["video_title"],
                "meeting_link": hit["meeting_link"],
                "published_time": hit.get("published_time"),
                "live_videos": classified["live"],
                "upcoming_videos": classified["upcoming"],
                "concluded_on_page": classified["concluded"],
                "skipped_videos": classified.get("skipped") or [],
                "channel_url": channel_url,
                "match_diagnostics": {
                    "mode": "video_id",
                    "looked_up": vid,
                    "found": True,
                    "bucket": "concluded",
                },
            }

        # Explicit video-id continuity: the monitor owns this id. Absence from a
        # successfully fetched Live tab means DetectEnd (concluded), not unknown.
        candidates = [
            *(classified.get("live") or []),
            *(classified.get("upcoming") or []),
            *(classified.get("concluded") or []),
        ]
        return {
            "status": "concluded",
            "video_id": vid,
            "video_title": None,
            "meeting_link": f"https://www.youtube.com/watch?v={vid}",
            "live_videos": classified["live"],
            "upcoming_videos": classified["upcoming"],
            "concluded_on_page": classified["concluded"],
            "skipped_videos": classified.get("skipped") or [],
            "channel_url": channel_url,
            "match_diagnostics": {
                "mode": "video_id",
                "looked_up": vid,
                "found": False,
                "candidate_count": len(candidates),
                "candidate_ids": [c.get("video_id") for c in candidates[:20]],
            },
            "note": (
                "Video id not found among Live-tab cards; treated as concluded "
                "(not currently live), matching WallFly DetectEnd.ts_youtube."
            ),
        }

