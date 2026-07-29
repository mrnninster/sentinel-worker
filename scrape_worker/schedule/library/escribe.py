import re
import os
import sys
from datetime import datetime
from urllib.parse import urlparse
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Escribe:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def escribe_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", class_="upcoming-meetings")

        items = div.find_all("div", class_="upcoming-meeting-container")
        for item in items:
            meeting_name = (
                item.find("h3", class_="meeting-title-heading").get_text().strip()
            )

            meeting_date_time_web = (
                item.find("div", class_="meeting-date").get_text().strip()
            )
            meeting_date_time_web = meeting_date_time_web.replace("@", "")

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%A, %B %d, %Y %I:%M %p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            wide_video_div = item.select_one(
                "div.pull-right.wideVideo, div.pull-right.wideVideo.d-none"
            )
            # Check for in-progress status
            if item.select_one("div.pull-right.wideVideo") and not item.select_one(
                "div.pull-right.wideVideo.d-none"
            ):
                status = "In Progress"
            elif re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            meeting_link = None
            if wide_video_div:
                a_tag = wide_video_div.find("a", class_="link")
                if a_tag and a_tag.get("href"):
                    meeting_link = domain + "/" + a_tag["href"]
            if not meeting_link:
                try:
                    meeting_link = item.find("div", class_="narrowVideo d-none ").find(
                        "a"
                    )["href"]
                    meeting_link = domain + "/" + meeting_link
                except AttributeError:
                    meeting_link = None

            package_list = item.find("div", class_="meeting-content").find(
                "div", class_="package-list"
            )
            try:
                agenda_link_div = package_list.find(
                    "span", {"class": "packageName"}, string="Agenda"
                ).find_next(
                    "a",
                    {
                        "aria-label": lambda value: value
                        and "agenda (pdf)" in value.lower()
                    },
                )
                agenda_link = agenda_link_div["href"] if agenda_link_div else None
                if agenda_link is not None:
                    agenda_link = domain + "/" + agenda_link
            except AttributeError:
                agenda_link = None

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
        url="https://pub-detroitmi.escribemeetings.com/?FillWidth=1&CurrentTab=mergedlist&Year=2024",
        schedule_type="escribe_table",
        timezone="America/New_York",
    )
