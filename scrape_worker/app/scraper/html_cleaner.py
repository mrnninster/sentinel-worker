"""
Reusable HTML cleaner for producing LLM-friendly output.

Strips non-semantic noise (scripts, styles, nav, ads, etc.) from raw HTML,
returning either cleaned HTML or GitHub-Flavored Markdown.  Designed to
reduce token counts by 70-90 % while preserving meaningful structure.

Usage::

    from app.scraper.html_cleaner import HtmlCleaner

    cleaner = HtmlCleaner()
    cleaned_html = cleaner.clean(raw_html)
    markdown     = cleaner.to_markdown(raw_html)

Options can be toggled per-instance::

    cleaner = HtmlCleaner(keep_links=False, keep_images=False)
"""

from __future__ import annotations

import re
from typing import List

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Phase 1 — tags to decompose (remove entirely, including children)
_DECOMPOSE_TAGS = frozenset(
    [
        # Scripts / styles
        "script",
        "style",
        "link",
        "noscript",
        "iframe",
        "frame",
        "frameset",
        # Media
        "svg",
        "canvas",
        "video",
        "audio",
        "picture",
        "source",
        # Forms
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "label",
        "fieldset",
        "legend",
        "datalist",
        "output",
        "option",
        "optgroup",
        # Metadata / embedded objects
        "meta",
        "head",
        "object",
        "embed",
        "param",
        "map",
        "area",
    ]
)

# ARIA roles whose containers are almost always boilerplate
_BOILERPLATE_ROLES = frozenset(
    ["navigation", "banner", "contentinfo", "search", "complementary"]
)

# Structural tags that are boilerplate by definition
_BOILERPLATE_TAGS = frozenset(["nav", "header", "footer", "aside"])

# Regex for class/id substrings that signal boilerplate containers
_BOILERPLATE_RE = re.compile(
    r"nav|navbar|sidebar|breadcrumb|cookie|consent|"
    r"\bad[s]?\b|advert|tracking|modal|popup|"
    r"social[-_]?share|share[-_]?bar|"
    r"skip[-_]?link|back[-_]?to[-_]?top|"
    r"footer|header|banner|menu|toolbar",
    re.IGNORECASE,
)

# Block-level tags that can be pruned when empty
_EMPTY_BLOCK_TAGS = frozenset(
    ["div", "span", "p", "section", "article", "main", "figure", "figcaption"]
)

# Tags whose text content contributes to "meaningful" content
_HEADING_TAGS = frozenset(["h1", "h2", "h3", "h4", "h5", "h6"])

# Blank-line compressor: 3+ newlines → 2
_MULTI_BLANK = re.compile(r"\n{3,}")

# Whitespace normaliser for inline text
_INLINE_WS = re.compile(r"[ \t]+")


# ---------------------------------------------------------------------------
# HtmlCleaner
# ---------------------------------------------------------------------------


class HtmlCleaner:
    """Three-phase HTML cleaner producing LLM-friendly output."""

    def __init__(self, *, keep_links: bool = True, keep_images: bool = True):
        self.keep_links = keep_links
        self.keep_images = keep_images

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clean(self, html: str) -> str:
        """Return cleaned HTML string."""
        soup = self._parse_and_clean(html)
        out = soup.decode_contents()
        return _MULTI_BLANK.sub("\n\n", out).strip()

    def to_markdown(self, html: str) -> str:
        """Return cleaned Markdown string."""
        soup = self._parse_and_clean(html)
        md = self._html_to_markdown(soup)
        return _MULTI_BLANK.sub("\n\n", md).strip()

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    def _parse_and_clean(self, html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        self._phase1_decompose(soup)
        self._phase2_strip_attrs(soup)
        self._phase3_compress(soup)
        return soup

    # -- Phase 1: Decompose ------------------------------------------------

    def _phase1_decompose(self, soup: BeautifulSoup) -> None:
        # Remove HTML comments
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        # Remove decompose-list tags (copy list — decompose mutates the tree)
        for tag in list(soup.find_all(_DECOMPOSE_TAGS)):
            if tag.attrs is None:
                continue
            tag.decompose()

        # Remove boilerplate by ARIA role
        for tag in list(soup.find_all(attrs={"role": True})):
            if tag.attrs is None:
                continue
            if (tag.get("role") or "").lower() in _BOILERPLATE_ROLES:
                tag.decompose()

        # Remove boilerplate structural tags
        for tag in list(soup.find_all(_BOILERPLATE_TAGS)):
            if tag.attrs is None:
                continue
            tag.decompose()

        # Remove div/section whose id or class matches boilerplate keywords
        for tag in list(soup.find_all(["div", "section"])):
            # Decomposing an ancestor can leave stale Tag objects with attrs=None
            if tag.attrs is None:
                continue
            cls = " ".join(tag.get("class") or [])
            tag_id = tag.get("id") or ""
            if _BOILERPLATE_RE.search(cls) or _BOILERPLATE_RE.search(tag_id):
                tag.decompose()

    # -- Phase 2: Strip attributes -----------------------------------------

    def _phase2_strip_attrs(self, soup: BeautifulSoup) -> None:
        for tag in list(soup.find_all(True)):
            if tag.attrs is None:
                continue
            name = tag.name

            if name == "a":
                href = tag.get("href", "")
                tag.attrs = {}
                if (
                    self.keep_links
                    and href
                    and not href.startswith("javascript:")
                    and href != "#"
                ):
                    tag["href"] = href
                # If not keeping links, the tag stays but has no href
                # (Phase 3 won't remove it since it has text children)

            elif name == "img":
                alt = (tag.get("alt") or "").strip()
                if self.keep_images and alt:
                    tag.replace_with(f"[Image: {alt}]")
                else:
                    tag.decompose()

            else:
                tag.attrs = {}

    # -- Phase 3: Compress --------------------------------------------------

    def _phase3_compress(self, soup: BeautifulSoup) -> None:
        changed = True
        while changed:
            changed = False
            for tag in list(soup.find_all(_EMPTY_BLOCK_TAGS)):
                if tag.attrs is None:
                    continue
                if not tag.get_text(strip=True) and not tag.find_all(
                    ["table", "ul", "ol", "dl", "pre", "blockquote"]
                ):
                    tag.decompose()
                    changed = True

    # ------------------------------------------------------------------
    # Markdown converter
    # ------------------------------------------------------------------

    def _html_to_markdown(self, soup: BeautifulSoup) -> str:
        parts: List[str] = []
        self._walk(soup, parts)
        return "".join(parts)

    def _walk(self, node, parts: List[str]) -> None:
        if isinstance(node, NavigableString):
            text = _INLINE_WS.sub(" ", str(node))
            parts.append(text)
            return

        if not isinstance(node, Tag):
            return

        name = node.name

        # --- Headings ---
        if name in _HEADING_TAGS:
            level = int(name[1])
            inner = self._collect_inline(node)
            if inner.strip():
                parts.append(f"\n\n{'#' * level} {inner.strip()}\n\n")
            return

        # --- Paragraphs ---
        if name == "p":
            inner = self._collect_inline(node)
            if inner.strip():
                parts.append(f"\n\n{inner.strip()}\n\n")
            return

        # --- Links ---
        if name == "a":
            href = node.get("href", "")
            text = self._collect_inline(node).strip()
            if href and text:
                parts.append(f"[{text}]({href})")
            elif text:
                parts.append(text)
            return

        # --- Emphasis ---
        if name in ("strong", "b"):
            inner = self._collect_inline(node).strip()
            if inner:
                parts.append(f"**{inner}**")
            return

        if name in ("em", "i"):
            inner = self._collect_inline(node).strip()
            if inner:
                parts.append(f"*{inner}*")
            return

        # --- Code ---
        if name == "code":
            inner = node.get_text()
            parts.append(f"`{inner}`")
            return

        if name == "pre":
            inner = node.get_text()
            parts.append(f"\n\n```\n{inner.strip()}\n```\n\n")
            return

        # --- Blockquotes ---
        if name == "blockquote":
            inner = self._collect_inline(node).strip()
            if inner:
                quoted = "\n".join(f"> {line}" for line in inner.split("\n"))
                parts.append(f"\n\n{quoted}\n\n")
            return

        # --- Unordered lists ---
        if name == "ul":
            parts.append("\n\n")
            for li in node.find_all("li", recursive=False):
                text = self._collect_inline(li).strip()
                if text:
                    parts.append(f"- {text}\n")
            parts.append("\n")
            return

        # --- Ordered lists ---
        if name == "ol":
            parts.append("\n\n")
            for i, li in enumerate(node.find_all("li", recursive=False), 1):
                text = self._collect_inline(li).strip()
                if text:
                    parts.append(f"{i}. {text}\n")
            parts.append("\n")
            return

        # --- Definition lists ---
        if name == "dl":
            parts.append("\n\n")
            for child in node.children:
                if isinstance(child, Tag):
                    text = self._collect_inline(child).strip()
                    if child.name == "dt" and text:
                        parts.append(f"**{text}**\n")
                    elif child.name == "dd" and text:
                        parts.append(f": {text}\n")
            parts.append("\n")
            return

        # --- Tables (GFM pipe tables) ---
        if name == "table":
            self._table_to_markdown(node, parts)
            return

        # --- Horizontal rule ---
        if name == "hr":
            parts.append("\n\n---\n\n")
            return

        # --- Line break ---
        if name == "br":
            parts.append("\n")
            return

        # --- Generic containers: recurse ---
        for child in node.children:
            self._walk(child, parts)

    def _collect_inline(self, node) -> str:
        """Collect inline text from a node's children (avoids re-entering the
        block-level handler for *node* itself)."""
        parts: List[str] = []
        for child in node.children:
            self._walk(child, parts)
        return "".join(parts)

    def _table_to_markdown(self, table: Tag, parts: List[str]) -> None:
        """Convert an HTML table to a GFM pipe table."""
        rows: List[List[str]] = []

        # Gather header rows from <thead>
        thead = table.find("thead")
        tbody = table.find("tbody")

        row_sources = []
        if thead:
            row_sources.extend(thead.find_all("tr", recursive=False))
        if tbody:
            row_sources.extend(tbody.find_all("tr", recursive=False))
        if not row_sources:
            row_sources = table.find_all("tr", recursive=False)

        for tr in row_sources:
            cells = tr.find_all(["th", "td"], recursive=False)
            row = [self._collect_inline(c).strip().replace("|", "\\|") for c in cells]
            rows.append(row)

        if not rows:
            return

        # Normalize column count
        max_cols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < max_cols:
                r.append("")

        # Determine if first row is a header (came from <thead> or contains <th>)
        has_header = thead is not None or (
            row_sources
            and row_sources[0].find("th") is not None
        )

        parts.append("\n\n")

        if has_header and rows:
            header = rows[0]
            parts.append("| " + " | ".join(header) + " |\n")
            parts.append("| " + " | ".join("---" for _ in header) + " |\n")
            data_rows = rows[1:]
        else:
            # Fabricate an empty header
            parts.append("| " + " | ".join("" for _ in range(max_cols)) + " |\n")
            parts.append("| " + " | ".join("---" for _ in range(max_cols)) + " |\n")
            data_rows = rows

        for row in data_rows:
            parts.append("| " + " | ".join(row) + " |\n")

        parts.append("\n")
