"""Typed YouTube card and channel snapshot models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

CardStatus = Literal["live", "upcoming", "concluded", "skipped"]
SourceTab = Literal["streams", "videos"]
MonitorStatus = Literal[
    "live",
    "upcoming",
    "concluded",
    "skipped",
    "channel_snapshot",
    "unknown",
    "fetch_failed",
]


@dataclass(frozen=True)
class StreamCard:
    """One YouTube channel card with structured title and date fields."""

    video_id: str
    video_title: str
    status: CardStatus
    meeting_link: str
    scheduled_time: Optional[str] = None
    published_time: Optional[str] = None
    youtube_date_text: Optional[str] = None
    started_streaming_on: Optional[str] = None
    source_tab: Optional[SourceTab] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StreamCard:
        return cls(
            video_id=str(raw["video_id"]),
            video_title=str(raw.get("video_title") or raw.get("Meeting name") or ""),
            status=raw.get("status") or "concluded",  # type: ignore[arg-type]
            meeting_link=str(
                raw.get("meeting_link")
                or raw.get("Meeting link")
                or f"https://www.youtube.com/watch?v={raw['video_id']}"
            ),
            scheduled_time=raw.get("scheduled_time") or raw.get("Scheduled time"),
            published_time=raw.get("published_time"),
            youtube_date_text=raw.get("youtube_date_text"),
            started_streaming_on=raw.get("started_streaming_on"),
            source_tab=raw.get("source_tab"),
            note=raw.get("note"),
        )


@dataclass
class ChannelSnapshot:
    """Classified channel cards from /streams and/or /videos."""

    live: list[StreamCard] = field(default_factory=list)
    upcoming: list[StreamCard] = field(default_factory=list)
    concluded: list[StreamCard] = field(default_factory=list)
    skipped: list[StreamCard] = field(default_factory=list)
    channel_url: str = ""
    streams_url: Optional[str] = None
    videos_url: Optional[str] = None
    fetch_ok: bool = True
    fetch_error: Optional[str] = None

    def all_cards(self) -> list[StreamCard]:
        return [*self.live, *self.upcoming, *self.concluded, *self.skipped]

    def to_legacy_dict(self) -> dict[str, Any]:
        """Shape expected by older schedule/library callers."""
        return {
            "live": [c.to_dict() for c in self.live],
            "upcoming": [c.to_dict() for c in self.upcoming],
            "concluded": [c.to_dict() for c in self.concluded],
            "skipped": [c.to_dict() for c in self.skipped],
            "channel_url": self.channel_url,
            "streams_url": self.streams_url,
            "videos_url": self.videos_url,
            "fetch_ok": self.fetch_ok,
            "fetch_error": self.fetch_error,
        }

    @classmethod
    def from_legacy_dict(cls, raw: dict[str, Any]) -> ChannelSnapshot:
        def _cards(key: str) -> list[StreamCard]:
            return [StreamCard.from_dict(item) for item in raw.get(key) or [] if item.get("video_id")]

        return cls(
            live=_cards("live"),
            upcoming=_cards("upcoming"),
            concluded=_cards("concluded"),
            skipped=_cards("skipped"),
            channel_url=str(raw.get("channel_url") or ""),
            streams_url=raw.get("streams_url"),
            videos_url=raw.get("videos_url"),
            fetch_ok=bool(raw.get("fetch_ok", True)),
            fetch_error=raw.get("fetch_error"),
        )
