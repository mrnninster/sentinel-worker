import os
import re
import sys
import pytz
import logging
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse, parse_qs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Nysenate:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    # Known video streaming domains for NY Senate
    VIDEO_DOMAINS = [
        "youtube.com/embed",
        "youtube.com/watch",
        "youtu.be",
        "ustream.tv",
        "ibm.com/video",  # IBM Video (formerly Ustream)
        "video.ibm.com",
    ]

    def _fetch_meeting_link(self, link):
        """
        Fetch the meeting page and extract the video stream URL.
        Uses ScraperAPI to avoid 403 errors from direct requests.
        Supports both Ustream iframes and YouTube embeds.

        Returns:
            str: Video stream URL if found, None otherwise
        """
        try:
            # Use ScraperAPI via the scraper to avoid 403 errors
            # Note: scrape_html() returns HTML text, same as fetch_with_bs()
            html = self.scraper.scrape_html(url=link)
            if not html or not isinstance(html, str):
                log.warning(f"Empty or invalid response from {link}")
                return None

            soup = self.scraper.convert_to_soup(string=html)

            # Try to find Ustream iframe first (legacy NY Senate pattern)
            frame_div = soup.find("div", class_="media-item__responsive-video")
            if frame_div:
                iframe = frame_div.find("iframe", id="UstreamIframe")
                if iframe and iframe.get("src"):
                    log.debug(f"Found Ustream iframe: {iframe.get('src')}")
                    return iframe.get("src")

            # Try to find video iframes in media-related containers
            video_containers = soup.find_all(
                "div",
                class_=lambda x: x
                and any(term in x for term in ["media", "player", "stream"]),
            )
            for container in video_containers:
                iframe = container.find("iframe")
                if iframe and iframe.get("src"):
                    src = iframe.get("src")
                    # Check if it's a known video streaming domain
                    if any(domain in src for domain in self.VIDEO_DOMAINS):
                        log.debug(f"Found video iframe in container: {src}")
                        return src

            # Also check for direct YouTube/Ustream embeds anywhere on the page
            all_iframes = soup.find_all("iframe", src=True)
            for iframe in all_iframes:
                src = iframe.get("src", "")
                if any(domain in src for domain in self.VIDEO_DOMAINS):
                    log.debug(f"Found video iframe on page: {src}")
                    return src

            log.info(f"No video iframe found on meeting page: {link}")
            return None

        except Exception as e:
            log.warning(f"Failed to fetch meeting link from {link}: {e}")
            return None

    def build_url_with_page(self, page_num, base_url):
        """Build URL with page parameter for pagination."""
        parsed = urlparse(base_url)
        query_params = parse_qs(parsed.query) if parsed.query else {}
        query_params["page"] = [str(page_num)]
        # Reconstruct query string
        query_string = "&".join([f"{k}={v[0]}" for k, v in query_params.items()])
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", query_string, "")
        )

    def unique_nysenate(self, url, timezone="America/New_York"):
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        current_datetime = datetime.now(tz=pytz.utc)
        is_last_page = False
        page_number = 1

        # Keep the full original URL (including existing query parameters)
        # so that filters like committee, date ranges, etc. are preserved
        # when constructing paginated URLs.
        base_url = url

        while not is_last_page:
            page_meetings = []

            # Fetch HTML for current page
            current_url = self.build_url_with_page(page_number, base_url) if page_number > 1 else url
            html = self.scraper.scrape_html(url=current_url)
            soup = self.scraper.convert_to_soup(string=html)

            # Find load more button for pagination
            load_more_button = soup.find("ul", class_="js-pager__items")
            if load_more_button:
                page_number += 1
            else:
                is_last_page = True

            # Add defensive null check for main content div
            div = soup.find("div", class_="view-content")
            if not div:
                log.warning(f"No view-content div found at {current_url}")
                is_last_page = True
                continue

            articles = div.find_all("article", class_="c-event-block")
            if not articles:
                log.warning(f"No event articles found at {current_url}")
                is_last_page = True
                continue

            for article in articles:
                # Add defensive null check for date element
                date_element = article.find("div", class_="c-event-date")
                if not date_element:
                    log.warning("Missing date element in article, skipping")
                    continue

                date = date_element.get_text(strip=True)

                # Add validation for date string parsing
                if len(date) < 2:
                    log.warning(f"Invalid date format: {date}")
                    continue

                day = date[:2]
                month = date[2:]

                # Calculate year with boundary handling
                year = current_datetime.year
                try:
                    month_num = datetime.strptime(month, "%b").month
                    current_month = current_datetime.month
                    # Handle year boundary: if we're in Dec and meeting is in Jan-Feb, it's next year
                    if current_month == 12 and month_num <= 2:
                        year += 1
                    # Handle year boundary: if we're in Jan and meeting is in Nov-Dec, it's last year
                    elif current_month == 1 and month_num >= 11:
                        year -= 1
                except ValueError:
                    log.warning(f"Unable to parse month: {month}")
                    continue

                items = article.find_all("div", class_="c-event--list-by-group")

                for item in items:
                    # meeting variables
                    meeting_link = None

                    # Construct datetime string with validation
                    time_div = item.find("div", class_="c-event-time")
                    if not time_div:
                        log.warning("Missing time div in item, skipping")
                        continue

                    time_element = time_div.find("time", class_="datetime")
                    if not time_element:
                        log.warning("Missing time element in time div, skipping")
                        continue

                    start_time = time_element.get_text(strip=True)

                    # Validate time string format before parsing
                    time_parts = start_time.split()
                    if len(time_parts) < 2:
                        log.warning(
                            f"Invalid time format (missing parts): {start_time}"
                        )
                        continue

                    time_str, day_zone = time_parts[0], time_parts[1]
                    time_components = time_str.split(":")
                    if len(time_components) < 2:
                        log.warning(
                            f"Invalid time format (missing colon): {time_str}"
                        )
                        continue

                    hours, minutes = time_components[0], time_components[1]
                    seconds = "00"
                    datetime_string = (
                        f"{day}-{month}-{year}, {hours}:{minutes}:{seconds} {day_zone}"
                    )
                    log.debug(f"Parsed datetime_string: {datetime_string}")

                    # Get UTC time
                    datetime_obj = parser.parse(datetime_string, fuzzy=True)
                    meeting_date_time = datetime.strftime(
                        datetime_obj, TimeFormatter.desired_format()
                    )
                    utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    utc_time_str = utc_time.isoformat().replace("+00:00", "Z")

                    # Get meeting name with null check (needed for cancelled check)
                    name_div = item.find("a")
                    if not name_div:
                        log.warning("Missing meeting name link, skipping")
                        continue

                    meeting_name = re.sub(r"\s+", " ", name_div.get_text().strip())

                    # Determine meeting status - check BEFORE time filtering
                    status = "Upcoming"  # Default status

                    # Check if meeting is cancelled first (higher priority)
                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        # Check if meeting is live - do this BEFORE time filtering
                        # so we don't skip meetings that have started but are still live
                        live_tag = item.find("a", class_="icon-before__youtube")
                        live_text = live_tag.get_text(strip=True) if live_tag else None
                        if live_text and live_text.lower() == "streaming live now":
                            status = "In progress"
                            href = name_div.get("href")
                            if href:
                                link = domain + href
                                meeting_link = self._fetch_meeting_link(link)

                    # For non-live meetings, skip if they're in the past
                    # For live meetings, keep them regardless of start time
                    if status != "In progress" and utc_time < current_datetime:
                        continue

                    agenda_link = None
                    page_meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": utc_time_str,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
            self.meetings.extend(page_meetings)
        return self.meetings


if __name__ == "__main__":
    url = "https://www.nysenate.gov/events/month?page=1&committee=finance"
    timezone = "America/New_York"
    schedule_type = "unique_nysenate"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
