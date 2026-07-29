import re
import sys
import os
import pytz
import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper

log = logging.getLogger(__name__)


class Rumble:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.max_concurrent_requests = (
            5  # Limit concurrent requests to avoid rate limiting
        )
        self.page_urls = []  # Store page URLs separately for async extraction
        self.self_contained_parser = True

    def rumble_table(self, url: str, timezone: str = "America/Chicago") -> list:
        """
        Scrapes meeting archive videos from Rumble channel pages.

        Args:
            soup: BeautifulSoup object of the page
            url: Base URL of the Rumble channel
            timezone: Timezone string (default: "America/Chicago")

        Returns:
            List of meeting dictionaries
        """
        html = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html)
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Find all video listing items on Rumble
        # Rumble uses various container classes for video listings
        video_containers = soup.find_all(
            ["article", "li"],
            class_=re.compile(r"video-listing|video-item|media-list-item"),
        )

        # If the above doesn't work, try finding by common Rumble structure
        if not video_containers:
            # Try alternative selectors
            video_containers = soup.find_all("li", class_=re.compile(r"video-listing"))

        # If still no results, try finding all links that look like video titles
        if not video_containers:
            # Look for h3 or h2 elements that might contain video titles
            video_containers = soup.find_all(
                ["h3", "h2"], class_=re.compile(r"video-item--title|title")
            )
            if video_containers:
                # Get parent containers
                video_containers = [
                    elem.find_parent(["article", "li", "div"])
                    for elem in video_containers
                ]
                video_containers = [
                    c for c in video_containers if c
                ]  # Filter out None values

        if not video_containers:
            log.warning("Warning: No video containers found on the page")
            return self.meetings

        for container in video_containers:
            try:
                # Initialize date_text to avoid scope issues
                date_text = None

                # Extract meeting name
                title_elem = container.find(
                    ["h3", "h2", "a"],
                    class_=re.compile(r"title|video-item--title"),
                )
                if not title_elem:
                    title_elem = container.find("a", href=re.compile(r"/v[a-z0-9]+"))

                if not title_elem:
                    continue

                meeting_name = title_elem.get_text(strip=True)

                # Extract video link
                video_link_elem = (
                    title_elem if title_elem.name == "a" else title_elem.find("a")
                )
                if not video_link_elem:
                    video_link_elem = container.find(
                        "a", href=re.compile(r"/v[a-z0-9]+")
                    )

                meeting_link = None
                if video_link_elem and video_link_elem.get("href"):
                    meeting_link = urljoin(url, video_link_elem.get("href"))

                # Extract date information (Rumble shows relative dates like "18 days ago")
                date_elem = container.find(
                    ["time", "span"], class_=re.compile(r"date|time|uploaded")
                )
                if not date_elem:
                    # Try finding text that matches relative date patterns
                    text_content = container.get_text()
                    date_match = re.search(
                        r"(\d+)\s+(month|day|week|hour)s?\s+ago",
                        text_content,
                        re.IGNORECASE,
                    )
                    if date_match:
                        date_text = date_match.group(0)
                    else:
                        # Try to extract date from the meeting name (e.g., "22OCT2025")
                        date_in_name = re.search(
                            r"(\d{1,2})([A-Z]{3})(\d{4})", meeting_name
                        )
                        if date_in_name:
                            day = int(date_in_name.group(1))
                            month_str = date_in_name.group(2)
                            year = int(date_in_name.group(3))

                            # Convert month abbreviation to month number
                            month_map = {
                                "JAN": 1,
                                "FEB": 2,
                                "MAR": 3,
                                "APR": 4,
                                "MAY": 5,
                                "JUN": 6,
                                "JUL": 7,
                                "AUG": 8,
                                "SEP": 9,
                                "OCT": 10,
                                "NOV": 11,
                                "DEC": 12,
                            }
                            month = month_map.get(month_str.upper(), 1)

                            # Create datetime object
                            # NOTE: Time is estimated as 6 PM since exact time is not in title
                            # This is a reasonable default for evening school board meetings
                            # The actual meeting time may differ - consider this approximate
                            meeting_date_time_local = tz.localize(
                                datetime(year, month, day, 18, 0, 0)
                            )
                        else:
                            # Default to now if we can't extract date
                            meeting_date_time_local = now
                else:
                    date_text = date_elem.get_text(strip=True)

                # Parse relative date if we have date_text
                if date_text:
                    meeting_date_time_local = self._parse_relative_date(date_text, tz)

                # Convert to UTC
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                meeting_date_time = meeting_date_time_utc.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )

                # Determine status - since these are uploaded videos, they're all "Ended"
                if meeting_date_time_local > now:
                    status = "Upcoming"
                else:
                    status = "Ended"

                # Check for cancelled meetings
                if re.search(r"cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"

                agenda_link = None

                # Store meeting info (video URL extraction happens later in batch)
                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,  # Store page URL temporarily
                        "Agenda link": agenda_link,
                        "Status": status,
                        "_page_url": meeting_link,  # Keep page URL for async extraction
                    }
                )

                # Store page URL separately for async extraction
                self.page_urls.append(meeting_link)

            except Exception as e:
                log.warning(f"Error processing video container: {str(e)}")
                continue

        # Batch extract direct video URLs asynchronously for better performance
        if self.meetings:
            log.info(
                f"Extracting direct video URLs for {len(self.meetings)} meetings (async batch processing)..."
            )
            self._batch_extract_video_urls()

        return self.meetings

    def _batch_extract_video_urls(self):
        """
        Extract direct video URLs for all meetings asynchronously.
        This is much faster than sequential extraction.
        """
        # Run async extraction in an event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        loop.run_until_complete(self._async_extract_all_video_urls())

    async def _async_extract_all_video_urls(self):
        """
        Asynchronously extract video URLs for all meetings with concurrency control.
        """
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(self.max_concurrent_requests)

        async def extract_with_semaphore(index):
            async with semaphore:
                meeting = self.meetings[index]
                page_url = self.page_urls[index]

                if not page_url:
                    return

                meeting_name = meeting.get("Meeting name", "Unknown")
                log.debug(f"  Extracting: {meeting_name[:50]}...")

                try:
                    # Run the synchronous scraper in a thread pool
                    loop = asyncio.get_event_loop()
                    direct_url = await loop.run_in_executor(
                        None, self._extract_direct_video_url, page_url
                    )

                    if direct_url:
                        meeting["Meeting link"] = direct_url
                        log.debug(f"    ✓ Found: {direct_url[:60]}...")
                    else:
                        log.debug(f"    ✗ No direct URL found")

                except Exception as e:
                    log.warning(f"    ✗ Error: {str(e)}")

        # Process all meetings concurrently
        await asyncio.gather(
            *[extract_with_semaphore(i) for i in range(len(self.meetings))]
        )

    def _parse_relative_date(self, date_text, tz):
        """
        Parse relative date strings like "18 days ago" or "1 month ago"

        Args:
            date_text: String containing relative date
            tz: Timezone object

        Returns:
            datetime object
        """
        now = datetime.now(tz)

        # Match patterns like "X days/weeks/months/hours ago"
        match = re.search(
            r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
            date_text,
            re.IGNORECASE,
        )

        if not match:
            return now

        amount = int(match.group(1))
        unit = match.group(2).lower()

        if unit == "second":
            return now - timedelta(seconds=amount)
        elif unit == "minute":
            return now - timedelta(minutes=amount)
        elif unit == "hour":
            return now - timedelta(hours=amount)
        elif unit == "day":
            return now - timedelta(days=amount)
        elif unit == "week":
            return now - timedelta(weeks=amount)
        elif unit == "month":
            return now - relativedelta(months=amount)
        elif unit == "year":
            return now - relativedelta(years=amount)
        else:
            return now

    def _categorize_video_url(self, url, all_video_urls, faa_urls):
        """
        Helper to categorize video URLs (optimized to avoid repeated checks).

        Args:
            url: URL to categorize
            all_video_urls: List to append non-Faa URLs
            faa_urls: List to append Faa URLs
        """
        if ".Faa.mp4" in url:
            faa_urls.append(url)
        else:
            all_video_urls.append(url)

    def _extract_urls_from_text(self, text, all_video_urls, faa_urls):
        """
        Extract and categorize MP4 URLs from text content (optimized single-pass).

        Args:
            text: Text content to search
            all_video_urls: List to append non-Faa URLs
            faa_urls: List to append Faa URLs
        """
        # Single regex pass to find all MP4 URLs
        mp4_matches = re.findall(r'(https://[^"\'<>\s]+\.mp4(?:\?[^"\'<>\s]*)?)', text)
        for match in mp4_matches:
            # Filter for Rumble CDN URLs
            if "1a-" in match or "video" in match.lower():
                self._categorize_video_url(match, all_video_urls, faa_urls)

    def _extract_direct_video_url(self, video_page_url):
        """
        Extract the direct MP4 video URL from a Rumble video page.
        Prioritizes .caa.mp4 URLs (audio+video) over .Faa.mp4 URLs (video-only).
        Optimized to minimize repeated regex searches.

        Args:
            video_page_url: URL of the individual video page

        Returns:
            Direct MP4 video URL with audio or None if not found
        """
        try:
            # Scrape the individual video page
            html = self.scraper.scrape_html(url=video_page_url, render="true")
            if isinstance(html, dict) and "max_failure" in html:
                return None

            soup = self.scraper.convert_to_soup(html)

            all_video_urls = []
            faa_urls = []  # Separate list for video-only URLs

            # OPTIMIZED: Search all text-based sources in one pass
            # 1. Scripts (highest priority - may have .caa URLs)
            for script in soup.find_all("script"):
                if script.string:
                    self._extract_urls_from_text(
                        script.string, all_video_urls, faa_urls
                    )

            # 2. Style tags
            for style in soup.find_all("style"):
                if style.string:
                    self._extract_urls_from_text(style.string, all_video_urls, faa_urls)

            # 3. Data attributes (consolidated check)
            for elem in soup.find_all(
                attrs=lambda x: any(
                    attr in x for attr in ["data-src", "data-video", "data-url"]
                )
            ):
                for attr in ["data-src", "data-video", "data-url"]:
                    src = elem.get(attr)
                    if src and ".mp4" in src and src.startswith("http"):
                        self._categorize_video_url(src, all_video_urls, faa_urls)

            # 4. Video tags (often have .Faa URLs)
            for video in soup.find_all("video"):
                src = video.get("src")
                if (
                    src
                    and not src.startswith("blob:")
                    and ".mp4" in src
                    and src.startswith("http")
                ):
                    self._categorize_video_url(src, all_video_urls, faa_urls)

                # Check source tags within video
                for source in video.find_all("source"):
                    src = source.get("src")
                    if (
                        src
                        and not src.startswith("blob:")
                        and ".mp4" in src
                        and src.startswith("http")
                    ):
                        self._categorize_video_url(src, all_video_urls, faa_urls)

            # Prioritize URLs with audio over video-only URLs
            # Order: .caa.mp4 with params > .caa.mp4 > any with params > .Faa as last resort

            # First priority: .caa.mp4 with query parameters (audio+video, best quality)
            for url in all_video_urls:
                if ".caa.mp4?" in url:
                    return url

            # Second priority: .caa.mp4 without query parameters
            for url in all_video_urls:
                if ".caa.mp4" in url:
                    return url

            # Third priority: any .mp4 with query parameters (may have audio)
            for url in all_video_urls:
                if ".mp4?" in url:
                    return url

            # Fourth priority: any non-.Faa URL found
            if all_video_urls:
                return all_video_urls[0]

            # Last resort: .Faa.mp4 URLs (video-only, no audio)
            if faa_urls:
                # Prefer .Faa with parameters over plain .Faa
                for url in faa_urls:
                    if "?" in url:
                        return url
                return faa_urls[0]

            return None

        except Exception as e:
            log.warning(f"  Error: {str(e)}")
            return None


if __name__ == "__main__":

    # Test with Osceola WI School Board
    run_test(
        url="https://rumble.com/c/c-1275162/videos",
        schedule_type="rumble_table",
        timezone="America/Chicago",
        get_full_archive_flag=True,
    )
