#!/usr/bin/env python
"""
CLI: scrape a URL to LLM-friendly Markdown/HTML (no LLM call).

Usage::

    python -m app.cli_scrape --url "https://example.gov/meetings/" --markdown
    python -m app.cli_scrape --url "https://example.gov/meetings/" --output /tmp/page.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.scraper.html_cleaner import HtmlCleaner
from app.scraper.page_fetcher import fetch_page_html


async def scrape(args: argparse.Namespace) -> str:
    fetched = await fetch_page_html(
        args.url,
        wait=args.wait,
        wait_for_selector=args.wait_for_selector,
        wait_until=args.wait_until,
    )
    cleaner = HtmlCleaner(
        keep_links=not args.no_links,
        keep_images=not args.no_images,
    )
    if args.markdown:
        output = cleaner.to_markdown(fetched.html)
    else:
        output = cleaner.clean(fetched.html)

    out_bytes = len(output.encode("utf-8"))
    reduction = (1 - out_bytes / fetched.raw_bytes) * 100 if fetched.raw_bytes else 0
    print(
        f"Raw: {fetched.raw_bytes:,} bytes → Cleaned: {out_bytes:,} bytes "
        f"({reduction:.1f}% reduction)",
        file=sys.stderr,
    )
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape a URL and output LLM-friendly HTML or Markdown.",
    )
    p.add_argument("--url", required=True, help="URL to scrape")
    p.add_argument(
        "--wait",
        type=float,
        default=2,
        help="Seconds to wait after page load (default: 2)",
    )
    p.add_argument("--wait-for-selector", default=None)
    p.add_argument(
        "--wait-until",
        default="domcontentloaded",
        choices=["load", "domcontentloaded", "networkidle"],
    )
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--no-links", action="store_true")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--output", default=None)
    return p


def main() -> None:
    args = build_parser().parse_args()
    output = asyncio.run(scrape(args))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
