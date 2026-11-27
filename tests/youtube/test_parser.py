"""Parser regression: lockup + legacy cards, structured dates vs titles."""

from __future__ import annotations

from datetime import datetime

import pytz

from tests.youtube.conftest import legacy_card, lockup_card
from youtube_core.parser import StreamCardParser


def test_lockup_live_and_relative_published_time(tz):
    item = lockup_card(
        video_id="abcdefghijk",
        title="City Council Meeting Part 2 — July fantasy",
        badge="LIVE",
        metadata_parts=["2 days ago"],
    )
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "live"
    assert card.video_id == "abcdefghijk"
    assert card.published_time is not None
    assert card.youtube_date_text == "2 days ago"
    # Title must not supply the date — published_time comes from metadata.
    published = datetime.fromisoformat(card.published_time.replace("Z", "+00:00"))
    age_days = (datetime.now(pytz.UTC) - published).total_seconds() / 86400
    assert 1.0 < age_days < 3.5


def test_lockup_upcoming_premieres_is_case_insensitive(tz):
    item = lockup_card(
        video_id="PREMIERE001",
        title="2026 Varsity Football: District Semifinals",
        badge="Upcoming",
        metadata_parts=["pReMiErEs 11/27/26, 4:00\u202fPM"],
    )
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "upcoming"
    assert card.scheduled_time == "2026-11-27T21:00:00Z"


def test_lockup_upcoming_scheduled_for(tz):
    item = lockup_card(
        video_id="ABCDEFGHIJK",
        title="Zoning Board",
        badge="UPCOMING",
        metadata_parts=["Scheduled for July 30, 2026 6:00 PM"],
    )
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "upcoming"
    assert card.scheduled_time is not None
    assert card.scheduled_time.startswith("2026-07-30")


def test_lockup_concluded_streamed_date_not_from_title(tz):
    item = lockup_card(
        video_id="12345678901",
        title="Budget Hearing on 2020-01-01 Part 15",
        badge=None,
        metadata_parts=["Streamed Jul 28, 2026"],
    )
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "concluded"
    assert card.published_time is not None
    assert "2026-07-28" in card.published_time
    assert "2020" not in card.published_time


def test_legacy_video_renderer_live_and_upcoming(tz):
    live = StreamCardParser.classify_rich_item(
        legacy_card(video_id="liveVideo01", title="Live Now", live=True),
        timezone=tz,
    )
    assert live is not None and live.status == "live"

    upcoming = StreamCardParser.classify_rich_item(
        legacy_card(
            video_id="upcomVideo1",
            title="Tomorrow",
            upcoming_start=1785405600,  # ~2026-07-30
        ),
        timezone=tz,
    )
    assert upcoming is not None and upcoming.status == "upcoming"
    assert upcoming.scheduled_time is not None

    concluded = StreamCardParser.classify_rich_item(
        legacy_card(
            video_id="conclVideo1",
            title="Old VOD Part 2",
            published_time_text="Streamed 3 days ago",
        ),
        timezone=tz,
    )
    assert concluded is not None and concluded.status == "concluded"
    assert concluded.published_time is not None


def test_stale_live_detection():
    started = datetime.now(pytz.UTC).replace(microsecond=0)
    assert not StreamCardParser.is_stale_live(started, max_age_hours=24)
    old = datetime(2020, 1, 1, tzinfo=pytz.UTC)
    assert StreamCardParser.is_stale_live(old, max_age_hours=24)


def test_lockup_live_badge_style_without_text(tz):
    item = lockup_card(
        video_id="styleLive001",
        title="City Council",
        badge=None,
        metadata_parts=["1.2K watching"],
    )
    lockup = item["richItemRenderer"]["content"]["lockupViewModel"]
    lockup["contentImage"] = {
        "thumbnailViewModel": {
            "overlays": [
                {
                    "thumbnailBottomOverlayViewModel": {
                        "badges": [
                            {
                                "thumbnailBadgeViewModel": {
                                    "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "live"


def test_lockup_started_streaming_metadata_is_live(tz):
    item = lockup_card(
        video_id="startedLive1",
        title="County Commission",
        badge=None,
        metadata_parts=["Started streaming 2 hours ago"],
    )
    card = StreamCardParser.classify_rich_item(item, timezone=tz)
    assert card is not None
    assert card.status == "live"


def test_live_broadcast_start_ignores_publish_date():
    started = StreamCardParser.live_broadcast_start(
        {
            "microformat": {
                "playerMicroformatRenderer": {
                    "publishDate": "2023-12-01",
                    "liveBroadcastDetails": {
                        "startTimestamp": "2026-09-02T18:00:00+00:00",
                        "isLiveNow": True,
                    },
                }
            }
        }
    )
    assert started is not None
    assert started.year == 2026
    assert started.month == 9
