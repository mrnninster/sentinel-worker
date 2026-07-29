# This scraper is totally broken and is turned off in live anyway.
import os
import sys
import re
from urllib.parse import urlparse
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Collier:
    """
    NOTE (2026-01-13): The Collier County TV website has been redesigned.
    The original schedule page structure (div.schedule-page) no longer exists.
    Site now shows a carousel of past recordings instead of upcoming schedule.
    This scraper needs investigation - either new URL or complete rewrite.
    """

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_collier(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        container = soup.find("div", class_="schedule-page")

        wrapper = container.find("div", class_="schedule-wrapper clearfix")

        schedule = wrapper.find("div", class_="shows-schedule is-desktop ember-view")
        rows = schedule.find_all("div", class_="ember-view")
        for row in rows:
            date_div = row.find("div", class_="event-date")
            name_div = row.find(
                "div", class_="schedule-title is-mobile u-truncate-text"
            ).find("a")

            meeting_time = date_div.get_text(strip=True).split("at")[1].strip()
            meeting_name, meeting_date = name_div.get_text(strip=True).split("-")

            meeting_date = meeting_date.strip()
            meeting_name = meeting_name.strip()

            meeting_date_time_web = meeting_date + " " + meeting_time
            try:
                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%B %d, %Y %I:%M %p"
                )

            except ValueError:
                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%b. %d, %Y %I:%M %p"
                )
            # Convert each datetime object to the specified timezone
            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            meeting_link_div = soup.find(
                "a", class_="btn btn-watch pull-right ember-view"
            )
            meeting_link = (
                domain + meeting_link_div.get("href") if meeting_link_div else None
            )
            agenda_link = None
            status_div = row.find("div", class_="plaque-content not-now").get_text(
                strip=True
            )
            if "now" in status_div.lower():
                status = "In progress"
            dictionary = {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
            }
            self.meetings.append(dictionary)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="http://tv.colliergov.net/CablecastPublicSite/schedule?channel=1",
        schedule_type="unique_collier",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
