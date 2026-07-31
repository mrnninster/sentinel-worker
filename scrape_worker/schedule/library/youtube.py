"""Schedule-library adapter over youtube_core.YouTubeService.

Public method names stay stable for ScheduleScraper dispatch
(``youtube_table``, ``youtube_table_la``, ``youtube_table_md``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytz
from dateutil import parser

from youtube_core.service import YouTubeService, clear_youtube_page_cache

try:
    from utils.youtube import Youtube as YoutubeUtils
except ImportError:  # pragma: no cover
    YoutubeUtils = None

log = logging.getLogger(__name__)

__all__ = ["Youtube", "clear_youtube_page_cache"]


class Youtube(YouTubeService):
    """Dedicated YouTube schedule parser (thin adapter over youtube_core)."""

    def youtube_table(self, url, timezone="America/New_York", return_soup=False):
        """
        Upcoming meetings from the channel Live (/streams) tab.

        Fetches /streams once, classifies all cards, then filters to upcoming.
        ``return_soup`` is retained for WallFly call compatibility but always
        returns ``None`` for the soup half — callers should use
        ``get_live_videos(channel_url=...)`` instead of a second soup scrape.
        """
        classified = self.classify_channel_streams(url, timezone=timezone)
        meetings = [
            self.stream_item_to_meeting(item, timezone)
            for item in classified.get("upcoming") or []
        ]
        seen = set()
        unique = []
        for meeting in meetings:
            key = (
                meeting.get("Meeting link"),
                meeting.get("Meeting name"),
                meeting.get("Scheduled time"),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(meeting)
        if return_soup:
            return unique, None
        return unique

    def youtube_table_la(self, url, timezone="America/New_York"):
        """Filter out SAP meetings for Los Angeles YouTube pages."""
        try:
            meetings = self.youtube_table(url, timezone)
            return [m for m in meetings if "SAP" not in m["Meeting name"]]
        except Exception:
            log.exception("Error in youtube_table_la()")
            return []

    def youtube_table_md(self, url, timezone="America/New_York"):
        """
        Maryland-specific YouTube scraper: one next meeting per day.

        Requires ``ARG_CHANNEL_URL``. Uses modern channel classification for
        the live check (no broken soup handoff).
        """
        channel_url = os.getenv("ARG_CHANNEL_URL")
        if not channel_url:
            raise ValueError(
                "ARG_CHANNEL_URL environment variable is required for youtube_table_md"
            )

        if YoutubeUtils is not None:
            youtube_utils = YoutubeUtils(url=channel_url, meeting_title="")
            if not youtube_utils.is_valid_youtube_streams_url():
                raise ValueError(f"Invalid YouTube channel URL format: {channel_url}")

        all_meetings = self.youtube_table(channel_url, timezone)
        if not all_meetings:
            return []

        tz = pytz.timezone(timezone)
        current_time = datetime.now(pytz.utc)

        live_videos: list[str] = []
        try:
            live_videos_data = self.get_live_videos(channel_url=channel_url)
            if live_videos_data:
                live_videos = [v.get("video_id") for v in live_videos_data if v.get("video_id")]
        except Exception:
            log.exception("Error checking live videos in youtube_table_md")

        meetings_by_date: dict = {}
        for meeting in all_meetings:
            try:
                scheduled_time_str = meeting.get("Scheduled time", "")
                if not scheduled_time_str:
                    continue
                if scheduled_time_str.endswith("Z"):
                    scheduled_time = datetime.fromisoformat(
                        scheduled_time_str.replace("Z", "+00:00")
                    )
                else:
                    scheduled_time = parser.parse(scheduled_time_str)
                local_scheduled = scheduled_time.astimezone(tz)
                date_key = local_scheduled.date()
                meetings_by_date.setdefault(date_key, []).append(
                    {
                        "meeting": meeting,
                        "scheduled_time": scheduled_time,
                        "local_scheduled": local_scheduled,
                    }
                )
            except Exception as exc:
                log.warning("Error parsing meeting time in youtube_table_md: %s", exc)
                continue

        for date_key in meetings_by_date:
            meetings_by_date[date_key].sort(key=lambda x: x["scheduled_time"])

        filtered_meetings = []
        today_local = current_time.astimezone(tz).date()
        has_live_now = len(live_videos) > 0

        for date_key in sorted(meetings_by_date.keys()):
            date_meetings = meetings_by_date[date_key]
            if date_key > today_local:
                if date_meetings:
                    filtered_meetings.append(date_meetings[0]["meeting"])
                continue

            next_meeting = None
            if has_live_now:
                first_meeting_data = date_meetings[0]
                if first_meeting_data["scheduled_time"] > current_time:
                    next_meeting = first_meeting_data["meeting"]
            else:
                for idx, meeting_data in enumerate(date_meetings):
                    meeting = meeting_data["meeting"]
                    if idx == 0:
                        next_meeting = meeting
                        break
                    all_earlier_concluded = True
                    for earlier_idx in range(idx):
                        earlier_scheduled_time = date_meetings[earlier_idx]["scheduled_time"]
                        if earlier_scheduled_time > current_time:
                            all_earlier_concluded = False
                            break
                    if all_earlier_concluded:
                        next_meeting = meeting
                        break

            if next_meeting:
                filtered_meetings.append(next_meeting)

        for meeting in filtered_meetings:
            meeting["Stream type"] = "ts_youtube"
        return filtered_meetings
