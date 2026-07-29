"""YouTube stream status check — live / upcoming / concluded (metadata only)."""

from __future__ import annotations

import logging
from typing import Optional

from schedule.library.youtube import Youtube

log = logging.getLogger(__name__)


def check_youtube_stream_status(
    *,
    channel_url: str,
    video_url: Optional[str] = None,
    video_id: Optional[str] = None,
    timezone: str = "America/New_York",
) -> dict:
    """
    Metadata-only status check (no HLS / stream download).

    WallFly equivalent: utils.youtube.Youtube.get_live_videos +
    DetectEnd.ts_youtube (video still in LIVE overlay list ⇒ live;
    absent ⇒ concluded).
    """
    yt = Youtube()
    return yt.check_stream_status(
        channel_url=channel_url,
        video_id=video_id,
        video_url=video_url,
        timezone=timezone,
    )
