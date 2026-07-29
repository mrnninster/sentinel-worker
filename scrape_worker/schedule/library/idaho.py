# idaho.py

from datetime import datetime
from typing import Optional, Tuple
import json
import logging
import os
import re
import pytz
import requests
import pdfplumber
from io import BytesIO
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse, urljoin
from dateutil import parser
from dateutil.relativedelta import relativedelta
from bs4 import BeautifulSoup
from utils.pdf_text import extract_pdf_text_from_bytes

from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    import sys

    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

from utils.scrape_html import HtmlScraper

MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Idaho:
    def __init__(self):
        self.base_url = None
        self.url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        # Get API key from scraper object (it loads from env)
        self.scraper_api_key = self.scraper.SCRAPERAPICOM_API_KEY
        # Cache for scraped URLs to avoid redundant API calls
        self._scraped_url_cache = {}
        # Maximum number of nested scrapes per agenda
        self._max_nested_scrapes = 3

    def idaho_table(self, url: str, timezone: str = "America/Denver") -> list:
        # Reset cache for each new scrape session
        self._scraped_url_cache = {}
        self.timezone = timezone
        self.url = url
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get current date in geo timezone and calculate next month
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        current_year = now.year
        current_month = now.month

        # Calculate next month (handle year rollover)
        next_date = now + relativedelta(months=1)
        next_year = next_date.year
        next_month = next_date.month

        # Parse the base URL to extract parameters
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)

        # Get the calendar ID if it exists
        cid = query_params.get("cid", [""])[0]

        # Build URLs for current month and next month
        urls_to_scrape = []

        # Current month URL
        current_params = {
            "yr": str(current_year),
            "month": str(current_month),
            "dy": "",
            "cid": cid,
        }
        current_url = urlunparse(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                parsed_url.path,
                parsed_url.params,
                urlencode(current_params),
                parsed_url.fragment,
            )
        )
        urls_to_scrape.append((current_year, current_month, current_url))

        # Next month URL
        next_params = {
            "yr": str(next_year),
            "month": str(next_month),
            "dy": "",
            "cid": cid,
        }
        next_url = urlunparse(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                parsed_url.path,
                parsed_url.params,
                urlencode(next_params),
                parsed_url.fragment,
            )
        )
        urls_to_scrape.append((next_year, next_month, next_url))

        # Scrape both months
        tz_info = pytz.timezone(timezone)

        for year, month, scrape_url in urls_to_scrape:
            log.info(
                f"Scraping Idaho Legislature calendar for {year}-{month:02d}: {scrape_url}"
            )
            # Use the original method with render enabled
            soup = self._get_page_soup_using_scraperapi(scrape_url)
            if not soup:
                log.warning(f"Failed to fetch page for {year}-{month:02d}")
                continue

            # Find the calendar table - try multiple approaches
            calendar_table = soup.find("table", class_="my-calendar-table")
            if not calendar_table:
                # Try finding by class regex
                calendar_table = soup.find(
                    "table", class_=re.compile(r"my-calendar|calendar")
                )
            if not calendar_table:
                # Try finding any table
                all_tables = soup.find_all("table")
                log.warning(
                    f"Calendar table not found for {year}-{month:02d}. "
                    f"Found {len(all_tables)} table(s)."
                )
                # Check for the table by ID or other attributes
                calendar_table = soup.find(
                    "table", id=re.compile(r"calendar|my-calendar")
                )
                if not calendar_table:
                    # Try finding divs that might contain calendar
                    calendar_divs = soup.find_all("div", class_=re.compile(r"calendar"))
                    log.warning(
                        f"Found {len(calendar_divs)} div(s) with 'calendar' in class"
                    )
                    if calendar_divs:
                        # Check if table is inside a calendar div
                        for div in calendar_divs:
                            calendar_table = div.find("table")
                            if calendar_table:
                                log.info(f"Found table inside calendar div")
                                break
                if not calendar_table:
                    continue

            # Find all cells with events
            event_cells = calendar_table.find_all(
                "td", class_=re.compile(r"mc-events|has-events")
            )

            for cell in event_cells:
                # Extract date from screen-reader-text
                date_span = cell.find("span", class_="screen-reader-text")
                if not date_span:
                    continue

                date_text = date_span.get_text(strip=True)
                # Parse date like "December 2, 2025"
                try:
                    date_match = re.search(
                        r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", date_text
                    )
                    if not date_match:
                        continue
                    month_name, day_str, year_str = date_match.groups()
                    event_day = int(day_str)
                    event_year = int(year_str)
                    event_month = datetime.strptime(month_name, "%B").month
                except (ValueError, AttributeError) as e:
                    log.warning(f"Could not parse date from '{date_text}': {e}")
                    continue

                # Find all event articles in this cell
                event_articles = cell.find_all("article", class_="calendar-event")

                for article in event_articles:
                    # Extract meeting name and time from button
                    button = article.find("button", class_="calendar")
                    if not button:
                        continue

                    button_div = button.find("div")
                    if not button_div:
                        continue

                    button_text = button_div.get_text(strip=True)

                    # Extract meeting name first (needed for logging)
                    meeting_name = re.sub(
                        r"^\d{1,2}:\d{2}\s*[ap]m:\s*",
                        "",
                        button_text,
                        flags=re.IGNORECASE,
                    )
                    meeting_name = meeting_name.strip()
                    if not meeting_name:
                        # Try getting from event title
                        title_h3 = article.find("h3", class_="event-title")
                        if title_h3:
                            meeting_name = title_h3.get_text(strip=True)

                    if not meeting_name:
                        continue

                    # Extract agenda link from details section first (needed for time extraction fallback)
                    agenda_link = None
                    meeting_link = None
                    details_div = article.find("div", class_="details")
                    if details_div:
                        links = details_div.find_all("a", href=True)
                        for link in links:
                            href = link.get("href", "")
                            link_text = link.get_text(strip=True)
                            if (
                                "agenda" in link_text.lower()
                                or "pdf" in link_text.lower()
                                or href.endswith(".pdf")
                            ):
                                agenda_link = urljoin(self.base_url, href)
                                break

                    # Check for all-day events (mc-start-00-00)
                    # Only skip if no agenda link (agenda link offers chance to check for meeting time)
                    is_all_day_event = "mc-start-00-00" in article.get("class", [])
                    if is_all_day_event:
                        if not agenda_link:
                            log.info(
                                f"Skipping all-day meeting with no agenda link: {meeting_name}"
                            )
                            continue
                        else:
                            log.info(
                                f"All-day event detected but agenda link exists, checking PDF for time: {meeting_name}"
                            )

                    # Extract time (e.g., "10:00 am" or "12:00 am" for midnight)
                    hour = None
                    minute = None
                    time_match = re.search(
                        r"(\d{1,2}):(\d{2})\s*(am|pm)", button_text, re.IGNORECASE
                    )
                    if time_match:
                        hour, minute, am_pm = time_match.groups()
                        hour = int(hour)
                        minute = int(minute)

                        # Convert to 24-hour format
                        if am_pm.lower() == "pm" and hour != 12:
                            hour += 12
                        elif am_pm.lower() == "am" and hour == 12:
                            hour = 0
                    else:
                        # Try to extract from class name like "mc-start-10-00"
                        class_str = " ".join(article.get("class", []))
                        class_time_match = re.search(
                            r"mc-start-(\d{2})-(\d{2})", class_str
                        )
                        if class_time_match:
                            hour = int(class_time_match.group(1))
                            minute = int(class_time_match.group(2))

                    # If still no time found, OR if it's an all-day event with agenda link, check agenda PDF for time
                    # (All-day events with mc-start-00-00 will have hour=0, minute=0, but we want to override with PDF time)
                    if (hour is None or minute is None) or (
                        is_all_day_event and agenda_link
                    ):
                        if agenda_link:
                            if is_all_day_event:
                                log.info(
                                    f"All-day event with agenda link, extracting time from PDF: {meeting_name}"
                                )
                            else:
                                log.info(
                                    f"No time found in HTML for {meeting_name}, checking agenda PDF"
                                )
                            agenda_result = self._extract_time_from_agenda(agenda_link)
                            agenda_time, extracted_meeting_link = agenda_result
                            if agenda_time:
                                hour, minute = agenda_time
                                # If meeting link was found in PDF, use it
                                if extracted_meeting_link:
                                    meeting_link = extracted_meeting_link
                            else:
                                log.warning(
                                    f"Could not extract time from agenda PDF for: {meeting_name}"
                                )
                                continue
                        else:
                            log.warning(
                                f"No agenda link found and no time in HTML, skipping: {meeting_name}"
                            )
                            continue

                    # Create datetime object
                    try:
                        local_dt = datetime(
                            event_year, event_month, event_day, hour, minute
                        )
                        local_dt = tz_info.localize(local_dt)
                    except ValueError as e:
                        log.warning(f"Invalid date/time: {e}")
                        continue

                    # Note: Past meeting filtering is disabled to allow scraping of historical meetings
                    # Uncomment the following lines to skip meetings older than 1 day:
                    # if local_dt < now - relativedelta(days=1):
                    #     continue

                    # Convert to UTC
                    utc_dt = local_dt.astimezone(pytz.utc)
                    meeting_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                    # Check agenda first for meeting link (more accurate than keyword matching)
                    if agenda_link:
                        extracted_link = self._extract_meeting_link_from_agenda(
                            agenda_link
                        )
                        if extracted_link:
                            meeting_link = extracted_link

                    # Fallback to keyword-based assignment if no link found in agenda
                    if not meeting_link:
                        meeting_name_lower = meeting_name.lower()
                        if (
                            "senate" in meeting_name_lower
                            or "senate" in button_text.lower()
                        ):
                            meeting_link = (
                                "https://player.streamguys.com/iptv4/sldp/index.html"
                            )
                        elif (
                            "house" in meeting_name_lower
                            or "house" in button_text.lower()
                        ):
                            meeting_link = (
                                "https://player.streamguys.com/iptv3/sldp/index.html"
                            )

                    # Determine status
                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"

                    parsed_meeting = {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }

                    # Check for duplicates
                    if parsed_meeting not in self.meetings:
                        self.meetings.append(parsed_meeting)

        return self.meetings

    def _get_page_soup_using_scraperapi(self, url: str) -> BeautifulSoup:
        # Try with wait_for_selector first
        try:
            html_content = self.scraper.scrape_html(
                url=url, render="true", wait_for_selector="table.my-calendar-table"
            )
            if (
                html_content
                and isinstance(html_content, str)
                and len(html_content) > 100
            ):
                log.info(
                    f"Got HTML content via wait_for_selector: {len(html_content)} chars"
                )
                soup = self.scraper.convert_to_soup(string=html_content)
                return soup
            else:
                log.warning(
                    f"wait_for_selector returned: type={type(html_content)}, len={len(html_content) if isinstance(html_content, str) else 'N/A'}"
                )
        except Exception as e:
            log.warning(f"wait_for_selector approach failed: {e}")

        # Fallback to direct API call with render
        log.info(f"Trying direct API call for {url}")
        payload = {
            "api_key": self.scraper_api_key,
            "url": url,
            "render": "true",
            "country_code": "us",
        }
        page_with_needed_data = self.scraper.fetch_with_scraperapi(payload=payload)

        if not page_with_needed_data:
            log.warning(f"fetch_with_scraperapi returned None or empty for {url}")
        elif isinstance(page_with_needed_data, str):
            log.info(
                f"fetch_with_scraperapi returned string of length {len(page_with_needed_data)}"
            )
            if len(page_with_needed_data) < 100:
                log.warning(
                    f"Response seems short. First 500 chars: {page_with_needed_data[:500]}"
                )
        else:
            log.warning(
                f"fetch_with_scraperapi returned unexpected type: {type(page_with_needed_data)}"
            )

        soup = self.scraper.convert_to_soup(string=page_with_needed_data)
        return soup

    def _extract_time_from_agenda(
        self, agenda_url: str
    ) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
        """
        Extract meeting time and meeting link from agenda PDF.
        Returns ((hour, minute), meeting_link) tuple or (None, None) if not found.
        meeting_link will be None if no link is found in PDF or extracted from iframe.
        """
        meeting_link = None
        hour = None
        minute = None

        try:
            # Fetch the PDF
            response = requests.get(agenda_url, timeout=10)
            response.raise_for_status()

            pdf_content = BytesIO(response.content)
            text = extract_pdf_text_from_bytes(response.content)
            text = text.strip()

            lines = text.split("\n")

            # Look for time patterns in the PDF
            # Common patterns: "10:00 AM", "10:00 am", "10:00AM", "10:00am", "10:00 A.M.", "10:00 a.m.", etc.
            time_patterns = [
                r"\b(\d{1,2}):(\d{2})\s*([APap])\.?([Mm])\.?\b",  # "10:00 AM", "10:00 A.M.", "10:00am"
                r"\b(\d{1,2}):(\d{2})\s*([APap][Mm])\b",  # "10:00AM" (no space)
            ]

            for line in lines:
                line = line.strip()
                for time_pattern in time_patterns:
                    time_match = re.search(time_pattern, line, re.IGNORECASE)
                    if time_match:
                        hour = int(time_match.group(1))
                        minute = int(time_match.group(2))
                        # Handle both patterns - group 3 might be just A/P or AM/PM
                        if len(time_match.groups()) >= 4:
                            am_pm = (time_match.group(3) + time_match.group(4)).upper()
                        else:
                            am_pm = time_match.group(3).upper()

                        # Convert to 24-hour format
                        if am_pm.startswith("P") and hour != 12:
                            hour += 12
                        elif am_pm.startswith("A") and hour == 12:
                            hour = 0

                        log.info(
                            f"Extracted time from PDF: {hour:02d}:{minute:02d} from line: {line[:100]}"
                        )
                        break
                if hour is not None and minute is not None:
                    break

            # Search for meeting links in the PDF
            pdf_links = []
            if pdfplumber:
                try:
                    pdf_content.seek(0)  # Reset to beginning
                    with pdfplumber.open(pdf_content) as pdf:
                        for page in pdf.pages:
                            hyperlinks = page.hyperlinks
                            for link in hyperlinks:
                                uri = link.get("uri", "")
                                if uri:
                                    pdf_links.append(uri)
                except Exception as e:
                    log.debug(f"Error extracting links with pdfplumber: {e}")

            # Also search for URLs in the text using regex
            url_pattern = r"https?://[^\s\)]+"
            text_urls = re.findall(url_pattern, text)
            pdf_links.extend(text_urls)

            # Look for meeting-related links (watch, live, stream, streamguys, iptv, etc.)
            meeting_keywords = [
                "watch",
                "live",
                "stream",
                "streamguys",
                "iptv",
                "legislature.idaho.gov",
            ]
            # Limit the number of links we check to avoid excessive API calls
            checked_links = 0
            max_links_to_check = 5  # Limit to first 5 matching links
            for link in pdf_links:
                link_lower = link.lower()
                if any(keyword in link_lower for keyword in meeting_keywords):
                    # Found a potential meeting link, try to extract from iframe
                    meeting_link = self._extract_meeting_link_from_url(link)
                    if meeting_link:
                        break
                    checked_links += 1
                    if checked_links >= max_links_to_check:
                        log.debug(
                            f"Reached max links check limit ({max_links_to_check}) for agenda {agenda_url}"
                        )
                        break

        except Exception as e:
            log.warning(f"Error extracting time from agenda PDF {agenda_url}: {e}")

        if hour is not None and minute is not None:
            return ((hour, minute), meeting_link)
        return (None, meeting_link)

    def _extract_meeting_link_from_url(self, url: str, depth: int = 0) -> str:
        """
        Extract meeting link from a URL by scraping it and looking for iframes.
        Returns the meeting link if found, None otherwise.

        Args:
            url: URL to scrape
            depth: Current recursion depth (to prevent infinite loops)
        """
        # Check cache first to avoid redundant API calls
        if url in self._scraped_url_cache:
            log.debug(f"Using cached result for URL: {url}")
            return self._scraped_url_cache[url]

        # Prevent excessive nesting
        if depth > 2:
            log.debug(f"Max depth reached for URL: {url}")
            return None

        try:
            # Scrape the URL to find iframes
            payload = {
                "api_key": self.scraper_api_key,
                "url": url,
                "render": "true",
            }
            page_content = self.scraper.fetch_with_scraperapi(payload=payload)
            soup = self.scraper.convert_to_soup(string=page_content)

            if not soup:
                return None

            # Look for iframes that might contain the meeting link
            iframes = soup.find_all("iframe")
            for iframe in iframes:
                iframe_src = iframe.get("src", "")
                if iframe_src:
                    # Check if it's a streamguys link
                    if "streamguys.com" in iframe_src or "iptv" in iframe_src:
                        # Make sure it's a full URL
                        if iframe_src.startswith("//"):
                            iframe_src = "https:" + iframe_src
                        elif iframe_src.startswith("/"):
                            iframe_src = urljoin(self.base_url, iframe_src)
                        # Cache the result
                        self._scraped_url_cache[url] = iframe_src
                        return iframe_src

        except Exception as e:
            log.debug(f"Error extracting meeting link from URL {url}: {e}")

        # Cache None result to avoid retrying failed URLs
        self._scraped_url_cache[url] = None
        return None

    def _extract_meeting_link_from_agenda(self, agenda_url: str) -> str:
        """
        Extract meeting link from agenda PDF page.
        Some agenda PDFs link to a page that contains an iframe with the meeting link.
        Uses caching to avoid redundant API calls.
        """
        # Check cache first
        if agenda_url in self._scraped_url_cache:
            log.debug(f"Using cached result for agenda URL: {agenda_url}")
            return self._scraped_url_cache[agenda_url]

        try:
            # First, try to scrape the agenda URL (might be a PDF or HTML page)
            payload = {
                "api_key": self.scraper_api_key,
                "url": agenda_url,
                "render": "true",
                "country_code": "us",
            }
            page_content = self.scraper.fetch_with_scraperapi(payload=payload)
            soup = self.scraper.convert_to_soup(string=page_content)

            if not soup:
                return None

            # Look for iframes that might contain the meeting link
            iframes = soup.find_all("iframe")
            for iframe in iframes:
                iframe_src = iframe.get("src", "")
                if iframe_src:
                    # Check if it's a streamguys link
                    if "streamguys.com" in iframe_src or "iptv" in iframe_src:
                        # Make sure it's a full URL
                        if iframe_src.startswith("//"):
                            iframe_src = "https:" + iframe_src
                        elif iframe_src.startswith("/"):
                            iframe_src = urljoin(self.base_url, iframe_src)
                        # Cache the result
                        self._scraped_url_cache[agenda_url] = iframe_src
                        return iframe_src

            # Also check for links in the page that might lead to the meeting page
            # Limit nested scrapes to avoid excessive API calls
            links = soup.find_all("a", href=True)
            nested_scrape_count = 0
            for link in links:
                if nested_scrape_count >= self._max_nested_scrapes:
                    log.debug(
                        f"Reached max nested scrapes limit ({self._max_nested_scrapes}) for agenda {agenda_url}"
                    )
                    break

                href = link.get("href", "")
                link_text = link.get_text(strip=True).lower()
                # Look for links that might be meeting links
                if "watch" in link_text or "live" in link_text or "stream" in link_text:
                    full_url = urljoin(self.base_url, href)
                    # Use cached method to avoid redundant calls
                    nested_result = self._extract_meeting_link_from_url(
                        full_url, depth=1
                    )
                    if nested_result:
                        # Cache the result for the agenda URL
                        self._scraped_url_cache[agenda_url] = nested_result
                        return nested_result
                    nested_scrape_count += 1
        except Exception as e:
            log.warning(
                f"Error extracting meeting link from agenda {agenda_url}: {e}"
            )

        # Cache None result to avoid retrying failed URLs
        self._scraped_url_cache[agenda_url] = None
        return None


if __name__ == "__main__":
    run_test(
        url="https://legislature.idaho.gov/calendar/?yr=2025&month=2&dy=&cid=mc-0a237a65b4310d4acf431aa13a93646d",
        schedule_type="idaho_table",
        timezone="America/Denver",
        get_full_archive_flag=False,
    )
