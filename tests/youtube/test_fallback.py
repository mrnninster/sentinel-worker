"""Calendar overlay / fallback and facade regression tests."""

from __future__ import annotations

from youtube_core.models import StreamCard
from youtube_core.service import YouTubeService
from utils.youtube import Youtube as UtilsYoutube


def test_apply_schedule_fallback_require_title_match(monkeypatch):
    yt = YouTubeService()

    def fake_classify(channel_url, timezone="America/New_York"):
        return {
            "live": [],
            "upcoming": [],
            "concluded": [
                {
                    "status": "concluded",
                    "video_id": "goodVideo01",
                    "video_title": "Finance and Appropriations",
                    "meeting_link": "https://www.youtube.com/watch?v=goodVideo01",
                    "published_time": "2026-07-30T12:00:00Z",
                },
                {
                    "status": "concluded",
                    "video_id": "badVideo000",
                    "video_title": "Unrelated Zoning Hearing",
                    "meeting_link": "https://www.youtube.com/watch?v=badVideo000",
                    "published_time": "2026-07-30T12:00:00Z",
                },
            ],
            "skipped": [],
            "channel_url": channel_url,
            "streams_url": channel_url,
            "videos_url": channel_url.replace("/streams", "/videos"),
            "fetch_ok": True,
        }

    monkeypatch.setattr(yt, "classify_channel_for_fallback", fake_classify)

    meetings = [
        {
            "Meeting name": "Finance and Appropriations",
            "Scheduled time": "2026-07-30T18:00:00Z",
            "Status": "Upcoming",
        }
    ]
    out = yt.apply_schedule_fallback(
        meetings,
        channel_url="https://www.youtube.com/@x/streams",
        timezone="America/New_York",
        require_title_match=True,
        title_match_threshold=0.3,
        max_meeting_age_hours=720,  # allow fixture date in tests
    )
    result_meetings = out["meetings"] if isinstance(out, dict) else out
    assert result_meetings
    hit = result_meetings[0]
    assert hit.get("video_id") == "goodVideo01"
    assert "Meeting link" in hit


def test_utils_facade_get_live_videos_uses_core(monkeypatch):
    calls = {}

    def fake_get_live_videos(self, channel_url=None, soup=None):
        calls["channel_url"] = channel_url
        calls["soup"] = soup is not None
        return [{"video_id": "liveVideo001", "video_title": "Live"}]

    monkeypatch.setattr(YouTubeService, "get_live_videos", fake_get_live_videos)
    utils = UtilsYoutube(
        url="https://www.youtube.com/@Handle/streams", meeting_title="Council"
    )
    live = utils.get_live_videos(soup=object())
    assert live == [{"video_id": "liveVideo001", "video_title": "Live"}]
    assert calls["soup"] is True


def test_stream_card_roundtrip():
    card = StreamCard.from_dict(
        {
            "video_id": "abcdefghijk",
            "video_title": "Council",
            "status": "live",
            "meeting_link": "https://www.youtube.com/watch?v=abcdefghijk",
            "published_time": "2026-07-30T12:00:00Z",
        }
    )
    assert card.to_dict()["published_time"] == "2026-07-30T12:00:00Z"
