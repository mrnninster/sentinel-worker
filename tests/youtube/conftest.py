"""Shared fixtures for YouTube core regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRAPE_WORKER = ROOT / "scrape_worker"
if str(SCRAPE_WORKER) not in sys.path:
    sys.path.insert(0, str(SCRAPE_WORKER))


def lockup_card(
    *,
    video_id: str,
    title: str,
    badge: str | None = None,
    metadata_parts: list[str] | None = None,
) -> dict:
    """Minimal modern lockupViewModel rich-item fixture."""
    rows = []
    if metadata_parts:
        rows.append(
            {
                "metadataParts": [
                    {"text": {"content": part}} for part in metadata_parts
                ]
            }
        )
    lockup: dict = {
        "contentId": video_id,
        "metadata": {
            "lockupMetadataViewModel": {
                "title": {"content": title},
                "metadata": {
                    "contentMetadataViewModel": {
                        "metadataRows": rows,
                    }
                },
            }
        },
    }
    if badge:
        # Structure expected by StreamCardParser._lockup_badge_text
        lockup["contentImage"] = {
            "thumbnailViewModel": {
                "overlays": [
                    {
                        "thumbnailBottomOverlayViewModel": {
                            "badges": [
                                {"thumbnailBadgeViewModel": {"text": badge}}
                            ]
                        }
                    }
                ]
            }
        }
    return {"richItemRenderer": {"content": {"lockupViewModel": lockup}}}


def legacy_card(
    *,
    video_id: str,
    title: str,
    live: bool = False,
    upcoming_start: int | None = None,
    published_time_text: str | None = None,
) -> dict:
    """Minimal legacy videoRenderer rich-item fixture."""
    video: dict = {
        "videoId": video_id,
        "title": {"runs": [{"text": title}]},
    }
    if live:
        video["thumbnailOverlays"] = [
            {
                "thumbnailOverlayTimeStatusRenderer": {
                    "text": {
                        "accessibility": {
                            "accessibilityData": {"label": "LIVE"}
                        }
                    }
                }
            }
        ]
    if upcoming_start is not None:
        video["upcomingEventData"] = {"startTime": str(upcoming_start)}
    if published_time_text:
        video["publishedTimeText"] = {"simpleText": published_time_text}
    return {"richItemRenderer": {"content": {"videoRenderer": video}}}


@pytest.fixture
def tz() -> str:
    return "America/New_York"
