import os
import re
import sys
import pytz
import logging
import requests
import pdfplumber
from datetime import datetime
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from utils.pdf_scanner import PDFScanner, RequestParams
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Cgcc:
    """
    Scraper for California Gambling Control Commission (CGCC) meeting schedule page.

    This scraper extracts meeting dates, times, and notes from the tables found on:
        https://www.cgcc.ca.gov/?pageID=2025meeting_schedule
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.pdf_scanner = PDFScanner()
        self.base_url = "https://www.cgcc.ca.gov"
        self.zoom_link_cache = {}  # Cache for zoom links to avoid re-downloading PDFs

    def extract_agenda_links(self, soup, url: str) -> dict:
        """
        Extract agenda links from the CGCC meetings page and match them to meeting dates.
        Fetches from: https://www.cgcc.ca.gov/?pageID=2025meetings&pageName=Meetings

        Args:
            soup: BeautifulSoup object (not used, but kept for compatibility)
            url: Base URL of the page

        Returns:
            dict: Dictionary mapping meeting dates (YYYY-MM-DD) to agenda links
        """
        agenda_map = {}

        try:
            # Fetch the meetings page with agenda links
            # Dynamically determine current year to avoid hardcoding
            current_year = datetime.now().year
            meetings_url = f"https://www.cgcc.ca.gov/?pageID={current_year}meetings&pageName=Meetings"
            try:
                response = self.scraper.scrape_html(url=meetings_url, render="true")
                meetings_soup = self.scraper.convert_to_soup(string=response)
            except (
                requests.RequestException,
                AttributeError,
                ValueError,
            ) as e:
                log.warning(f"Network error fetching meetings page: {e}")
                return agenda_map

            # Find the table with meeting information
            # The table has columns: TYPE, DATE, AGENDA, ADDITIONAL MEETING DOCUMENTS, MINUTES, AUDIO
            table = meetings_soup.find("table", id="myTable")

            if not table:
                log.warning("Could not find meetings table with id='myTable'")
                return agenda_map

            rows = table.find_all("tr", class_="blue_row")

            # Skip header row
            for row in rows:
                cells = row.find_all("td")

                if len(cells) < 3:
                    continue

                # Column 1: DATE (format: M/D/YYYY or M/DD/YYYY)
                date_cell = cells[1] if len(cells) > 1 else None
                if not date_cell:
                    continue

                date_text = date_cell.get_text(strip=True)

                # Handle dates that might have additional text like "See Notice of Continuance"
                # Extract just the date part (M/D/YYYY format)
                # For date ranges like "2/26/2025 - 2/27/2025", use the first date
                date_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_text)
                if not date_match:
                    continue

                try:
                    month = int(date_match.group(1))
                    day = int(date_match.group(2))
                    year = int(date_match.group(3))

                    # Validate date components
                    if not (1 <= month <= 12):
                        log.debug(f"Invalid month in date: {date_text}")
                        continue
                    if not (1 <= day <= 31):
                        log.debug(f"Invalid day in date: {date_text}")
                        continue
                    if not (2000 <= year <= 2100):  # Reasonable year range
                        log.debug(f"Invalid year in date: {date_text}")
                        continue

                    # Try to create date - this will raise ValueError for invalid dates like 2/30/2025
                    agenda_date = datetime(year, month, day).date()
                    date_key = agenda_date.strftime("%Y-%m-%d")
                except (ValueError, IndexError) as e:
                    log.debug(f"Could not parse or validate date: {date_text}, {e}")
                    continue

                # Column 2: AGENDA (contains link to PDF)
                agenda_cell = cells[2] if len(cells) > 2 else None
                if agenda_cell:
                    agenda_link_tag = agenda_cell.find("a", href=True)
                    if agenda_link_tag:
                        href = agenda_link_tag.get("href", "")

                        # Skip empty links
                        if href and href.strip():
                            # Handle relative URLs
                            if href.startswith("/"):
                                full_url = urljoin(self.base_url, href)
                            elif href.startswith("http"):
                                full_url = href
                            else:
                                full_url = urljoin(meetings_url, href)

                            agenda_map[date_key] = full_url
                            log.debug(
                                f"Found agenda link for {date_key}: {full_url}"
                            )

        except (AttributeError, ValueError, KeyError, TypeError) as e:
            log.warning(f"Error extracting agenda links: {e}")
        except Exception as e:
            log.warning(f"Unexpected error extracting agenda links: {e}")

        return agenda_map

    def extract_zoom_link_from_pdf(self, pdf_url: str) -> Optional[str]:
        """
        Extract zoom link from PDF agenda using pdf_scanner.
        Uses caching to avoid re-downloading the same PDF multiple times.

        Args:
            pdf_url: URL to the PDF file

        Returns:
            Optional[str]: Zoom meeting link if found, None otherwise
        """
        # Check cache first
        if pdf_url in self.zoom_link_cache:
            return self.zoom_link_cache[pdf_url]

        try:
            # Use PDFScanner to fetch PDF content with proper headers and cookies
            headers = self.pdf_scanner.generate_headers(use_fake_user_agent=False)
            cookies = self.pdf_scanner.generate_cookies(pdf_url, headers, verify=True)
            pdf_content_bytes = self.pdf_scanner.fetch_pdf_content_in_bytes(
                pdf_url, headers, cookies, verify=True
            )

            # Convert to BytesIO for pdfplumber
            pdf_content = BytesIO(pdf_content_bytes)

            # Extract hyperlinks using pdfplumber (more reliable for links)
            links = []
            with pdfplumber.open(pdf_content) as pdf:
                # Check multiple pages for zoom links (agendas can be multi-page)
                for page in pdf.pages:
                    page_links = page.hyperlinks
                    if page_links:
                        links.extend(page_links)

            # Look for zoom links in hyperlinks first (most reliable)
            zoom_link = None
            for link in links:
                uri = link.get("uri", "")
                if uri:
                    uri_lower = uri.lower()
                    if "zoom.us" in uri_lower or "zoom.com" in uri_lower:
                        zoom_link = uri
                        break

            # If not found in hyperlinks, extract text and search
            if not zoom_link:
                # Use PDFScanner to extract text from first few pages
                params = RequestParams(
                    link=pdf_url,
                    use_fake_user_agent=False,
                    extract_from_pages=3,  # Check first 3 pages for zoom links
                    verify=True,
                )
                text = self.pdf_scanner.scan_pdf_by_link(params)

                if text:
                    lines = text.split("\n")
                    zoom_patterns = [
                        r"https?://(?:[a-z0-9-]+\.)?zoom\.us/[^\s\)]+",
                        r"https?://(?:[a-z0-9-]+\.)?zoom\.com/[^\s\)]+",
                        r"zoom\.us/j/\d+",
                        r"zoom\.us/my/\w+",
                    ]

                    for line in lines:
                        for pattern in zoom_patterns:
                            match = re.search(pattern, line, re.IGNORECASE)
                            if match:
                                zoom_link = match.group(0)
                                if not zoom_link.startswith("http"):
                                    zoom_link = "https://" + zoom_link
                                break
                        if zoom_link:
                            break

            # Cache the result (even if None)
            self.zoom_link_cache[pdf_url] = zoom_link
            return zoom_link

        except (requests.RequestException, IOError, ValueError) as e:
            log.warning(f"Error extracting zoom link from PDF {pdf_url}: {e}")
            self.zoom_link_cache[pdf_url] = None
            return None
        except Exception as e:
            log.warning(
                f"Unexpected error extracting zoom link from PDF {pdf_url}: {e}"
            )
            self.zoom_link_cache[pdf_url] = None
            return None

    def unique_cgcc(self, url: str, timezone: str = "America/Los_Angeles") -> list:
        """
        Extract meeting data from CGCC meeting schedule page.

        Args:
            url (str): Target webpage URL.
            timezone (str): Timezone (e.g., 'America/Los_Angeles').

        Returns:
            list: A list of meeting dictionaries with keys:
                  'Meeting name', 'Scheduled time', 'Meeting link', 'Agenda link', 'Status'
        """
        self.meetings = []

        # Fetch and parse HTML with error handling
        try:
            response = self.scraper.scrape_html(url=url, render="true")
            soup = self.scraper.convert_to_soup(string=response)
        except (requests.RequestException, AttributeError, ValueError) as e:
            log.warning(f"Network error fetching schedule page: {e}")
            return self.meetings

        # Extract agenda links and map them to dates
        agenda_map = self.extract_agenda_links(soup, url)

        # Find all tables on the page
        tables = soup.find_all("table")

        for table in tables:
            # Check if this is a meeting schedule table
            # Look for table headers or preceding text that indicates meeting schedule
            table_headers = table.find_all("th")
            if not table_headers:
                continue

            # Check if this table contains "UPCOMING COMMISSION MEETINGS" in any header
            # Also check table caption or preceding text
            header_found = False
            for th in table_headers:
                header_text = th.get_text(strip=True)
                if "upcoming commission meetings" in header_text.lower():
                    header_found = True
                    break

            # Also check table caption
            if not header_found:
                caption = table.find("caption")
                if caption:
                    caption_text = caption.get_text(strip=True)
                    if "upcoming commission meetings" in caption_text.lower():
                        header_found = True

            if not header_found:
                continue

            # Get table body if it exists, otherwise get all rows
            # There may be multiple tbody elements (one for headers, one for data)
            tbodies = table.find_all("tbody")
            rows = []

            if tbodies:
                # Get rows from all tbody elements, but skip header rows (those with th tags)
                for tbody in tbodies:
                    for row in tbody.find_all("tr", class_="blue_row"):
                        # Skip header rows (rows that contain th tags instead of td tags)
                        if row.find("th"):
                            continue
                        # Only add rows that have td cells (data rows)
                        if row.find_all("td"):
                            rows.append(row)
            else:
                # If no tbody, get rows directly from table
                for row in table.find_all("tr", class_="blue_row"):
                    if row.find("th"):
                        continue
                    # Only add rows that have td cells (data rows)
                    if row.find_all("td"):
                        rows.append(row)

            for row in rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 3:  # Need at least DATE, TIME, LOCATION columns
                        continue

                    # Extract date, time, location, and notes
                    date_text = cells[0].get_text(strip=True) if len(cells) > 0 else ""
                    time_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    location_text = (
                        cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    )
                    notes_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""

                    if not date_text or not time_text:
                        continue

                    # Parse date from format like "Thursday, November 20th, 2025"
                    # Remove ordinal suffixes (st, nd, rd, th)
                    date_cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_text)

                    # Parse the date
                    try:
                        # Parse with format like "Thursday, November 20, 2025"
                        parsed_date = datetime.strptime(date_cleaned, "%A, %B %d, %Y")
                    except ValueError:
                        log.warning(f"Could not parse date: {date_text}")
                        continue

                    # Parse time from format like "10:00 AM" or "10:00:00 AM" or "10:00AM"
                    # Normalize time text by adding space before AM/PM if missing
                    time_normalized = re.sub(
                        r"(\d)([AP]M)",
                        r"\1 \2",
                        time_text,
                        flags=re.IGNORECASE,
                    )
                    try:
                        # Try parsing with seconds first
                        parsed_time = datetime.strptime(
                            time_normalized, "%I:%M:%S %p"
                        ).time()
                    except ValueError:
                        try:
                            # Try parsing without seconds
                            parsed_time = datetime.strptime(
                                time_normalized, "%I:%M %p"
                            ).time()
                        except ValueError:
                            log.warning(f"Could not parse time: {time_text}")
                            continue

                    # Combine date and time
                    meeting_datetime = datetime.combine(parsed_date.date(), parsed_time)

                    # Format for TimeFormatter: DD-MM-YYYY, HH:MM:SS AM/PM
                    formatted_datetime = meeting_datetime.strftime(
                        "%d-%m-%Y, %I:%M:%S %p"
                    )

                    # Convert to UTC
                    try:
                        meeting_utc = TimeFormatter(
                            formatted_datetime, timezone
                        ).get_utc_time(as_datetime=True)
                        utc_time = meeting_utc.isoformat().replace("+00:00", "Z")
                    except (ValueError, AttributeError) as e:
                        log.warning(f"Error converting time to UTC: {e}")
                        continue

                    # Check if meeting is in the past (before expensive PDF operations)
                    utc_now = datetime.now(pytz.utc)
                    if meeting_utc < utc_now:
                        continue

                    # Determine status
                    status = "Upcoming"
                    if "Cancelled" in notes_text or "Cancelled" in date_text:
                        status = "Cancelled"

                    # Create meeting name from date and notes
                    meeting_name = f"CGCC Commission Meeting"
                    if notes_text:
                        meeting_name += f" ({notes_text})"

                    # Get meeting date key for agenda matching
                    meeting_date_key = parsed_date.date().strftime("%Y-%m-%d")

                    # Find matching agenda link
                    agenda_link = agenda_map.get(meeting_date_key)
                    meeting_link = None

                    # If agenda link found, extract zoom link from PDF
                    if agenda_link:
                        meeting_link = self.extract_zoom_link_from_pdf(agenda_link)

                    meeting = {
                        "Meeting name": meeting_name,
                        "Scheduled time": utc_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }

                    self.meetings.append(meeting)

                except (ValueError, AttributeError, IndexError, KeyError) as e:
                    log.warning(f"Error parsing row: {e}")
                    continue
                except Exception as e:
                    log.warning(f"Unexpected error parsing row: {e}")
                    continue
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.cgcc.ca.gov/?pageID=2025meeting_schedule",
        schedule_type="unique_cgcc",
        timezone="America/Los_Angeles",
    )
