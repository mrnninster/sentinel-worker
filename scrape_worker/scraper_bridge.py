"""Bridge to embedded schedule scraper or remote HTTP scraper."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
_service = None
_mode: Optional[str] = None


def scraper_mode() -> str:
    """embedded | http"""
    explicit = (os.environ.get("SCRAPER_MODE") or "").strip().lower()
    if explicit in {"embedded", "http"}:
        return explicit
    if (os.environ.get("SCRAPER_URL") or "").strip():
        return "http"
    return "embedded"


def _ensure_embedded() -> Any:
    global _service
    if _service is not None:
        return _service
    scraper_root = Path(os.environ.get("SCRAPER_ROOT") or _ROOT).resolve()
    if not (scraper_root / "app").is_dir():
        raise RuntimeError(
            f"Scraper package not found under {scraper_root} (expected app/). "
            "Set SCRAPER_ROOT or SCRAPER_URL."
        )
    root_str = str(scraper_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from app.config import get_settings, reload_settings  # type: ignore
    from app.service import ScheduleScrapeService  # type: ignore

    settings = reload_settings() if hasattr(reload_settings, "__call__") else get_settings()
    _service = ScheduleScrapeService(settings)
    log.info("Embedded schedule scraper ready from %s", scraper_root)
    return _service


async def run_scrape(scrape_request: dict[str, Any]) -> dict[str, Any]:
    mode = scraper_mode()
    if mode == "http":
        base = (os.environ.get("SCRAPER_URL") or "").rstrip("/")
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(f"{base}/scrape/raw", json=scrape_request)
            resp.raise_for_status()
            return resp.json()

    service = _ensure_embedded()
    from app.models import ScrapeScheduleRequest  # type: ignore

    body = ScrapeScheduleRequest.model_validate(scrape_request)
    result = await service.scrape(body)
    return {
        "meetings": [m.to_display_dict() for m in result.meetings],
        "meta": result.meta.model_dump() if result.meta else {},
    }


async def run_stream_status(payload: dict[str, Any]) -> dict[str, Any]:
    mode = scraper_mode()
    if mode == "http":
        base = (os.environ.get("SCRAPER_URL") or "").rstrip("/")
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{base}/stream-status", json=payload)
            resp.raise_for_status()
            return resp.json()

    _ensure_embedded()
    import asyncio

    from app.youtube_status import check_youtube_stream_status  # type: ignore

    return await asyncio.to_thread(
        check_youtube_stream_status,
        channel_url=str(payload["channel_url"]),
        video_url=str(payload["video_url"]) if payload.get("video_url") else None,
        video_id=payload.get("video_id"),
        timezone=payload.get("timezone") or "America/New_York",
    )
