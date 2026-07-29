import os
import re
import sys
import logging
import asyncio
import pytz
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.playwright_utils import BrowserManager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Wait times for page loading
PAGE_LOAD_WAIT = 10  # seconds - wait for Invintus widgets to fully load


class Oregonlegislature:
    """
    Self-contained scraper for the Oregon State Legislature using Playwright.

    This scraper uses Playwright to load the Legislative Video page which contains
    Invintus event listing widgets. It distinguishes between "In Progress" (live)
    meetings and "Upcoming" meetings based on Live span tags in the HTML.

    Example request format:
    {
        "geodicts": [
            {
                "schedule_type": "unique_oregonlegislature",
                "url": "https://www.oregonlegislature.gov/citizen_engagement/Pages/Legislative-Video.aspx",
                "timezone": "America/Los_Angeles",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": ""
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.base_url = "https://www.oregonlegislature.gov"

    async def unique_oregonlegislature(
        self,
        url="https://www.oregonlegislature.gov/citizen_engagement/Pages/Legislative-Video.aspx",
        timezone="America/Los_Angeles",
    ):
        """
        Scrapes the Oregon State Legislature Legislative Video page using Playwright.

        Args:
            url: URL of the Oregon Legislature Legislative Video page to scrape.
                 Defaults to https://www.oregonlegislature.gov/citizen_engagement/Pages/Legislative-Video.aspx
            timezone: Timezone for event times. Defaults to America/Los_Angeles

        Returns:
            List of meeting dictionaries with Meeting name, Scheduled time,
            Meeting link, Agenda link, and Status (In Progress/Upcoming/Cancelled)
        """
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()

            meetings = []
            tz = pytz.timezone(timezone)

            # Navigate to the page and wait for network idle
            await page.goto(url, wait_until="networkidle", timeout=60000)
            # Additional sleep for Invintus JavaScript widgets to fully render
            await asyncio.sleep(PAGE_LOAD_WAIT)

            # Extract events from both Live and Scheduled sections
            # Note: Selectors use wildcards to handle CSS-in-JS generated class hashes
            # that may change when the site is rebuilt
            events_data = await page.evaluate(
                """
                () => {
                    // Helper function to find element with fallback strategies
                    function findElement(parent, selectors) {
                        for (const selector of selectors) {
                            const el = parent.querySelector(selector);
                            if (el) return el;
                        }
                        return null;
                    }

                    // Helper function to extract event data from a section
                    function extractEventsFromSection(sectionClass) {
                        const section = document.querySelector(sectionClass);
                        if (!section) return [];

                        const eventRows = section.querySelectorAll('.table-event');
                        const events = [];

                        eventRows.forEach(row => {
                            try {
                                // Check for Live span using multiple strategies
                                // The 'Live' text appears in a nested span within a Status wrapper
                                let liveSpan = findElement(row, [
                                    '[class*="table__Status"] span',  // Status wrapper > span
                                    'span[class*="Status"] span',      // Alternative Status class
                                ]);

                                // Fallback: search all spans in the row for "Live" text
                                if (!liveSpan) {
                                    const allSpans = row.querySelectorAll('span');
                                    for (const span of allSpans) {
                                        if (span.textContent.trim() === 'Live') {
                                            liveSpan = span;
                                            break;
                                        }
                                    }
                                }

                                const isLive = liveSpan && liveSpan.textContent.trim() === 'Live';

                                // Get title cell using wildcard attribute selector
                                // Matches any class containing 'table__Title' regardless of hash
                                const titleCell = findElement(row, [
                                    '[class*="table__Title"]',
                                    '.table-event > div > [class*="Title"]',
                                ]);
                                let title = titleCell ? titleCell.textContent : '';

                                // Remove 'Title' label, Live badge, and On Break status from title
                                title = title.replace(/^Title/, '').replace(/Live/, '').replace(/On Break/, '').trim();

                                // Get date cell using wildcard selector
                                const dateCell = findElement(row, [
                                    '[class*="table__Date"]',
                                    '.table-event > div > [class*="Date"]',
                                ]);
                                let dateText = dateCell ? dateCell.textContent : '';
                                dateText = dateText.replace(/^Date/, '').trim();

                                // Get links cell using wildcard selector
                                const linksCell = findElement(row, [
                                    '[class*="table__Links"]',
                                    '.table-event > div > [class*="Links"]',
                                ]);
                                const videoLink = linksCell ? linksCell.querySelector('a[href*="mediaplayer"]') : null;
                                const videoUrl = videoLink ? videoLink.getAttribute('href') : null;

                                // Get agenda link if available
                                const agendaLink = row.querySelector('a[href*="Agenda"]');
                                const agendaUrl = agendaLink ? agendaLink.getAttribute('href') : null;

                                // Only add event if we got required data
                                if (title && dateText) {
                                    events.push({
                                        title: title,
                                        date: dateText,
                                        videoUrl: videoUrl,
                                        agendaUrl: agendaUrl,
                                        isLive: isLive
                                    });
                                }
                            } catch (err) {
                                // Skip individual events that fail to parse
                                console.error('Error parsing event:', err);
                            }
                        });

                        return events;
                    }

                    // Extract from Live section (preference-252)
                    const liveEvents = extractEventsFromSection('.invintus-event-listing-preference-252');

                    // Extract from Scheduled section (preference-253)
                    const scheduledEvents = extractEventsFromSection('.invintus-event-listing-preference-253');

                    return {
                        liveEvents: liveEvents,
                        scheduledEvents: scheduledEvents
                    };
                }
            """
            )

            # Process Live events
            for event in events_data.get("liveEvents", []):
                meeting = self._process_event(event, tz)
                if meeting:
                    meetings.append(meeting)

            # Process Scheduled events
            for event in events_data.get("scheduledEvents", []):
                meeting = self._process_event(event, tz)
                if meeting:
                    meetings.append(meeting)

            log.info(f"Found {len(meetings)} meetings from Oregon Legislature")
            self.meetings = meetings
            return meetings

        finally:
            await browser_manager.close_browser()

    def _process_event(self, event, tz):
        """
        Process a single event from the Invintus widget.

        Args:
            event: Event dictionary from JavaScript extraction
            tz: pytz timezone object

        Returns:
            Meeting dictionary or None if parsing fails
        """
        try:
            # Extract meeting name and date from title
            # Format: "Meeting Name 01/14/2026 2:30 PM"
            title_full = event.get("title", "")
            date_text = event.get("date", "")

            # Parse the date from the date field: "01/14/2026 — 02:30 PM"
            date_match = re.search(
                r"(\d{2}/\d{2}/\d{4})\s*—\s*(\d{1,2}:\d{2}\s*[AP]M)", date_text
            )
            if not date_match:
                log.warning(f"Could not parse date from: {date_text}")
                return None

            date_str = date_match.group(1)  # "01/14/2026"
            time_str = date_match.group(2)  # "02:30 PM"

            # Combine and parse
            datetime_str = f"{date_str} {time_str}"
            meeting_dt = datetime.strptime(datetime_str, "%m/%d/%Y %I:%M %p")

            # Localize to the given timezone
            meeting_dt = tz.localize(meeting_dt)

            # Convert to UTC
            meeting_dt_utc = meeting_dt.astimezone(pytz.utc)
            meeting_date_time = meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # Extract meeting name by removing the date/time portion from title
            # The title contains both name and datetime, we need just the name
            meeting_name = re.sub(
                r"\s*\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M\s*$", "", title_full
            ).strip()

            # Determine status based on Live span presence
            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            elif event.get("isLive"):
                status = "In Progress"
            else:
                status = "Upcoming"

            # Process video link
            video_url = event.get("videoUrl")
            meeting_link = None
            if video_url:
                # Convert mediaplayer URL to HLS stream URL
                if "mediaplayer" in video_url.lower():
                    hls_url = self._process_invintus_mediaplayer_url(video_url)
                    meeting_link = hls_url if hls_url else video_url
                else:
                    meeting_link = video_url

            # Process agenda link
            agenda_link = event.get("agendaUrl")
            if agenda_link and not agenda_link.startswith("http"):
                agenda_link = self.base_url + agenda_link

            return {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
            }

        except Exception as e:
            log.warning(f"Error processing event: {e}", exc_info=True)
            return None

    def _process_invintus_mediaplayer_url(self, mediaplayer_url: str) -> str:
        """
        Process Invintus mediaplayer URL to extract HLS stream URL.
        Extracts clientID and eventID from the URL and constructs the HLS stream URL.

        Args:
            mediaplayer_url: URL like https://olis.oregonlegislature.gov/liz/mediaplayer?clientID=4879615486&eventID=2025121006

        Returns:
            HLS stream URL like https://api.v3.invintus.com/StreamURI/hls/4879615486/2025121006/media.m3u8
            or None if extraction fails
        """
        try:
            # Parse URL to extract query parameters
            parsed_url = urlparse(mediaplayer_url)
            query_params = parse_qs(parsed_url.query)

            client_id = query_params.get("clientID", [None])[0]
            event_id = query_params.get("eventID", [None])[0]

            # Try regex fallback if URL parsing fails
            if not client_id or not event_id:
                match = re.search(r"clientID=(\d+).*eventID=(\d+)", mediaplayer_url)
                if match:
                    client_id = match.group(1)
                    event_id = match.group(2)

            if client_id and event_id:
                # Construct HLS stream URL
                hls_url = f"https://api.v3.invintus.com/StreamURI/hls/{client_id}/{event_id}/media.m3u8"
                log.debug(f"Extracted Invintus HLS URL: {hls_url}")
                return hls_url
        except Exception as e:
            log.warning(
                f"Failed to process Invintus mediaplayer URL {mediaplayer_url}: {e}"
            )

        return None


if __name__ == "__main__":
    run_test(
        url="https://www.oregonlegislature.gov/citizen_engagement/Pages/Legislative-Video.aspx",
        schedule_type="unique_oregonlegislature",
    )
