import os
import re
import sys
import pytz
import logging
import requests
from dateutil import parser
from dotenv import load_dotenv
from urllib.parse import urlparse, quote
from datetime import datetime, time
from bs4 import BeautifulSoup
from typing import Optional, List, Dict

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

M3U8_EXTENSION = ".m3u8"
ASSEMBLY_MEDIA_PATH = "/events/media/"
ASSEMBLY_BASE_URL = "https://www.assembly.ca.gov"
# If parse success rate is below this, raise to surface systemic parsing issues
PARSE_FAILURE_RATE_THRESHOLD = 0.5

from utils.format_time import TimeFormatter


class MeetingStatus:
    """Constants for meeting status values."""
    UPCOMING = "Upcoming"
    IN_PROGRESS = "In progress"
    CANCELLED = "Cancelled"


class Caliassembly:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def filter_future_meetings(self, meetings_list, timezone="America/Los_Angeles"):
        """
        Filter meetings to only include future meetings (today or later in local time).
        Skips meetings with midnight (0,0) scheduled time.
        """
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        future_meetings = []

        for meeting in meetings_list:
            meeting_date_time_str = meeting.get("Scheduled time")
            if not meeting_date_time_str:
                continue
            try:
                # Support both .000Z and variable-length fractional seconds
                if meeting_date_time_str.endswith("Z"):
                    meeting_date_time_str = meeting_date_time_str.replace("Z", "+00:00")
                meeting_dt = datetime.fromisoformat(meeting_date_time_str)
                if meeting_dt.tzinfo is None:
                    meeting_dt = meeting_dt.replace(tzinfo=pytz.UTC)
                meeting_dt = meeting_dt.astimezone(tz)
            except (ValueError, TypeError) as e:
                log.warning(f"Could not parse Scheduled time '{meeting_date_time_str}': {e}")
                continue
            if meeting_dt.time() == time(0, 0):
                continue
            if meeting_dt.date() >= now.date():
                future_meetings.append(meeting)

        return future_meetings

    def replace_ordinals(self, text):
        return re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)

    def process_time(self, text: str, timezone: str = "America/Los_Angeles") -> Optional[datetime]:
        """
        Parse text with date and/or time information and return a UTC datetime object.

        Param:
        -----
        text[str]: The Assembly has adjourned until Friday, January 16th, 2026 at 9:00 a.m

        Returns:
        -------
        datetime: UTC datetime object, or None if parsing fails
        """
        try:
            # Parse with fuzzy matching - returns naive datetime
            datetime_obj = parser.parse(text, fuzzy=True)
            meeting_date_time_str = datetime.strftime(datetime_obj, TimeFormatter.desired_format())
            utc_time = TimeFormatter(meeting_date_time_str, timezone).get_utc_time(as_datetime=True)
            return utc_time
        except Exception as e:
            log.warning(f"Failed to parse time from text '{text}': {e}")
            return None

    def extract_stream_url_from_media_page(self, page_url: str) -> Optional[str]:
        """
        Extract the m3u8 stream URL from a California Assembly media page.
        Tries data-media-source on the page first, then falls back to /api/mediahtml/{id}.

        Args:
            page_url: URL like https://www.assembly.ca.gov/events/media/18826

        Returns:
            First m3u8 URL from data-media-source, or None if not found.
        """
        try:
            match = re.search(rf"{re.escape(ASSEMBLY_MEDIA_PATH)}(\d+)", page_url)
            if not match:
                log.warning(f"Could not extract media ID from URL: {page_url}")
                return None

            media_id = match.group(1)
            parsed = urlparse(page_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            api_id = media_id

            try:
                response = requests.get(page_url, timeout=30.0, allow_redirects=True)
                response.raise_for_status()
                page_content = response.text
                soup = BeautifulSoup(page_content, "html.parser")
                media_element = soup.find("div", {"data-media-source": True})
                if media_element:
                    media_source = media_element.get("data-media-source", "")
                    if media_source:
                        urls = [u.strip() for u in media_source.split(",")]
                        for u in urls:
                            if M3U8_EXTENSION in u:
                                log.info(f"Found m3u8 URL from page data-media-source: {u}")
                                return u
                api_id_match = re.search(r"/api/mediahtml/(\d+)", page_content)
                if api_id_match:
                    api_id = api_id_match.group(1)
            except Exception as e:
                log.warning(f"Error fetching page: {e}, trying direct API call")
                api_id = media_id

            api_url = f"{base_url}/api/mediahtml/{api_id}"
            log.info(f"Fetching California Assembly API: {api_url}")
            response = requests.get(api_url, timeout=30.0, allow_redirects=True)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            media_element = soup.find("div", {"data-media-source": True})
            if not media_element:
                log.warning(f"No data-media-source in API response for {api_url}")
                return None
            media_source = media_element.get("data-media-source", "")
            if not media_source:
                return None
            urls = [u.strip() for u in media_source.split(",")]
            for u in urls:
                if M3U8_EXTENSION in u:
                    log.info(f"Found m3u8 URL from California Assembly API: {u}")
                    return u
            return None
        except Exception as e:
            log.warning(f"Failed to extract California Assembly stream URL: {e}")
            return None

    def _build_agenda_link(self, agenda_id: Optional[str]) -> Optional[str]:
        """Build agenda link with URL encoding for agenda ID."""
        if agenda_id and len(str(agenda_id)) < 1000:  # Sanity check to prevent abuse
            encoded_id = quote(str(agenda_id))
            return f"{ASSEMBLY_BASE_URL}/api/dailyfile/agenda/{encoded_id}?htmx=true"
        return None

    def _parse_floor_meetings(self, soup, tz, meetings: List[Dict]) -> None:
        """Parse floor meetings from the soup. Uses conditional flow (no early returns) for consistency with loop-based parsers."""
        parse_attempted = 0
        parse_succeeded = 0
        try:
            floor_meeting = soup.find("li", class_="events-page--item--committee-floorMeeting")
            if floor_meeting is None:
                return

            parse_attempted = 1
            title_div = floor_meeting.select_one("div.df-event-title-text.events-page--title")
            description_div = floor_meeting.select_one("div.df-event-description-text.events-page--desc")

            if title_div is None:
                log.warning(f"Could not find title div in floor_meeting: {floor_meeting}")
            elif description_div is None:
                log.warning(f"Could not find description div in floor_meeting: {floor_meeting}")
            else:
                title = title_div.get_text(strip=True).lower()
                description = description_div.get_text(strip=True).lower()
                utc_time = self.process_time(description)
                if utc_time is None:
                    log.warning(f"Could not parse time from floor meeting description: {description}")
                else:
                    meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
                    agenda_anchor = floor_meeting.find("a")
                    agenda_id = agenda_anchor.get("data-target-id") if agenda_anchor else None
                    agenda_link = self._build_agenda_link(agenda_id)
                    meeting = {
                        "Meeting name": title,
                        "Scheduled time": meet_date_time,
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": MeetingStatus.UPCOMING,
                    }
                    meetings.append(meeting)
                    parse_succeeded = 1
        except AttributeError as e:
            log.warning(f"Error parsing floor meeting: {e}")
        except Exception as e:
            log.warning(f"Unexpected error processing floor meeting: {e}")
        finally:
            if parse_attempted > 0 and parse_succeeded / parse_attempted < PARSE_FAILURE_RATE_THRESHOLD:
                raise RuntimeError(
                    f"California Assembly floor meeting parse failure rate too high: "
                    f"{parse_succeeded}/{parse_attempted} succeeded (threshold {PARSE_FAILURE_RATE_THRESHOLD})"
                )

    def _parse_todays_events(self, soup, tz, domain: str, meetings: List[Dict]) -> None:
        """Parse today's events including live status."""
        parse_attempted = 0
        parse_succeeded = 0
        try:
            todays_events = [
                li for li in soup.find_all("li", class_="df-event-committee-hearing-content")
                if "df-event-alt-status-content" not in li.get("class", [])
            ]
            if not todays_events:
                return

            parse_attempted = len(todays_events)
            # Use meeting timezone for today's date, not UTC
            now_local = datetime.now(tz=tz)
            todays_date = now_local.date().strftime("%Y-%m-%d")
            now_utc = datetime.now(tz=pytz.UTC)

            for event in todays_events:
                event_time = event.select_one("div.df-event-time.events-page--status.events-page--date.events-page--time")
                is_live = event.select_one("div.df-event-live.events-page--status.live-session--status")
                title_element = event.select_one("div.df-event-text.events-page--title")
                
                if title_element is None:
                    log.warning(f"Could not find title element in today's event")
                    continue

                title = title_element.get_text(strip=True).lower()
                agenda_element = event.select_one("a.button.events-page--btn.btn.view-committee-hearing-agenda")
                agenda_id = agenda_element.get("data-target-id") if agenda_element else None
                agenda_link = self._build_agenda_link(agenda_id)
                
                status = None
                meeting_link = None
                meet_date_time = None

                if event_time is not None:
                    status = MeetingStatus.UPCOMING
                    event_time_str = f"{todays_date} {event_time.get_text(strip=True).lower()}"
                    utc_time = self.process_time(event_time_str)
                    if utc_time is None:
                        log.warning(f"Could not parse time from event time string: {event_time_str}")
                        continue
                    meet_date_time = utc_time.isoformat().replace("+00:00", "Z")
                
                elif event_time is None and is_live is not None:
                    if is_live.get_text(strip=True).lower() == "live":
                        status = MeetingStatus.IN_PROGRESS
                        title_anchor = title_element.find("a")
                        if title_anchor:
                            relative_url = title_anchor.get("href")
                            meeting_link = f"{ASSEMBLY_BASE_URL}{relative_url}"
                        meet_date_time = now_utc.isoformat().replace("+00:00", "Z")
                    else:
                        continue
                else:
                    continue

                user_live_link = meeting_link
                final_meeting_link = meeting_link
                if (
                    meeting_link
                    and ASSEMBLY_BASE_URL in meeting_link
                    and ASSEMBLY_MEDIA_PATH in meeting_link
                ):
                    stream_url = self.extract_stream_url_from_media_page(meeting_link)
                    if stream_url:
                        final_meeting_link = stream_url
                        log.info(f"Extracted stream URL for today's event: {stream_url}")

                meeting = {
                    "Meeting name": title,
                    "Scheduled time": meet_date_time,
                    "Meeting link": final_meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                if user_live_link is not None:
                    meeting["user_live_link"] = user_live_link
                meetings.append(meeting)
                parse_succeeded += 1
        except AttributeError as e:
            log.warning(f"Error parsing today's events: {e}")
        except Exception as e:
            log.warning(f"Unexpected error processing today's events: {e}")
        finally:
            if parse_attempted > 0 and parse_succeeded / parse_attempted < PARSE_FAILURE_RATE_THRESHOLD:
                raise RuntimeError(
                    f"California Assembly today's events parse failure rate too high: "
                    f"{parse_succeeded}/{parse_attempted} succeeded (threshold {PARSE_FAILURE_RATE_THRESHOLD})"
                )

    def _parse_upcoming_events(self, soup, tz, domain: str, meetings: List[Dict]) -> None:
        """Parse upcoming events from ICS links."""
        parse_attempted = 0
        parse_succeeded = 0
        try:
            event_items = soup.find_all("li", class_="df-event-committee-hearing")
            if not event_items:
                return

            parse_attempted = len(event_items)
            for item in event_items:
                title_div = item.find("div", class_="events-page--title")
                if not title_div:
                    continue
                meeting_name = title_div.get_text(strip=True).lower()

                # Extract datetime from ICS link which has format: start=YYYYMMDDTHHmmss
                ics_link = item.find("a", href=lambda x: x and "/api/ics/generate" in x)
                meeting_date_time = None

                if ics_link:
                    href = ics_link.get("href", "")
                    start_match = re.search(r"start=(\d{8}T\d{6})", href)
                    if start_match:
                        start_str = start_match.group(1)
                        meeting_dt = datetime.strptime(start_str, "%Y%m%dT%H%M%S")
                        meeting_dt = tz.localize(meeting_dt)
                        meeting_dt_utc = meeting_dt.astimezone(pytz.utc)
                        meeting_date_time = meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                if not meeting_date_time:
                    # Fallback: try parsing from the date-time div
                    date_time_div = item.find("div", class_="events-page--date")
                    if date_time_div:
                        date_time_text = date_time_div.get_text(strip=True)
                        try:
                            meeting_dt = parser.parse(date_time_text, fuzzy=True)
                            if meeting_dt.year == 1900:
                                meeting_dt = meeting_dt.replace(year=datetime.now().year)
                            meeting_dt = tz.localize(meeting_dt)
                            meeting_dt_utc = meeting_dt.astimezone(pytz.utc)
                            meeting_date_time = meeting_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                        except Exception as e:
                            log.warning(f"Could not parse date: {date_time_text} - {e}")
                            continue

                if not meeting_date_time:
                    continue

                # Determine status
                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = MeetingStatus.CANCELLED
                else:
                    status = MeetingStatus.UPCOMING

                # Check for live/watch link
                watch_link = item.find("a", class_="link--watch")
                meeting_link = None
                if watch_link:
                    status = MeetingStatus.IN_PROGRESS
                    meeting_link = watch_link.get("href")
                    if meeting_link and not meeting_link.startswith("http"):
                        meeting_link = domain + meeting_link

                # Agenda link
                agenda_element = item.select_one("a.button.btn.view-committee-hearing-agenda.live-session--btn")
                agenda_id = agenda_element.get("data-target-id") if agenda_element else None
                agenda_link = self._build_agenda_link(agenda_id)

                user_live_link = meeting_link
                final_meeting_link = meeting_link
                if (
                    meeting_link
                    and ASSEMBLY_BASE_URL in meeting_link
                    and ASSEMBLY_MEDIA_PATH in meeting_link
                ):
                    stream_url = self.extract_stream_url_from_media_page(meeting_link)
                    if stream_url:
                        final_meeting_link = stream_url
                        log.info(f"Extracted stream URL for upcoming event: {stream_url}")

                meeting_record = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": final_meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                if user_live_link is not None:
                    meeting_record["user_live_link"] = user_live_link
                meetings.append(meeting_record)
                parse_succeeded += 1
        except AttributeError as e:
            log.warning(f"Error parsing upcoming events: {e}")
        except Exception as e:
            log.warning(f"Unexpected error processing upcoming events: {e}")
        finally:
            if parse_attempted > 0 and parse_succeeded / parse_attempted < PARSE_FAILURE_RATE_THRESHOLD:
                raise RuntimeError(
                    f"California Assembly upcoming events parse failure rate too high: "
                    f"{parse_succeeded}/{parse_attempted} succeeded (threshold {PARSE_FAILURE_RATE_THRESHOLD})"
                )

    def unique_caliassembly(self, url, timezone="America/Los_Angeles"):
        """
        Parse events from the California Assembly events page.

        Handles three types of events:
        - Floor meetings: li elements with class 'events-page--item--committee-floorMeeting'
        - Today's events: li elements with class 'df-event-committee-hearing-content' (including live status)
        - Upcoming events: li elements with class 'df-event-committee-hearing' with ICS calendar links

        Parameters:
        ----------
        url : str
            The URL being scraped
        timezone : str
            Timezone name (default: "America/Los_Angeles")

        Returns:
        -------
        List[Dict]
            List of meeting dictionaries with precise datetime information.
        """
        # Fetch HTML (rendered for JavaScript-populated event elements)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        # Reset meetings at start to avoid state mutation issues
        self.meetings = []

        tz = pytz.timezone(timezone)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        
        self._parse_floor_meetings(soup, tz, self.meetings)
        self._parse_todays_events(soup, tz, domain, self.meetings)
        self._parse_upcoming_events(soup, tz, domain, self.meetings)

        self.meetings = self.filter_future_meetings(self.meetings, timezone)
        return self.meetings


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    import os

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test

    run_test(
        url=f"{ASSEMBLY_BASE_URL}/events",
        schedule_type="unique_caliassembly",
        timezone="America/Los_Angeles",
        get_full_archive_flag=True,
    )
