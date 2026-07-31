"""Title and candidate ranking for calendar ↔ YouTube matching."""

from __future__ import annotations

from datetime import date, datetime
from typing import AbstractSet, Any, Literal, Mapping, Optional, Sequence

import pytz
from dateutil import parser as date_parser
from fuzzywuzzy import fuzz

from youtube_core.models import StreamCard

MatchMode = Literal["title_date", "video_id"]


def normalize_for_matching(text: str) -> str:
    """Normalize text for fuzzy matching by removing common noise."""
    import re

    if not text:
        return ""

    text = text.lower()
    prefixes_to_remove = [
        r"^senate of virginia:\s*",
        r"^virginia senate:\s*",
        r"^house of delegates:\s*",
        r"^virginia house:\s*",
        r"^commonwealth of virginia:\s*",
    ]
    for prefix in prefixes_to_remove:
        text = re.sub(prefix, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+on\s+\d{4}-\d{2}-\d{2}.*$", "", text)
    text = re.sub(
        r"\s+on\s+[a-z]+\s+\d{1,2},?\s+\d{4}.*$", "", text, flags=re.IGNORECASE
    )
    text = re.sub(r"\s*\[.*?\]\s*", " ", text)
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    noise_words = {
        "committee",
        "subcommittee",
        "senate",
        "house",
        "of",
        "the",
        "virginia",
        "commonwealth",
        "joint",
        "special",
        "select",
        "standing",
    }
    tokens = [t for t in text.split() if t not in noise_words]
    return " ".join(tokens)


def fuzzy_match_confidence(calendar_name: str, youtube_title: str) -> float:
    """Jaccard similarity on normalized tokens (0.0–1.0)."""
    cal_normalized = normalize_for_matching(calendar_name)
    yt_normalized = normalize_for_matching(youtube_title)
    if not cal_normalized or not yt_normalized:
        return 0.0
    cal_tokens = set(cal_normalized.split())
    yt_tokens = set(yt_normalized.split())
    if not cal_tokens or not yt_tokens:
        return 0.0
    overlap = len(cal_tokens & yt_tokens)
    union = len(cal_tokens | yt_tokens)
    return overlap / union if union else 0.0


def title_match_details(
    calendar_name: str,
    youtube_title: str,
    *,
    jaccard_threshold: float = 0.3,
    keyword_threshold: float = 0.6,
    fuzzy_threshold: float = 0.7,
) -> tuple[float, str | None]:
    """Match titles using exact, containment, keyword, Jaccard, and fuzzy signals."""
    calendar = normalize_for_matching(calendar_name)
    youtube = normalize_for_matching(youtube_title)
    if not calendar or not youtube:
        return 0.0, None

    calendar_tokens = set(calendar.split())
    youtube_tokens = set(youtube.split())
    common = calendar_tokens & youtube_tokens

    if calendar == youtube:
        return 1.0, "exact"

    shorter, longer = sorted((calendar, youtube), key=len)
    if shorter in longer and (len(shorter.split()) >= 2 or calendar_tokens == youtube_tokens):
        return 0.98, "containment"

    fuzzy = fuzz.token_set_ratio(calendar, youtube) / 100.0
    typo_threshold = 0.82 if not common else 0.92
    if (
        len(common) < 2
        and len(calendar_tokens) >= 2
        and len(youtube_tokens) >= 2
        and fuzzy >= max(fuzzy_threshold, typo_threshold)
    ):
        return fuzzy, "fuzzy"

    if len(common) < 2:
        return 0.0, None

    candidates: list[tuple[float, str]] = []
    union = calendar_tokens | youtube_tokens
    jaccard = len(common) / len(union) if union else 0.0
    if jaccard >= jaccard_threshold:
        candidates.append((jaccard, "token_jaccard"))

    keyword_coverage = len(common) / min(len(calendar_tokens), len(youtube_tokens))
    if keyword_coverage >= keyword_threshold:
        candidates.append((keyword_coverage, "keyword_intersection"))

    if fuzzy >= fuzzy_threshold:
        candidates.append((fuzzy, "fuzzy"))

    return max(candidates, default=(0.0, None), key=lambda candidate: candidate[0])


def find_best_match(
    meeting_title: str, live_videos: list, threshold: float = 0.3
) -> tuple:
    """Find the best matching live video for a meeting title."""
    if not live_videos or not meeting_title:
        return None, 0.0, None

    best_match = None
    best_confidence = 0.0
    match_type = None
    for video_data in live_videos:
        video_title = video_data.get("video_title", "")
        confidence, candidate_type = title_match_details(
            meeting_title,
            video_title,
            jaccard_threshold=threshold,
        )
        if confidence > best_confidence:
            best_confidence = confidence
            best_match = video_data
            match_type = candidate_type

    if best_confidence >= threshold:
        return best_match, best_confidence, match_type
    return None, best_confidence, None


def _meeting_title(meeting: Mapping[str, Any]) -> str:
    return str(
        meeting.get("Meeting name")
        or meeting.get("meeting_name")
        or meeting.get("title")
        or ""
    ).strip()


def _meeting_video_id(meeting: Mapping[str, Any]) -> str | None:
    import re

    for key in ("video_id", "Meeting link", "meeting_link", "user_live_link", "user_archive_link"):
        raw = meeting.get(key)
        if not raw:
            continue
        text = str(raw).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
            return text
        m = re.search(r"(?:v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})", text)
        if m:
            return m.group(1)
    return None


def _meeting_local_date(meeting: Mapping[str, Any], timezone: str) -> date | None:
    raw = meeting.get("Scheduled time") or meeting.get("scheduled_time")
    if not raw:
        return None
    try:
        tz = pytz.timezone(timezone)
    except Exception:
        tz = pytz.UTC
    try:
        when = date_parser.parse(str(raw))
        if when.tzinfo is None:
            when = tz.localize(when)
        return when.astimezone(tz).date()
    except Exception:
        return None


def card_local_date(card: StreamCard, timezone: str) -> date | None:
    """Local calendar date from structured YouTube fields only (never the title)."""
    try:
        tz = pytz.timezone(timezone)
    except Exception:
        tz = pytz.UTC
    for raw in (card.scheduled_time, card.started_streaming_on, card.published_time):
        if not raw:
            continue
        try:
            when = date_parser.parse(str(raw))
            if when.tzinfo is None:
                when = tz.localize(when)
            return when.astimezone(tz).date()
        except Exception:
            continue
    return None


def rank_candidates(
    meeting: Mapping[str, Any],
    cards: Sequence[StreamCard],
    *,
    timezone: str,
    require_title_match: bool = False,
    title_match_threshold: float = 0.3,
    prefer_statuses: Sequence[str] = ("live", "upcoming", "concluded"),
    exclude_video_ids: AbstractSet[str] | None = None,
) -> list[tuple[StreamCard, float, str | None]]:
    """
    Rank cards for a calendar meeting.

    Order: explicit video_id > title strategies > structured-date proximity.
    Within equal title/date quality, prefer live > upcoming > concluded.
    """
    excluded = exclude_video_ids or set()
    meeting_vid = _meeting_video_id(meeting)
    meeting_date = _meeting_local_date(meeting, timezone)
    title = _meeting_title(meeting)
    status_bonus = {status: 3.0 - idx for idx, status in enumerate(prefer_statuses)}

    ranked: list[tuple[StreamCard, float, str | None]] = []
    for card in cards:
        if card.video_id in excluded or card.status == "skipped":
            continue
        if meeting_vid and card.video_id == meeting_vid:
            ranked.append((card, 100.0 + status_bonus.get(card.status, 0.0), "video_id"))
            continue

        title_score, match_type = title_match_details(
            title,
            card.video_title,
            jaccard_threshold=title_match_threshold,
        )
        if require_title_match and match_type is None:
            continue

        card_date = card_local_date(card, timezone)
        date_ok = meeting_date is not None and card_date == meeting_date
        if meeting_date is not None and card_date is not None and not date_ok:
            # Date mismatch: only keep if we are not requiring date equality later.
            continue

        score = status_bonus.get(card.status, 0.0)
        if match_type:
            score += 10.0 + title_score
        if date_ok:
            score += 5.0
        if match_type or date_ok:
            ranked.append((card, score, match_type or ("date" if date_ok else None)))

    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked


def match_meeting_to_card(
    meeting: Mapping[str, Any],
    cards: Sequence[StreamCard],
    *,
    timezone: str,
    match: MatchMode = "title_date",
    require_title_match: bool = False,
    title_match_threshold: float = 0.3,
    max_meeting_age_hours: float | None = 24.0,
    exclude_video_ids: AbstractSet[str] | None = None,
) -> tuple[Optional[StreamCard], float, str | None]:
    """Pick the best card for a meeting under the given policy."""
    meeting_vid = _meeting_video_id(meeting)
    if match == "video_id" and not meeting_vid:
        return None, 0.0, None

    # Exact video_id always wins and bypasses age bound.
    if meeting_vid:
        for card in cards:
            if card.video_id == meeting_vid and card.video_id not in (exclude_video_ids or set()):
                return card, 100.0, "video_id"

    if match == "video_id":
        return None, 0.0, None

    if max_meeting_age_hours is not None and max_meeting_age_hours > 0:
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC
        when = None
        raw = meeting.get("Scheduled time") or meeting.get("scheduled_time")
        if raw:
            try:
                when = date_parser.parse(str(raw))
                if when.tzinfo is None:
                    when = tz.localize(when)
                when = when.astimezone(tz)
            except Exception:
                when = None
        if when is not None:
            age_limit = datetime.now(tz) - __import__("datetime").timedelta(
                hours=float(max_meeting_age_hours)
            )
            if when < age_limit:
                return None, 0.0, None

    ranked = rank_candidates(
        meeting,
        cards,
        timezone=timezone,
        require_title_match=require_title_match,
        title_match_threshold=title_match_threshold,
        exclude_video_ids=exclude_video_ids,
    )
    if not ranked:
        return None, 0.0, None
    card, score, kind = ranked[0]
    if require_title_match and kind in {None, "date"}:
        return None, score, None
    # title_date without require_title_match: date equality is enough.
    if not require_title_match:
        meeting_date = _meeting_local_date(meeting, timezone)
        card_date = card_local_date(card, timezone)
        if meeting_date and card_date and meeting_date == card_date:
            return card, score, kind or "date"
        if kind and kind != "date":
            return card, score, kind
        return None, score, None
    return card, score, kind
