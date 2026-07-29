import os
import re
import sys
import logging
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Arizonagaming:
    """
    This scraper handles the request for the Arizona gaming comission schedule.
    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_arizonagaming",
                "url": "https://gaming.az.gov/adg-event/calendar",
                "timezone": "America/Phoenix",
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
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def unique_arizonagaming(self, url, timezone):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        calendar_events = soup.find_all("div", class_="views-row")
        for calendar_event in calendar_events:

            # Initialize variables
            agenda_link = None
            access_code = None
            meeting_link = None
            phone_number = None
            status = "Upcoming"

            # Get the readmore link
            readmore_link = (
                calendar_event.find("div", class_="readmore-link").find("a").get("href")
            )

            # Get the event url
            event_url = f"{domain}{readmore_link}"
            event_soup_str = self.scraper.scrape_html(url=event_url)
            event_soup = self.scraper.convert_to_soup(event_soup_str)

            # Get the event name
            event_name = event_soup.find("h2", id="event-title").text.strip()
            event_name = event_name.split(" - ")[0]

            # Get the event date
            event_date = event_soup.find("div", class_="ics-start").text.strip()
            event_date = parser.parse(event_date, fuzzy=True)
            event_date_time = datetime.strftime(
                event_date, TimeFormatter.desired_format()
            )
            utc_time = TimeFormatter(event_date_time, timezone).get_utc_time(
                as_datetime=True
            )
            event_date_time_str = utc_time.isoformat().replace("+00:00", "Z")

            # 1) Get the raw text
            event_content = event_soup.find("div", class_="event-content").get_text(
                " ", strip=True
            )

            # normalize tricky Unicode (keeps your previous cleaning)
            text = re.sub(
                r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", event_content
            )
            text = text.replace("\u00a0", " ").replace("\u202f", " ")
            text = re.sub(r"[\u2010-\u2015]", "-", text)

            m = re.search(
                r"Or\s+dial:\s*"
                r"(?:\([A-Za-z]{2,}\)\s*)?"
                r"([+*\d][\d()\s\-\u2010-\u2015\u00A0\u202F]*?)"
                r"\s*PIN:\s*([0-9\s]+)",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )

            phone_number = access_code = ""
            if m:
                phone_number = re.sub(r"\D", "", m.group(1))  # digits only
                access_code = re.sub(r"\D", "", m.group(2))  # digits only

            # Fallback for long URL pin
            if not access_code:
                url_pin = re.search(r"[?&]pin=(\d+)", text, flags=re.IGNORECASE)
                if url_pin:
                    access_code = url_pin.group(1)

            # Set meeting
            meeting = {
                "Meeting name": event_name,
                "Scheduled time": event_date_time_str,
                "Meeting link": meeting_link,
                "Phone number": phone_number,
                "Access ID": access_code,
                "Agenda link": agenda_link,
                "Status": status,
            }

            self.meetings.append(meeting)
            # log.info(f"Meeting: {meeting}")
        return self.meetings


if __name__ == "__main__":
    url = "https://gaming.az.gov/adg-event/calendar"
    timezone = "America/Phoenix"
    schedule_type = "unique_arizonagaming"
    run_test(url=url, timezone=timezone, schedule_type=schedule_type)
