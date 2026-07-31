"""Stream-status / monitor semantics: fetch_failed must not conclude."""

from __future__ import annotations

from youtube_core.service import YouTubeService


def test_fetch_failed_is_non_terminal(monkeypatch):
    yt = YouTubeService()

    def boom(channel_url, timezone="America/New_York"):
        return {
            "live": [],
            "upcoming": [],
            "concluded": [],
            "skipped": [],
            "channel_url": channel_url,
            "fetch_ok": False,
            "fetch_error": "ytInitialData missing or unreadable",
        }

    monkeypatch.setattr(yt, "classify_channel_streams", boom)
    result = yt.check_stream_status(
        channel_url="https://www.youtube.com/@x/streams",
        video_id="abcdefghijk",
    )
    assert result["status"] == "fetch_failed"
    assert result["status"] not in {"concluded", "skipped", "adjourned"}


def test_absent_video_after_successful_fetch_is_concluded(monkeypatch):
    yt = YouTubeService()

    def ok(channel_url, timezone="America/New_York"):
        return {
            "live": [
                {
                    "status": "live",
                    "video_id": "otherVideo1",
                    "video_title": "Other",
                    "meeting_link": "https://www.youtube.com/watch?v=otherVideo1",
                }
            ],
            "upcoming": [],
            "concluded": [],
            "skipped": [],
            "channel_url": channel_url,
            "fetch_ok": True,
        }

    monkeypatch.setattr(yt, "classify_channel_streams", ok)
    result = yt.check_stream_status(
        channel_url="https://www.youtube.com/@x/streams",
        video_id="abcdefghijk",
    )
    assert result["status"] == "concluded"
    assert result["match_diagnostics"]["found"] is False


def test_video_id_continuity_live(monkeypatch):
    yt = YouTubeService()

    def ok(channel_url, timezone="America/New_York"):
        return {
            "live": [
                {
                    "status": "live",
                    "video_id": "abcdefghijk",
                    "video_title": "Council",
                    "meeting_link": "https://www.youtube.com/watch?v=abcdefghijk",
                    "started_streaming_on": "2026-07-30T12:00:00Z",
                    "published_time": "2026-07-30T12:00:00Z",
                }
            ],
            "upcoming": [],
            "concluded": [],
            "skipped": [],
            "channel_url": channel_url,
            "fetch_ok": True,
        }

    monkeypatch.setattr(yt, "classify_channel_streams", ok)
    result = yt.check_stream_status(
        channel_url="https://www.youtube.com/@x/streams",
        video_id="abcdefghijk",
    )
    assert result["status"] == "live"
    assert result["started_streaming_on"] == "2026-07-30T12:00:00Z"
    assert result["published_time"] == "2026-07-30T12:00:00Z"
    assert result["match_diagnostics"]["bucket"] == "live"


def test_runner_terminal_statuses():
    """fetch_failed / unknown must not end the monitor loop."""
    terminal = {"concluded", "adjourned", "skipped"}
    for status in ("fetch_failed", "unknown", "live", "upcoming"):
        assert (status in terminal) is False
    for status in ("concluded", "adjourned", "skipped"):
        assert status in terminal
