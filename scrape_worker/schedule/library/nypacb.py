# No longer used

import os
import re
import sys
import pytz
import logging
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test

# Setup the log
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Nypacb:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_nypacb(self, url, timezone="America/New_York"):
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        meeting_div = soup.find("main", id="dobcontent_inner")
        if not meeting_div:
            log.warning("Could not find main content div")
            return self.meetings

        mtg_notice = meeting_div.find("a", id="mtgNotice")
        if not mtg_notice:
            log.warning("Could not find meeting notice anchor")
            return self.meetings

        # Look for meeting info in p tags after the notice anchor
        meeting_info = ""
        p_tag = mtg_notice.find_next("p")
        while p_tag:
            text = p_tag.get_text()
            # Look for the expected pattern with time and date
            if re.search(r"\d{1,2}:\d{2} [ap]\.m\.", text):
                meeting_info = text
                break
            p_tag = p_tag.find_next("p")

        if not meeting_info:
            log.warning("No upcoming meeting announced on this page")
            return self.meetings

        # Define regex patterns for meeting name and date-time
        meeting_name_pattern = r"(?<=A )\w+ meeting"
        date_time_pattern = r"\d{1,2}:\d{2} [ap]\.m\. on \w+, \w+ \d{1,2}, \d{4}"

        # Find meeting name and date-time using regex
        meeting_name_match = re.search(meeting_name_pattern, meeting_info)
        meeting_date_time_match = re.search(date_time_pattern, meeting_info)

        if meeting_name_match and meeting_date_time_match:
            meeting_name = meeting_name_match.group(0).capitalize()

            meeting_date_time_web = meeting_date_time_match.group(0)
            time_str, date_str = meeting_date_time_web.split(" on ")
            meeting_date_time_web = date_str + " " + time_str
            meeting_date_time_web = meeting_date_time_web.replace(".", "")
            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%A, %B %d, %Y %I:%M %p"
            )

            # Convert each datetime object to the specified timezone
            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                return self.meetings

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            # Extract the agenda link
            agenda_link = None
            agenda_anchor = meeting_div.find("a", id="agenda")
            if agenda_anchor:
                agenda_p = agenda_anchor.find_next("p")
                if agenda_p:
                    agenda_a = agenda_p.find("a")
                    if agenda_a and agenda_a.get("href"):
                        agenda_link = domain + agenda_a["href"]

            # Extract the meeting link (live webcast)
            meeting_link = None
            webcast_anchor = meeting_div.find("a", id="live-webcast")
            if webcast_anchor:
                webcast_ul = webcast_anchor.find_next("ul")
                if webcast_ul:
                    webcast_a = webcast_ul.find("a")
                    if webcast_a:
                        meeting_link = webcast_a.get("href")
            if meeting_link is not None:
                status = "In progress"
            self.meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )
            return self.meetings
        else:
            meetings = []
            log.warning("No meetings on this page for now, check back later...")
            return meetings  # Return an empty meetings list


if __name__ == "__main__":
    run_test(
        url="https://www.budget.ny.gov/boards/pacb/index.html",
        schedule_type="unique_nypacb",
        timezone="America/New_York",
    )
