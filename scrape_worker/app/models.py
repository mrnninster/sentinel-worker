"""Request / response schemas for schedule extraction."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


class YoutubeFallbackConfig(BaseModel):
    """Optional YouTube overlay when primary calendar is thin or blocked."""

    channel_url: HttpUrl = Field(
        ...,
        description=(
            "YouTube channel URL (…/streams, …/videos, or channel root). "
            "Fallback scrapes both the Live and Videos tabs."
        ),
        examples=["https://www.youtube.com/@elpasocountycommissionerscourt/streams"],
    )
    on_primary_failure: Literal["status_only", "same_day_stub", "skip"] = Field(
        default="same_day_stub",
        description=(
            "skip = ignore YouTube. "
            "status_only = overlay Live/Concluded onto existing meetings only. "
            "same_day_stub = also create today's meeting(s) from Live/VOD cards "
            "when primary returned nothing for that day."
        ),
    )
    match: Literal["title_date", "video_id"] = Field(
        default="title_date",
        description=(
            "title_date = match by date parsed from VOD title (and scheduled time). "
            "video_id = match only via Meeting link / known video id."
        ),
    )


class ScrapeScheduleRequest(BaseModel):
    """POST body for schedule extraction."""

    url: HttpUrl = Field(..., description="Public calendar / meetings page URL")
    timezone: str = Field(
        default="America/New_York",
        description="IANA timezone for interpreting naive local times on the page",
        examples=["America/New_York", "America/Los_Angeles", "America/Chicago"],
    )
    mode: Literal["llm", "dedicated", "auto"] = Field(
        default="auto",
        description=(
            "llm = Playwright + HtmlCleaner + LLM. "
            "dedicated = platform parser (requires schedule_type, e.g. swagit_table). "
            "auto = dedicated when schedule_type is set, otherwise LLM."
        ),
    )
    schedule_type: Optional[str] = Field(
        default=None,
        description="Parser id, e.g. swagit_table, granicus_1_table, wordpress_table",
        examples=["swagit_table", "granicus_1_table", "wordpress_table"],
    )
    agenda_url: Optional[HttpUrl] = Field(
        default=None,
        description="Optional separate agenda URL for parsers that accept it",
    )
    youtube_fallback: Optional[YoutubeFallbackConfig] = Field(
        default=None,
        description=(
            "When set, scrape the YouTube Live (/streams) and Videos (/videos) tabs "
            "and merge status/links (and optional same-day stubs) into the primary "
            "scrape result."
        ),
    )
    wait: Optional[float] = Field(
        default=None,
        ge=0,
        description="Seconds to wait after load for JS (LLM mode; overrides env)",
    )
    wait_for_selector: Optional[str] = Field(
        default=None,
        description="CSS selector to wait for before capturing (LLM mode)",
    )
    wait_until: Optional[Literal["load", "domcontentloaded", "networkidle"]] = Field(
        default=None,
        description="Playwright navigation wait strategy (LLM mode)",
    )
    keep_links: bool = Field(
        default=True, description="Preserve hrefs in cleaned Markdown (LLM mode)"
    )
    include_page_markdown: bool = Field(
        default=False,
        description="Include cleaned Markdown in the response (LLM mode)",
    )
    include_past: bool = Field(
        default=False,
        description="If false, drop meetings before local midnight today",
    )
    filter_by_categories: bool = Field(
        default=True,
        description=(
            "If true, drop meetings whose titles are too distant from "
            "data/meeting_categories.json (celebration/music noise out; "
            "planning+commission style titles kept)."
        ),
    )
    category_match_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum soft-Jaccard / coverage score for category relevance",
    )


class Meeting(BaseModel):
    """Structured meeting extracted from a calendar page."""

    meeting_name: str = Field(..., alias="Meeting name")
    scheduled_time: str = Field(
        ...,
        alias="Scheduled time",
        description="ISO-8601 UTC ending in Z, e.g. 2026-07-26T17:00:00Z",
    )
    status: str = Field(default="Upcoming", alias="Status")
    agenda_link: Optional[str] = Field(default=None, alias="Agenda link")
    meeting_link: Optional[str] = Field(default=None, alias="Meeting link")
    stream_type: Optional[str] = Field(default=None, alias="Stream type")
    phone_number: Optional[str] = Field(default=None, alias="Phone number")
    passcode: Optional[str] = Field(default=None, alias="Passcode")
    access_id: Optional[str] = Field(default=None, alias="Access ID")
    user_live_link: Optional[str] = Field(default=None, alias="user_live_link")
    user_archive_link: Optional[str] = Field(default=None, alias="user_archive_link")

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }

    def to_display_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class ScrapeMeta(BaseModel):
    url: str
    timezone: str
    mode_used: str
    schedule_type: Optional[str] = None
    meeting_count: int
    raw_html_bytes: Optional[int] = None
    cleaned_markdown_bytes: Optional[int] = None
    token_reduction_pct: Optional[float] = None
    model_used: Optional[str] = None
    extraction_attempts: Optional[int] = None
    primary_ok: Optional[bool] = None
    primary_error: Optional[str] = None
    youtube_used: bool = False
    youtube_channel_url: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class ScrapeScheduleResponse(BaseModel):
    meetings: list[Meeting]
    meta: ScrapeMeta
    page_markdown: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    openai_configured: bool
    dedicated_parsers_available: bool


class StreamStatusRequest(BaseModel):
    """Check whether a YouTube stream is live, upcoming, or concluded."""

    channel_url: HttpUrl = Field(
        ...,
        description="YouTube channel streams URL, e.g. https://www.youtube.com/@Handle/streams",
        examples=["https://www.youtube.com/@WorcesterCountyPS/streams"],
    )
    video_url: Optional[HttpUrl] = Field(
        default=None,
        description="Optional watch URL (https://www.youtube.com/watch?v=…)",
    )
    video_id: Optional[str] = Field(
        default=None,
        description="Optional 11-char video id (overrides id parsed from video_url)",
        examples=["FRhtCgsIPRU"],
    )
    timezone: str = Field(
        default="America/New_York",
        description="IANA timezone for parsing upcoming 'Scheduled for …' text",
    )


class StreamVideoInfo(BaseModel):
    status: Optional[str] = None
    video_id: str
    video_title: str
    meeting_link: Optional[str] = None
    scheduled_time: Optional[str] = None


class StreamStatusResponse(BaseModel):
    status: Literal["live", "upcoming", "concluded", "channel_snapshot", "skipped"]
    video_id: Optional[str] = None
    video_title: Optional[str] = None
    meeting_link: Optional[str] = None
    scheduled_time: Optional[str] = None
    started_streaming_on: Optional[str] = None
    live_videos: list[StreamVideoInfo] = Field(default_factory=list)
    upcoming_videos: list[StreamVideoInfo] = Field(default_factory=list)
    concluded_on_page: list[StreamVideoInfo] = Field(default_factory=list)
    skipped_videos: list[StreamVideoInfo] = Field(default_factory=list)
    channel_url: str
    note: Optional[str] = None
