# indiana.py
import os
import sys
import json
import re
import pytz
import logging
import requests
import asyncio
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.format_time import TimeFormatter

INDIANA_LEGISLATURE_API_URL = "https://iga.in.gov/api"
INDIANA_LEGISLATURE_FALLBACK_API_URL = (
    "https://tlhgp53g3c.execute-api.us-east-2.amazonaws.com/beta/api"
)
INDIANA_SANITY_PROJECT_ID = "75zw3lpd"
INDIANA_SANITY_DATASET = "production"
INDIANA_SANITY_QUERY_URL = (
    f"https://{INDIANA_SANITY_PROJECT_ID}.api.sanity.io/"
    f"v2021-10-21/data/query/{INDIANA_SANITY_DATASET}"
)
MEETING_DATE_PARSING_TO_UTC_TIMEZONE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Indiana:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.url = None
        self.meetings = []
        self.self_contained_parser = True

    async def _intercept_api_call_with_playwright(
        self, url: str
    ) -> Optional[Dict[str, Any]]:
        """Use Playwright to intercept the API call and capture the response."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    java_script_enabled=True,
                )
                page = await context.new_page()

                # Capture API responses
                api_response = None
                api_url_captured = None
                all_requests = []

                async def handle_response(response):
                    nonlocal api_response, api_url_captured
                    response_url = response.url
                    if "getUpcomingMeetings" in response_url:
                        api_url_captured = response_url
                        try:
                            if response.status == 200:
                                api_response = await response.json()
                                log.info(
                                    f"Successfully intercepted API call: {response_url} - Status: {response.status}"
                                )
                            else:
                                log.warning(
                                    f"API call intercepted but failed: {response_url} - Status: {response.status}"
                                )
                        except Exception as e:
                            log.warning(f"Failed to parse API response: {e}")

                def log_all_requests(request):
                    all_requests.append(request.url)
                    if "api" in request.url.lower() or "aws" in request.url.lower():
                        log.debug(f"Network request: {request.url}")

                # Set up handlers before navigation to capture all requests
                page.on(
                    "response",
                    lambda response: asyncio.create_task(handle_response(response)),
                )
                page.on("request", log_all_requests)

                # Navigate to the page and wait for API call
                log.info(f"Navigating to {url} with Playwright...")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Try clicking "Upcoming Meetings" link if it exists to trigger data load
                try:
                    upcoming_link = page.get_by_role("link", name="Upcoming Meetings")
                    if await upcoming_link.is_visible(timeout=5000):
                        log.info("Clicking 'Upcoming Meetings' link...")
                        await upcoming_link.click()
                        await asyncio.sleep(2)
                except Exception:
                    log.debug("'Upcoming Meetings' link not found or not clickable")

                # Wait for the API call to complete - check for network idle or specific selector
                try:
                    # Wait for network to be idle or for a specific element that indicates data loaded
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    # If networkidle times out, wait a bit more
                    await asyncio.sleep(5)

                # Give additional time for any delayed API calls
                await asyncio.sleep(3)

                # Log summary of captured requests
                if all_requests:
                    log.debug(
                        f"Total network requests captured: {len(all_requests)}"
                    )
                    api_requests = [
                        r
                        for r in all_requests
                        if "api" in r.lower() or "aws" in r.lower()
                    ]
                    if api_requests:
                        log.info(f"API-related requests found: {api_requests}")

                if api_response:
                    log.info("Successfully captured API response via Playwright")
                elif api_url_captured:
                    log.warning(
                        f"API call was intercepted but response was not 200: {api_url_captured}"
                    )
                else:
                    log.warning(
                        "API call was not intercepted - page may not be making the call"
                    )

                await browser.close()
                return api_response

        except ImportError:
            log.warning("Playwright not available, falling back to direct API call")
            return None
        except Exception as e:
            log.warning(f"Playwright interception failed: {e}")
            import traceback

            log.debug(traceback.format_exc())
            return None

    def _fetch_sanity_meetings(
        self, local_timezone: str
    ) -> Dict[str, Dict[str, str]]:
        """Fetch meeting status overrides from the Sanity CMS for today's date."""
        local_tz = pytz.timezone(local_timezone)
        today_str = datetime.now(local_tz).strftime("%m/%d/%Y")
        meeting_types = [
            "house_meeting_status",
            "senate_meeting_status",
            "interim_meeting_status",
        ]
        sanity_meetings: Dict[str, Dict[str, str]] = {}

        for meeting_type in meeting_types:
            query = f'*[_type == "{meeting_type}"][0]'
            try:
                response = requests.get(
                    INDIANA_SANITY_QUERY_URL,
                    params={"query": query},
                    timeout=10,
                )
                response.raise_for_status()
                result = response.json().get("result") or {}
                for meeting in result.get("meetings", []):
                    if meeting.get("date") != today_str:
                        continue
                    lpid = meeting.get("lpid")
                    if not lpid:
                        continue
                    sanity_meetings[lpid] = {
                        "status": (meeting.get("status") or "").lower(),
                        "url": meeting.get("url") or "",
                    }
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("Unable to fetch Sanity meeting status: %s", e)

        return sanity_meetings

    def indiana_table(self, url: str, local_timezone: str) -> list:

        try:
            self.timezone = local_timezone
            self.url = url
            self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc
            current_datetime = datetime.now(pytz.UTC)
            local_tz = pytz.timezone(self.timezone)
            current_local_date = datetime.now(local_tz).date()

            # Extract session year from URL (e.g., /session/2025/...)
            # Fallback to current year if not found in URL
            year_match = re.search(r"/session/(\d{4})/", url)
            if year_match:
                session_year = int(year_match.group(1))
            else:
                session_year = datetime.today().year
            log.info(
                f"Using session year: {session_year} (extracted from URL: {url})"
            )

            # Try Playwright interception first to get API response with proper browser context
            json_response = None
            try:
                log.info("Attempting to intercept API call with Playwright...")
                json_response = asyncio.run(
                    self._intercept_api_call_with_playwright(url)
                )
            except Exception as e:
                log.warning(f"Playwright interception failed: {e}")

            # Fallback to direct API call if Playwright didn't work
            if not json_response:
                # Create a session to maintain cookies
                session = requests.Session()

                # Add proper headers to avoid bot detection / HTML fallbacks
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": url,
                    "Origin": self.base_url,
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
                session.headers.update(headers)

                # First, fetch the main page to establish session and get any cookies
                try:
                    log.info(f"Fetching main page to establish session: {url}")
                    main_page_response = session.get(url, timeout=10)
                    main_page_response.raise_for_status()
                except requests.RequestException as e:
                    log.warning(
                        f"Could not fetch main page (continuing anyway): {e}"
                    )

                session_lpid = f"session_{session_year}"
                interim_session_lpid = f"session_{session_year}"
                try:
                    session_years_url = f"{INDIANA_LEGISLATURE_API_URL}/getSessionYears"
                    session_years_response = session.get(
                        session_years_url, timeout=10
                    )
                    if "application/json" in session_years_response.headers.get(
                        "Content-Type", ""
                    ):
                        years = session_years_response.json().get("years", [])
                        active_year = next(
                            (year for year in years if year.get("active")), None
                        )
                        if active_year:
                            session_lpid = active_year.get("lpid") or session_lpid
                            interim_year = active_year.get("interim_year")
                            if interim_year:
                                interim_session_lpid = f"session_{interim_year}"
                            log.info(
                                "Using active session from API: %s (interim %s)",
                                session_lpid,
                                interim_session_lpid,
                            )
                except requests.RequestException as e:
                    log.warning(
                        "Unable to fetch session years (using URL year): %s", e
                    )
                except (ValueError, KeyError) as e:
                    log.warning(
                        "Unable to parse session years (using URL year): %s", e
                    )

                params = {
                    "session_lpid": session_lpid,
                    "interim_session_lpid": interim_session_lpid,
                }
                api_candidates = [
                    f"{INDIANA_LEGISLATURE_API_URL}/getUpcomingMeetings",
                    f"{INDIANA_LEGISLATURE_FALLBACK_API_URL}/getUpcomingMeetings/",
                ]

                for api_url in api_candidates:
                    try:
                        log.info(f"Making API call to: {api_url}")
                        response = session.get(api_url, params=params, timeout=10)
                        if response.status_code == 403:
                            log.warning(
                                f"API call blocked (403) for {api_url} - trying next"
                            )
                            continue
                        response.raise_for_status()
                        if "application/json" in response.headers.get(
                            "Content-Type", ""
                        ):
                            json_response = response.json()
                            break
                        log.warning(
                            "Warning: Response does not contain valid JSON."
                        )
                    except requests.RequestException as e:
                        log.warning(
                            f"JSON API call request exception occurred: {e}"
                        )
                if not json_response:
                    json_response = {}

            meetings = json_response.get("meetings", []) if json_response else []
            sanity_meetings = self._fetch_sanity_meetings(self.timezone)

            for meeting in meetings:
                chamber = (meeting.get("chamber") or "").lower()
                meeting_date = meeting.get("date")
                meeting_time = meeting.get("start_time")
                meeting_status_json = meeting.get("status") or ""
                meeting_name = meeting.get("topic")
                meeting_time_end = meeting.get("end_time")
                custom_start = meeting.get("custom_start") or ""
                meeting_link_path = meeting.get("video")
                meeting_lpid = meeting.get("lpid")
                sanity_meeting = (
                    sanity_meetings.get(meeting_lpid) if meeting_lpid else None
                )
                sanity_status = (
                    sanity_meeting.get("status") if sanity_meeting else ""
                ).lower()
                if not meeting_link_path and sanity_meeting:
                    meeting_link_path = sanity_meeting.get("url") or ""

                meeting_time = meeting_time.strip() if meeting_time else ""
                if not meeting_time and custom_start:
                    if re.search(r"\b\d{1,2}:\d{2}\b", custom_start) or re.search(
                        r"\b\d{1,2}\s*(AM|PM)\b", custom_start, re.IGNORECASE
                    ):
                        meeting_time = custom_start
                if not meeting_time and sanity_status:
                    meeting_time = "09:00 AM"
                meeting_time_end = meeting_time_end.strip() if meeting_time_end else ""
                if not meeting_time_end and meeting_time:
                    meeting_time_end = meeting_time

                # skip because of broken json object or canceled meeting
                if not meeting_date or not meeting_time or not meeting_name:
                    continue

                meeting_str_date = f"{meeting_date} {meeting_time}"
                try:
                    meeting_time_parsed = parser.parse(
                        meeting_str_date, fuzzy=True, ignoretz=True
                    )
                except Exception as e:
                    log.warning(
                        f"Skipping meeting with unparseable time '{meeting_str_date}': {e}"
                    )
                    continue
                formatted_naive_datetime = meeting_time_parsed.strftime(
                    TimeFormatter.desired_format()
                )
                meeting_local_date = meeting_time_parsed.date()
                time_formatter = TimeFormatter(formatted_naive_datetime, self.timezone)
                utc_time = time_formatter.get_utc_time(as_datetime=True)
                event_datetime = utc_time.isoformat().replace("+00:00", "Z")

                meeting_end_str = f"{meeting_date} {meeting_time_end}"
                try:
                    meeting_end_parsed = parser.parse(
                        meeting_end_str, fuzzy=True, ignoretz=True
                    )
                except Exception as e:
                    log.warning(
                        f"Skipping meeting with unparseable end time '{meeting_end_str}': {e}"
                    )
                    continue
                formatted_naive_end_datetime = meeting_end_parsed.strftime(
                    TimeFormatter.desired_format()
                )
                time_formatter = TimeFormatter(
                    formatted_naive_end_datetime, self.timezone
                )
                end_utc_time = time_formatter.get_utc_time(as_datetime=True)

                notes = meeting.get("notes")
                notes_text = ""
                if isinstance(notes, list):
                    notes_text = " ".join(
                        note for note in notes if isinstance(note, str)
                    )
                elif isinstance(notes, str):
                    notes_text = notes

                # Set event status - check status fields first, then time-based logic
                meeting_status = "Upcoming"
                status_context = " ".join(
                    part
                    for part in [
                        meeting_status_json,
                        custom_start,
                        notes_text,
                    ]
                    if part
                ).lower()

                status_lower = (sanity_status or meeting_status_json).lower()
                if "adjourned" in status_context:
                    meeting_status = "Ended"
                elif sanity_status:
                    if sanity_status == "live":
                        meeting_status = "In progress"
                    elif sanity_status == "adjourned":
                        meeting_status = "Ended"
                    else:
                        meeting_status = "Upcoming"
                elif status_lower in ["live", "in progress", "streaming"]:
                    meeting_status = "In progress"
                elif utc_time < current_datetime < end_utc_time:
                    meeting_status = "In progress"
                elif status_lower == "inactive" and end_utc_time < current_datetime:
                    meeting_status = "Ended"
                elif (
                    meeting_status == "Upcoming"
                    and meeting_link_path
                    and meeting_local_date == current_local_date
                    and re.search(
                        r"/livestreams/(house|senate)(/|$)",
                        meeting_link_path,
                        re.IGNORECASE,
                    )
                ):
                    meeting_status = "In progress"

                # Ignore if the meeting is in the past unless explicitly adjourned
                if end_utc_time < current_datetime and meeting_status not in [
                    "Ended",
                    "In progress",
                ]:
                    continue

                if chamber == "house":
                    meeting_full_name = f"(H) {meeting_name}"
                elif chamber == "senate":
                    meeting_full_name = f"(S) {meeting_name}"
                else:
                    meeting_full_name = meeting_name
                if meeting_link_path:
                    # Handle both relative and absolute URLs
                    if meeting_link_path.startswith(
                        "http://"
                    ) or meeting_link_path.startswith("https://"):
                        meeting_link = meeting_link_path
                    else:
                        # Ensure path starts with / if it's relative
                        if not meeting_link_path.startswith("/"):
                            meeting_link_path = "/" + meeting_link_path
                        meeting_link = f"{self.base_url}{meeting_link_path}"
                else:
                    meeting_link = ""

                self.meetings.append(
                    {
                        "Meeting name": meeting_full_name,
                        "Scheduled time": event_datetime,
                        "Meeting link": meeting_link,
                        "Agenda link": None,
                        "Status": meeting_status,
                    }
                )

            return self.meetings

        except (
            requests.RequestException,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as e:
            # Known possible errors
            log.exception(f"Error fetching Indiana meetings (expected error): {e}")
            return []
        except Exception:
            # Unexpected errors
            log.exception("Unexpected error in indiana_table")
            raise

    async def _get_rendered_html_table(self, url: str) -> Optional[str]:
        """Use Playwright to render the page and return HTML content."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    java_script_enabled=True,
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                # Track failed requests
                failed_requests = []
                page.on("requestfailed", lambda req: failed_requests.append(req.url))

                log.info(f"Navigating to {url} with Playwright...")

                # Listen for console messages and errors
                console_messages = []
                page.on("console", lambda msg: console_messages.append(msg.text))
                page.on("pageerror", lambda err: log.warning(f"Page error: {err}"))

                # Navigate and wait for load
                try:
                    response = await page.goto(
                        url, wait_until="networkidle", timeout=30000
                    )
                    if response:
                        log.info(f"Page response status: {response.status}")
                        if response.status >= 400:
                            log.warning(
                                f"Page returned error status: {response.status}"
                            )
                except Exception as e:
                    log.warning(f"Navigation issue: {e}, trying domcontentloaded")
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for React app to load - check for content in root div
                try:
                    await page.wait_for_function(
                        "() => { const root = document.getElementById('root'); "
                        "return root && root.children.length > 0 && "
                        "root.innerText.length > 50; }",
                        timeout=20000,
                    )
                    log.info("React app content loaded")
                except Exception as e:
                    log.warning(f"React content may not have loaded: {e}")
                    await asyncio.sleep(5)

                # Additional wait for any async content - React apps need time
                await asyncio.sleep(15)

                # Check if React root has content now
                root_content = await page.evaluate(
                    "() => { const root = document.getElementById('root'); "
                    "return root ? root.innerText.length : 0; }"
                )
                log.info(f"React root content length: {root_content}")

                # Log any console messages
                if console_messages:
                    log.debug(
                        f"Console messages (first 5): {console_messages[:5]}"
                    )

                # Check for failed requests
                if failed_requests:
                    log.warning(
                        f"Some requests failed: {len(failed_requests)} total"
                    )
                    if len(failed_requests) <= 5:
                        log.debug(f"Failed requests: {failed_requests}")

                # Check page title and URL to verify we're on the right page
                page_title = await page.title()
                page_url = page.url
                log.info(f"Page title: {page_title}, URL: {page_url}")

                # Wait for specific content to load - look for meeting-related elements
                try:
                    await page.wait_for_selector("a[href]", timeout=15000)
                    log.info("Found links on page")
                except Exception as e:
                    log.warning(f"Timeout waiting for links: {e}")

                # Get the rendered HTML
                html_content = await page.content()

                # Debug: log HTML size and check for meeting indicators
                log.info(f"HTML content length: {len(html_content)}")
                if len(html_content) < 1000:
                    log.warning(
                        f"HTML seems too short ({len(html_content)} bytes), "
                        "page may not have loaded"
                    )

                if "(S)" in html_content or "(H)" in html_content:
                    log.info("Found (S) or (H) patterns in HTML")
                else:
                    log.warning(
                        "No (S) or (H) patterns found in HTML - "
                        "page may not have loaded meetings"
                    )

                await browser.close()
                return html_content

        except ImportError:
            log.warning(
                "Playwright not available. Please install: pip install playwright"
            )
            return None
        except Exception as e:
            log.warning(f"Playwright rendering failed: {e}")
            return None

    def _parse_meeting_time_table(
        self, time_str: str, date_str: str
    ) -> Optional[datetime]:
        """Parse meeting time string (e.g., '1:30 PM') with date."""
        try:
            datetime_str = f"{date_str} {time_str}"
            parsed = parser.parse(datetime_str, fuzzy=True, ignoretz=True)
            return parsed
        except Exception as e:
            log.warning(f"Failed to parse time '{time_str}': {e}")
            return None

    def _extract_meetings_from_html_table(self, html: str, url: str) -> list:
        """Extract meeting information from the rendered HTML."""
        soup = BeautifulSoup(html, "html.parser")
        meetings = []

        # Extract the current date from the page
        date_element = soup.find(
            string=re.compile(r"\w+day,\s+\w+\s+\d+,\s+\d{4}")
        )
        current_date = None
        if date_element:
            try:
                date_str = date_element.strip()
                current_date = parser.parse(date_str, fuzzy=True).date()
            except Exception:
                pass

        # Try alternative date patterns
        if not current_date:
            date_elem2 = soup.find(string=re.compile(r"\w+\s+\d+\s+\d{4}"))
            if date_elem2:
                try:
                    date_str = date_elem2.strip()
                    current_date = parser.parse(date_str, fuzzy=True).date()
                except Exception:
                    pass

        # If we can't find the date, use today's date
        if not current_date:
            current_date = datetime.now().date()
            log.warning("Could not extract date from page, using today's date")

        log.info(f"Using date: {current_date}")

        # Find meeting links - look for links containing (S) or (H) pattern
        all_links = soup.find_all("a", href=True)
        meeting_links = []
        for link in all_links:
            link_text = link.get_text(strip=True) if link.get_text() else ""
            if link_text and re.search(r"\([SH]\)", link_text):
                meeting_links.append(link)

        log.info(f"Found {len(meeting_links)} meeting links")

        for i, link in enumerate(meeting_links):
            try:
                meeting_name = link.get_text(strip=True)
                meeting_url = link.get("href", "")
                meeting_num = i + 1
                log.info(
                    f"Processing meeting {meeting_num}/{len(meeting_links)}: "
                    f"{meeting_name[:50]}"
                )

                # Extract chamber from meeting name (S) or (H)
                chamber_match = re.search(r"^\(([SH])\)", meeting_name)
                chamber = chamber_match.group(1) if chamber_match else "S"

                # Find the parent container
                parent = link.find_parent()
                if not parent:
                    continue

                # Try to find a container that holds all meeting info
                container = parent
                for _ in range(5):
                    if container:
                        text_elements = container.find_all(string=True)
                        text_content = " ".join(
                            [t.strip() for t in text_elements if t.strip()]
                        )
                        if (
                            container.name in ["li", "div", "article", "section", "tr"]
                            or container.get("class")
                            or (
                                re.search(
                                    r"\d{1,2}:\d{2}\s*(AM|PM)",
                                    text_content,
                                    re.IGNORECASE,
                                )
                                and re.search(r"\([SH]\)", text_content)
                            )
                        ):  # noqa: W503
                            break
                    container = container.find_parent() if container else None

                if not container:
                    container = parent

                # Look for time, status, and meeting link
                time_str = None
                status = "Upcoming"
                meeting_link = ""

                # Search for time pattern
                time_pattern = re.compile(r"\d{1,2}:\d{2}\s*(AM|PM)", re.IGNORECASE)
                all_text = container.get_text()
                time_match = time_pattern.search(all_text)
                if time_match:
                    time_str = time_match.group(0).strip()

                # Check for red video camera icon - indicates "In progress"
                # Look for SVG or icon elements that might represent the camera
                # Also check for "Watch Livestream" link
                watch_link = container.find(
                    "a", string=re.compile("Watch Livestream", re.IGNORECASE)
                )
                video_icon = container.find(
                    lambda tag: tag.name in ["svg", "i", "img"]
                    and (
                        "video" in str(tag.get("class", [])).lower()
                        or "camera" in str(tag.get("class", [])).lower()
                        or "live" in str(tag.get("class", [])).lower()
                        or tag.find(string=re.compile("video|camera", re.IGNORECASE))
                    )
                )
                if watch_link or video_icon:
                    status = "In progress"
                    if watch_link:
                        watch_href = watch_link.get("href", "")
                        if watch_href:
                            meeting_link = urljoin(self.base_url, watch_href)
                    # Also check for video icon's parent link
                    elif video_icon:
                        icon_parent_link = video_icon.find_parent("a")
                        if icon_parent_link:
                            icon_href = icon_parent_link.get("href", "")
                            if icon_href:
                                meeting_link = urljoin(self.base_url, icon_href)

                # Check for "ADJOURNED" status
                if re.search(r"ADJOURNED", all_text, re.IGNORECASE):
                    status = "Ended"
                    # Skip adjourned meetings
                    continue

                # If no time found, try searching in a wider area
                if not time_str:
                    search_parent = container
                    for _ in range(5):
                        if search_parent:
                            parent_text = search_parent.get_text()
                            time_match = time_pattern.search(parent_text)
                            if time_match:
                                time_str = time_match.group(0).strip()
                                log.debug(f"Found time in parent: {time_str}")
                                break
                            search_parent = search_parent.find_parent()

                # If still no time found, skip this meeting
                if not time_str:
                    log.warning(f"No time found for meeting: {meeting_name}")
                    continue

                # Parse the meeting time
                meeting_datetime = self._parse_meeting_time_table(
                    time_str, current_date.strftime("%Y-%m-%d")
                )
                if not meeting_datetime:
                    log.debug(
                        f"Failed to parse datetime for {meeting_name} "
                        f"with time {time_str}"
                    )
                    continue

                log.info(
                    f"Parsed datetime: {meeting_datetime} for {meeting_name}"
                )

                # Convert to UTC using the timezone
                time_formatter = TimeFormatter(
                    meeting_datetime.strftime(TimeFormatter.desired_format()),
                    self.timezone,
                )
                utc_time = time_formatter.get_utc_time(as_datetime=True)
                event_datetime = utc_time.isoformat().replace("+00:00", "Z")

                # Check if meeting is in the past
                current_datetime = datetime.now(pytz.UTC)
                meeting_date = utc_time.date()
                current_date = current_datetime.date()

                if (
                    utc_time < current_datetime
                    and status != "In progress"
                    and meeting_date < current_date
                ):
                    log.debug(
                        f"Skipping past meeting: {meeting_name} on {meeting_date}"
                    )
                    continue

                # If no meeting link was found from "Watch Livestream" or icon,
                # use the meeting URL
                if not meeting_link and meeting_url:
                    meeting_link = urljoin(self.base_url, meeting_url)

                # Build full meeting name
                meeting_name_clean = meeting_name.replace(f"({chamber})", "").strip()
                meeting_full_name = f"[{chamber.upper()}] {meeting_name_clean}"

                meetings.append(
                    {
                        "Meeting name": meeting_full_name,
                        "Scheduled time": event_datetime,
                        "Meeting link": meeting_link,
                        "Agenda link": None,
                        "Status": status,
                    }
                )

            except Exception as e:
                meeting_name_str = (
                    meeting_name if "meeting_name" in locals() else "unknown"
                )
                log.warning(f"Error parsing meeting entry '{meeting_name_str}': {e}")
                import traceback

                log.debug(traceback.format_exc())
                continue

        return meetings

    def indiana_table_v2(self, url: str, local_timezone: str) -> list:
        """HTML scraping version using Playwright for rendering."""
        try:
            self.timezone = local_timezone
            self.url = url
            self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

            # Get rendered HTML using Playwright
            html_content = asyncio.run(self._get_rendered_html_table(url))

            if not html_content:
                log.warning("Failed to get rendered HTML content")
                return []

            # Extract meetings from HTML
            meetings = self._extract_meetings_from_html_table(html_content, url)

            log.info(f"Found {len(meetings)} meetings")
            return meetings

        except Exception as e:
            log.exception(f"Error in indiana_table_v2: {e}")
            return []


if __name__ == "__main__":
    run_test(
        url="https://iga.in.gov",
        schedule_type="indiana_table",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
