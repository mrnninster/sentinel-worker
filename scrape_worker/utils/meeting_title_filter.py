"""
Filter scraped meetings against the known meeting-category vocabulary.

Meetings whose titles are too distant from ``data/meeting_categories.json``
are dropped. Close-but-new titles (e.g. planning + commission variants) are
kept so they can become new categories downstream.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_CATEGORIES_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "meeting_categories.json"
)

# Align with WallFly utils.youtube.normalize_for_matching noise words, plus
# schedule-title filler that should not drive category membership.
_NOISE_WORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
    "meeting",
    "meetings",
    "committee",
    "subcommittee",
    "senate",
    "house",
    "special",
    "regular",
    "emergency",
    "virtual",
    "hybrid",
    "zoom",
    "webcast",
    "live",
    "stream",
    "streaming",
}

_DATE_NOISE_RE = re.compile(
    r"\b(?:"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
    r",?\s+\d{4}"
    r"|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    r"\d{4}-\d{2}-\d{2}"
    r"|"
    r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?"
    r")\b",
    re.IGNORECASE,
)

_ORDINAL_DAY_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)

DEFAULT_THRESHOLD = 0.3


@lru_cache(maxsize=1)
def load_meeting_categories() -> tuple[str, ...]:
    raw = json.loads(_CATEGORIES_PATH.read_text(encoding="utf-8"))
    cats = raw.get("categories") if isinstance(raw, dict) else raw
    if not isinstance(cats, list) or not cats:
        raise ValueError(f"No categories found in {_CATEGORIES_PATH}")
    return tuple(str(c).strip() for c in cats if str(c).strip())


def normalize_title(text: str) -> str:
    """Normalize a meeting/category title for token comparison."""
    if not text:
        return ""
    text = _DATE_NOISE_RE.sub(" ", text)
    text = _ORDINAL_DAY_RE.sub(" ", text)
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens = [
        t for t in text.split() if t and t not in _NOISE_WORDS and not t.isdigit()
    ]
    return " ".join(tokens)


_EXCLUDE_CATEGORIES = {normalize_title(c) for c in ("Not a Meeting",)}


def _tokens_related(a: str, b: str) -> bool:
    if a == b:
        return True
    # commission ↔ commissioners, zoning ↔ rezoning, etc.
    if len(a) >= 5 and len(b) >= 5:
        return a.startswith(b) or b.startswith(a) or a in b or b in a
    return False


def soft_jaccard(title_tokens: set[str], category_tokens: set[str]) -> float:
    if not title_tokens or not category_tokens:
        return 0.0
    matched = 0
    for ct in category_tokens:
        if any(_tokens_related(ct, tt) for tt in title_tokens):
            matched += 1
    union = len(category_tokens) + len(title_tokens) - matched
    return matched / union if union else 0.0


def category_coverage(title_tokens: set[str], category_tokens: set[str]) -> float:
    """Fraction of category tokens found (soft) in the title."""
    if not category_tokens:
        return 0.0
    matched = sum(
        1
        for ct in category_tokens
        if any(_tokens_related(ct, tt) for tt in title_tokens)
    )
    return matched / len(category_tokens)


def best_category_match(
    title: str,
    categories: Optional[tuple[str, ...]] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[Optional[str], float, str]:
    """
    Find the best matching category for a title.

    Returns ``(category, score, match_type)`` where match_type is
    ``phrase``, ``coverage``, ``fuzzy``, or ``none``.
    """
    categories = categories or load_meeting_categories()
    title_norm = normalize_title(title)
    title_tokens = set(title_norm.split()) if title_norm else set()
    if not title_tokens:
        return None, 0.0, "none"

    best_name: Optional[str] = None
    best_score = 0.0
    best_type = "none"
    near_name: Optional[str] = None
    near_score = 0.0

    for category in categories:
        cat_norm = normalize_title(category)
        if not cat_norm:
            continue
        cat_tokens = set(cat_norm.split())
        if not cat_tokens:
            continue

        # Contiguous category phrase inside the title (not the reverse —
        # short titles like "board" must not match "Alcohol Control Board").
        if len(cat_norm) >= 5 and cat_norm in title_norm:
            return category, 1.0, "phrase"
        if title_norm == cat_norm:
            return category, 1.0, "phrase"

        coverage = category_coverage(title_tokens, cat_tokens)
        jaccard = soft_jaccard(title_tokens, cat_tokens)
        matched = sum(
            1
            for ct in cat_tokens
            if any(_tokens_related(ct, tt) for tt in title_tokens)
        )

        score = 0.0
        match_type = "none"
        long_singleton = len(cat_tokens) == 1 and len(next(iter(cat_tokens))) >= 5

        # Strong coverage of a multi-word (or long single-word) category
        if coverage >= 0.75 and (len(cat_tokens) >= 2 or long_singleton):
            score = max(coverage, jaccard)
            match_type = "coverage"
        # Fuzzy: need enough shared signal (avoid one weak shared word)
        elif jaccard >= threshold and (matched >= 2 or long_singleton):
            score = jaccard
            match_type = "fuzzy"

        if match_type != "none" and score > best_score:
            best_score = score
            best_name = category
            best_type = match_type
        elif max(jaccard, coverage) > near_score:
            near_score = max(jaccard, coverage)
            near_name = category

    if best_type != "none" and best_score >= threshold and best_name:
        return best_name, best_score, best_type
    return near_name, near_score, "none"


def is_relevant_meeting_title(
    title: str,
    *,
    categories: Optional[tuple[str, ...]] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> bool:
    category, score, match_type = best_category_match(
        title, categories=categories, threshold=threshold
    )
    if match_type == "none" or not category:
        return False
    if normalize_title(category) in _EXCLUDE_CATEGORIES:
        log.debug(
            "Rejecting title %r — matched exclude category %r (%.2f %s)",
            title,
            category,
            score,
            match_type,
        )
        return False
    return True


def filter_meetings_by_category(
    meetings: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    enabled: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Drop meetings whose titles are too distant from known categories.

    Returns ``(kept, notes)``.
    """
    if not enabled:
        return meetings, []

    categories = load_meeting_categories()
    kept: list[dict[str, Any]] = []
    notes: list[str] = []
    dropped = 0

    for meeting in meetings:
        title = meeting.get("Meeting name") or meeting.get("meeting_name") or ""
        category, score, match_type = best_category_match(
            str(title), categories=categories, threshold=threshold
        )
        relevant = (
            match_type != "none"
            and category is not None
            and normalize_title(category) not in _EXCLUDE_CATEGORIES
        )
        if relevant:
            kept.append(meeting)
            continue
        dropped += 1
        notes.append(
            f"filtered title={title!r} best={category!r} "
            f"score={score:.2f} type={match_type}"
        )

    if dropped:
        log.info(
            "Category filter kept %d / dropped %d (threshold=%.2f)",
            len(kept),
            dropped,
            threshold,
        )
        notes.insert(
            0,
            f"category_filter kept={len(kept)} dropped={dropped} "
            f"threshold={threshold}",
        )
    return kept, notes
