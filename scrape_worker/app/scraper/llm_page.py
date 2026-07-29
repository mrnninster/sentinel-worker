"""
LLM-friendly page prep — Playwright fetch + HtmlCleaner.

Produces cleaned Markdown (and HTML) suitable for LLM schedule extraction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.scraper.html_cleaner import HtmlCleaner
from app.scraper.page_fetcher import FetchResult, WaitUntil, fetch_page_html

log = logging.getLogger(__name__)


@dataclass
class CleanedPage:
    url: str
    final_url: str
    raw_html: str
    markdown: str
    cleaned_html: str
    raw_bytes: int
    cleaned_markdown_bytes: int
    token_reduction_pct: float


async def scrape_to_llm_markdown(
    url: str,
    *,
    wait: float = 2.0,
    wait_for_selector: Optional[str] = None,
    wait_until: WaitUntil = "domcontentloaded",
    navigation_timeout_ms: int = 30_000,
    keep_links: bool = True,
    keep_images: bool = False,
) -> CleanedPage:
    """
    Fetch a page and produce LLM-friendly Markdown (primary) plus cleaned HTML.

    HtmlCleaner typically cuts 70–90% of bytes while preserving headings, tables,
    lists, and links that encode meeting schedules.
    """
    fetched: FetchResult = await fetch_page_html(
        url,
        wait=wait,
        wait_for_selector=wait_for_selector,
        wait_until=wait_until,
        navigation_timeout_ms=navigation_timeout_ms,
    )

    cleaner = HtmlCleaner(keep_links=keep_links, keep_images=keep_images)
    markdown = cleaner.to_markdown(fetched.html)
    cleaned_html = cleaner.clean(fetched.html)

    md_bytes = len(markdown.encode("utf-8"))
    reduction = (1 - md_bytes / fetched.raw_bytes) * 100 if fetched.raw_bytes else 0.0

    log.info(
        "Cleaned page: raw=%s → markdown=%s (%.1f%% reduction)",
        f"{fetched.raw_bytes:,}",
        f"{md_bytes:,}",
        reduction,
    )

    return CleanedPage(
        url=url,
        final_url=fetched.final_url,
        raw_html=fetched.html,
        markdown=markdown,
        cleaned_html=cleaned_html,
        raw_bytes=fetched.raw_bytes,
        cleaned_markdown_bytes=md_bytes,
        token_reduction_pct=round(reduction, 1),
    )
