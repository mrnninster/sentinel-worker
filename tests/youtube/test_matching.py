"""Candidate ranking and title-match regression tests."""

from __future__ import annotations

from youtube_core.matching import (
    match_meeting_to_card,
    normalize_for_matching,
    rank_candidates,
    title_match_details,
)
from youtube_core.models import StreamCard
from utils.youtube import title_match_details as facade_title_match


def _card(**kwargs) -> StreamCard:
    defaults = {
        "video_id": "xxxxxxxxxxx",
        "video_title": "City Council Regular Meeting",
        "status": "concluded",
        "meeting_link": "https://www.youtube.com/watch?v=xxxxxxxxxxx",
    }
    defaults.update(kwargs)
    return StreamCard(**defaults)


def test_title_strategies_and_generic_word_rejection():
    assert title_match_details(
        "Finance and Appropriations",
        "Senate of Virginia: Finance & Appropriations on 2026-01-27",
    )[1] == "exact"
    # Single shared generic token must not match.
    conf, kind = title_match_details("City Council", "County Council")
    assert kind is None
    assert conf == 0.0
    # Facade re-exports the same implementation.
    assert facade_title_match("Finance and Appropriations", "Finance and Appropriations") == (
        1.0,
        "exact",
    )


def test_normalize_strips_noise():
    assert "finance" in normalize_for_matching(
        "Senate of Virginia: Finance & Appropriations on 2026-01-27 [Finished]"
    )


def test_explicit_video_id_wins_and_excludes_duplicates():
    cards = [
        _card(video_id="aaaaaaaaaaa", video_title="Wrong Title", status="live"),
        _card(
            video_id="bbbbbbbbbbb",
            video_title="City Council Regular Meeting",
            status="upcoming",
            scheduled_time="2026-07-30T18:00:00Z",
        ),
    ]
    meeting = {
        "Meeting name": "City Council Regular Meeting",
        "Scheduled time": "2026-07-30T18:00:00Z",
        "video_id": "aaaaaaaaaaa",
    }
    card, score, kind = match_meeting_to_card(
        meeting, cards, timezone="America/New_York"
    )
    assert kind == "video_id"
    assert card is not None and card.video_id == "aaaaaaaaaaa"

    card2, _, kind2 = match_meeting_to_card(
        meeting,
        cards,
        timezone="America/New_York",
        exclude_video_ids={"aaaaaaaaaaa"},
    )
    assert kind2 != "video_id" or card2 is None or card2.video_id != "aaaaaaaaaaa"


def test_ranking_prefers_title_then_date_and_live_status():
    cards = [
        _card(
            video_id="dateonly000",
            video_title="Unrelated Zoning",
            status="concluded",
            published_time="2026-07-30T12:00:00Z",
        ),
        _card(
            video_id="titledate00",
            video_title="City Council Regular Meeting",
            status="upcoming",
            scheduled_time="2026-07-30T18:00:00Z",
        ),
        _card(
            video_id="livetitle00",
            video_title="City Council Regular Meeting",
            status="live",
            published_time="2026-07-30T17:00:00Z",
        ),
    ]
    meeting = {
        "Meeting name": "City Council Regular Meeting",
        "Scheduled time": "2026-07-30T18:00:00Z",
    }
    ranked = rank_candidates(
        meeting, cards, timezone="America/New_York", require_title_match=True
    )
    assert ranked
    assert ranked[0][0].video_id == "livetitle00"


def test_require_title_match_blocks_date_only():
    cards = [
        _card(
            video_id="dateonly111",
            video_title="Completely Different Hearing",
            status="concluded",
            published_time="2026-07-30T12:00:00Z",
        )
    ]
    meeting = {
        "Meeting name": "City Council Regular Meeting",
        "Scheduled time": "2026-07-30T18:00:00Z",
    }
    card, _, kind = match_meeting_to_card(
        meeting,
        cards,
        timezone="America/New_York",
        require_title_match=True,
    )
    assert card is None
    assert kind is None


def test_max_meeting_age_hours_blocks_old_calendar_rows():
    cards = [
        _card(
            video_id="oldmeet0000",
            video_title="City Council Regular Meeting",
            status="concluded",
            published_time="2020-01-01T12:00:00Z",
        )
    ]
    meeting = {
        "Meeting name": "City Council Regular Meeting",
        "Scheduled time": "2020-01-01T18:00:00Z",
    }
    card, _, _ = match_meeting_to_card(
        meeting,
        cards,
        timezone="America/New_York",
        require_title_match=True,
        max_meeting_age_hours=24,
    )
    assert card is None
