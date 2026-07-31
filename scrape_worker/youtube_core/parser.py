"""Parse ytInitialData cards into StreamCard models. Dates never come from titles."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import pytz
from dateutil import parser as date_parser

from youtube_core.models import StreamCard

log = logging.getLogger(__name__)

_SCHEDULED_FOR_RE = re.compile(r"Scheduled for\s+(.+)", re.IGNORECASE)
_STARTED_STREAMING_ON_RE = re.compile(
    r"Started streaming on\s+(.+?)(?:\s*[·|]|\s*$)",
    re.IGNORECASE,
)
_STARTED_STREAMING_AGO_RE = re.compile(
    r"Started streaming\s+(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_DEFAULT_MAX_LIVE_AGE_HOURS = 24.0
_YOUTUBE_RELATIVE_DATE_RE = re.compile(
    r"\b(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)
_YOUTUBE_PAGE_DATE_PREFIX_RE = re.compile(
    r"^(?:scheduled for|started streaming on|streamed live on|streamed on|"
    r"streamed live|streamed|premiered on|premiered|published on|published)\s+",
    re.IGNORECASE,
)


class StreamCardParser:
    """Modern lockupViewModel + legacy videoRenderer parser."""

    @staticmethod
    def _lockup_badge_text(lockup: dict) -> str:
        overlays = (
            ((lockup.get("contentImage") or {}).get("thumbnailViewModel") or {}).get(
                "overlays"
            )
            or []
        )
        for overlay in overlays:
            badges = (overlay.get("thumbnailBottomOverlayViewModel") or {}).get(
                "badges"
            ) or []
            for badge in badges:
                text = (badge.get("thumbnailBadgeViewModel") or {}).get("text")
                if text:
                    return str(text).strip()
        return ""

    @staticmethod
    def _text_value(value) -> str:
        """Read simpleText/content/runs text from an ytInitialData value."""
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, dict):
            return ""
        direct = value.get("simpleText") or value.get("content")
        if isinstance(direct, str):
            return direct.strip()
        runs = value.get("runs") or []
        return "".join(
            str(run.get("text") or "") for run in runs if isinstance(run, dict)
        ).strip()

    @classmethod
    def _lockup_metadata_texts(cls, meta_vm: dict) -> list[str]:
        """All visible metadata strings from a modern channel card."""
        texts: list[str] = []
        try:
            rows = (
                ((meta_vm.get("metadata") or {}).get("contentMetadataViewModel") or {})
                .get("metadataRows")
                or []
            )
            for row in rows:
                for part in row.get("metadataParts") or []:
                    content = cls._text_value(part.get("text"))
                    if content:
                        texts.append(content)
        except Exception:
            return texts
        return texts

    @classmethod
    def _lockup_schedule_text(cls, meta_vm: dict) -> str:
        for content in cls._lockup_metadata_texts(meta_vm):
            if content.lower().startswith("scheduled for"):
                return content
        return ""

    @staticmethod
    def _is_youtube_page_date_text(text: str) -> bool:
        """Whether a card metadata string represents YouTube's own date."""
        cleaned = (text or "").strip().lower()
        if not cleaned:
            return False
        return (
            cleaned.startswith(
                (
                    "scheduled for ",
                    "started streaming ",
                    "streamed ",
                    "streamed live ",
                    "premiered ",
                    "published ",
                )
            )
            or bool(_YOUTUBE_RELATIVE_DATE_RE.fullmatch(cleaned))
        )

    @classmethod
    def _lockup_page_date_text(cls, meta_vm: dict) -> str:
        for content in cls._lockup_metadata_texts(meta_vm):
            if cls._is_youtube_page_date_text(content):
                return content
        return ""

    @classmethod
    def _legacy_page_date_text(cls, video_data: dict) -> str:
        return cls._text_value(video_data.get("publishedTimeText"))

    @staticmethod
    def _parse_scheduled_for(text: str, timezone: str):
        cleaned = (text or "").replace("\u202f", " ").replace("\xa0", " ").strip()
        # also handle actual unicode narrow nbsp if present as char
        cleaned = cleaned.replace("\u202f", " ")
        cleaned = cleaned.replace(" ", " ").replace(" ", " ")
        match = _SCHEDULED_FOR_RE.search(cleaned)
        if not match:
            return None
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.timezone("America/New_York")
        try:
            local_dt = date_parser.parse(match.group(1), fuzzy=True)
            if local_dt.tzinfo is None:
                local_dt = tz.localize(local_dt)
            return local_dt.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            log.warning("Could not parse YouTube schedule text %r", text)
            return None

    @staticmethod
    def _parse_youtube_page_time(
        text: str,
        timezone: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Parse the date displayed by YouTube on a channel card into UTC ISO.

        This consumes ytInitialData metadata only. It intentionally never reads
        a date from the video title.
        """
        cleaned = (
            (text or "")
            .replace("\u202f", " ")
            .replace("\xa0", " ")
            .replace(" ", " ")
            .replace(" ", " ")
            .strip()
        )
        if not cleaned:
            return None

        current = now or datetime.now(pytz.UTC)
        if current.tzinfo is None:
            current = pytz.UTC.localize(current)
        else:
            current = current.astimezone(pytz.UTC)

        relative = _YOUTUBE_RELATIVE_DATE_RE.search(cleaned)
        if relative:
            amount = int(relative.group(1))
            unit = relative.group(2).lower()
            delta = {
                "minute": timedelta(minutes=amount),
                "hour": timedelta(hours=amount),
                "day": timedelta(days=amount),
                "week": timedelta(weeks=amount),
                "month": timedelta(days=30 * amount),
                "year": timedelta(days=365 * amount),
            }[unit]
            return (current - delta).strftime("%Y-%m-%dT%H:%M:%SZ")

        chunk = _YOUTUBE_PAGE_DATE_PREFIX_RE.sub("", cleaned, count=1).strip()
        if not chunk or chunk == cleaned:
            return None
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC
        try:
            # YouTube's absolute card date often has no clock. Noon preserves
            # the displayed local calendar date across UTC conversion.
            local_default = datetime(current.year, 1, 1, 12, 0, 0)
            parsed = date_parser.parse(chunk, fuzzy=True, default=local_default)
            if parsed.tzinfo is None:
                parsed = tz.localize(parsed)
            return parsed.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            log.debug("Could not parse YouTube page date text %r", text)
            return None

    @classmethod
    def _published_page_fields(cls, text: str, timezone: str) -> dict[str, str]:
        published_time = cls._parse_youtube_page_time(text, timezone)
        if not published_time:
            return {}
        return {
            "published_time": published_time,
            "youtube_date_text": text,
        }
    @staticmethod
    def _max_live_age_hours() -> float:
        raw = (os.getenv("YOUTUBE_MAX_LIVE_AGE_HOURS") or "").strip()
        if not raw:
            return _DEFAULT_MAX_LIVE_AGE_HOURS
        try:
            return max(0.0, float(raw))
        except ValueError:
            return _DEFAULT_MAX_LIVE_AGE_HOURS

    @classmethod
    def parse_started_streaming_text(cls, text: str):
        """
        Parse YouTube live start copy into a timezone-aware UTC datetime.

        Handles:
          - "Started streaming on Jan 13, 2025"
          - "Started streaming on Jul 22, 2026"
          - "Started streaming 3 days ago"
        """
        cleaned = (text or "").replace("\u202f", " ").replace("\xa0", " ").strip()
        if not cleaned:
            return None

        ago = _STARTED_STREAMING_AGO_RE.search(cleaned)
        if ago:
            amount = int(ago.group(1))
            unit = ago.group(2).lower()
            now = datetime.now(pytz.UTC)
            if unit.startswith("minute"):
                return now - timedelta(minutes=amount)
            if unit.startswith("hour"):
                return now - timedelta(hours=amount)
            if unit.startswith("day"):
                return now - timedelta(days=amount)
            if unit.startswith("week"):
                return now - timedelta(weeks=amount)
            if unit.startswith("month"):
                return now - timedelta(days=30 * amount)
            if unit.startswith("year"):
                return now - timedelta(days=365 * amount)
            return None

        on = _STARTED_STREAMING_ON_RE.search(cleaned)
        if on:
            chunk = on.group(1).strip()
        elif "started streaming on" in cleaned.lower():
            idx = cleaned.lower().index("started streaming on")
            chunk = cleaned[idx + len("started streaming on") :].strip()
            chunk = re.split(r"[·|\n\r]", chunk, maxsplit=1)[0].strip()
        else:
            return None

        try:
            local_dt = date_parser.parse(chunk, fuzzy=True)
            if local_dt.tzinfo is None:
                local_dt = pytz.UTC.localize(local_dt)
            return local_dt.astimezone(pytz.UTC)
        except Exception:
            log.debug("Could not parse started-streaming text %r", text)
            return None

    @classmethod
    def _extract_started_streaming_from_yt_data(cls, yt_data) -> datetime | None:
        """Walk ytInitialData for 'Started streaming on …' strings."""
        if not yt_data:
            return None
        for node in cls.walk_nodes(yt_data):
            if isinstance(node, str) and "started streaming" in node.lower():
                started = cls.parse_started_streaming_text(node)
                if started:
                    return started
            if isinstance(node, dict):
                simple = node.get("simpleText")
                if isinstance(simple, str) and "started streaming" in simple.lower():
                    started = cls.parse_started_streaming_text(simple)
                    if started:
                        return started
        return None

    @classmethod
    def is_stale_live(cls, started_at: datetime, *, max_age_hours: float | None = None) -> bool:
        """True when a live stream has been running longer than max_age_hours."""
        if started_at is None:
            return False
        limit = cls._max_live_age_hours() if max_age_hours is None else max_age_hours
        if started_at.tzinfo is None:
            started_at = pytz.UTC.localize(started_at)
        age = datetime.now(pytz.UTC) - started_at.astimezone(pytz.UTC)
        return age.total_seconds() > limit * 3600

    @staticmethod
    def _legacy_is_live(video_data: dict) -> bool:
        """WallFly overlay-label check on videoRenderer."""
        try:
            overlays = video_data.get("thumbnailOverlays") or []
            if not overlays:
                return False
            overlay = overlays[0]
            status_text = (
                overlay.get("thumbnailOverlayTimeStatusRenderer") or {}
            ).get("text", {})
            label = (
                ((status_text.get("accessibility") or {}).get("accessibilityData") or {})
                .get("label", "")
                .lower()
            )
            return label == "live"
        except Exception:
            return False

    @classmethod
    def _classify_rich_item(cls, content: dict, timezone: str = "America/New_York") -> dict | None:
        """
        Classify one Live-tab card as live / upcoming / concluded.

        Returns dict with keys: status, video_id, video_title, meeting_link,
        plus scheduled_time (upcoming) or published_time (live/concluded) when
        those dates are exposed by ytInitialData.
        """
        if not isinstance(content, dict) or "continuationItemRenderer" in content:
            return None
        try:
            payload = content["richItemRenderer"]["content"]
        except (KeyError, TypeError):
            return None

        # Modern UI
        if "lockupViewModel" in payload:
            lockup = payload["lockupViewModel"]
            badge = (cls._lockup_badge_text(lockup) or "").lower()
            meta = lockup.get("metadata") or {}
            meta_vm = meta.get("lockupMetadataViewModel") or meta
            title = ((meta_vm.get("title") or {}).get("content") or "").strip()
            video_id = lockup.get("contentId")
            if not video_id or not title:
                return None
            meeting_link = f"https://www.youtube.com/watch?v={video_id}"
            schedule_text = cls._lockup_schedule_text(meta_vm)
            page_date_text = cls._lockup_page_date_text(meta_vm)
            if badge == "live":
                return {
                    "status": "live",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    **cls._published_page_fields(page_date_text, timezone),
                }
            if badge == "upcoming" or schedule_text:
                scheduled = (
                    cls._parse_scheduled_for(schedule_text, timezone)
                    if schedule_text
                    else None
                )
                return {
                    "status": "upcoming",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    "scheduled_time": scheduled,
                }
            # Past VODs / archives still listed on Live tab
            return {
                "status": "concluded",
                "video_id": video_id,
                "video_title": title,
                "meeting_link": meeting_link,
                **cls._published_page_fields(page_date_text, timezone),
            }

        # Legacy UI
        if "videoRenderer" in payload:
            video_data = payload["videoRenderer"]
            try:
                title = video_data["title"]["runs"][0]["text"]
                video_id = video_data["videoId"]
            except (KeyError, TypeError, IndexError):
                return None
            meeting_link = f"https://www.youtube.com/watch?v={video_id}"
            page_date_text = cls._legacy_page_date_text(video_data)
            if cls._legacy_is_live(video_data):
                return {
                    "status": "live",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    **cls._published_page_fields(page_date_text, timezone),
                }
            if "upcomingEventData" in video_data:
                try:
                    start = int(video_data["upcomingEventData"]["startTime"])
                    scheduled = datetime.fromtimestamp(start, tz=pytz.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except Exception:
                    scheduled = None
                return {
                    "status": "upcoming",
                    "video_id": video_id,
                    "video_title": title,
                    "meeting_link": meeting_link,
                    "scheduled_time": scheduled,
                }
            return {
                "status": "concluded",
                "video_id": video_id,
                "video_title": title,
                "meeting_link": meeting_link,
                **cls._published_page_fields(page_date_text, timezone),
            }
        return None

    @classmethod
    def classify_rich_item(cls, content: dict, timezone: str = "America/New_York") -> StreamCard | None:
        raw = cls._classify_rich_item(content, timezone)
        if not raw:
            return None
        return StreamCard.from_dict(raw)

    @classmethod
    def walk_nodes(cls, node):
        yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from cls.walk_nodes(value)
        elif isinstance(node, list):
            for item in node:
                yield from cls.walk_nodes(item)
