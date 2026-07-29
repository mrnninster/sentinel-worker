import os
import sys
import re
import pytz
from dateutil import tz
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Oklahoma_legislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_oklahoma_legislature(self, url, timezone):
        if "%%%" not in url:
            return self.meetings

        urls = [u for u in url.split("%%%") if u]

        for single_url in urls:
            soup = self.scraper.fetch_with_bs(url=single_url)
            soup = self.scraper.convert_to_soup(string=soup)
            meetings = self.sliqmedia_table(soup, single_url, timezone)
            self.meetings.extend(meetings)

        return self.meetings

    def sliqmedia_table(self, soup, url, timezone):
        meetings = []

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        divs = soup.find_all("div", class_="divEvent")

        for div in divs:
            a_tag = div.find("a")
            status_tag = div.find("div", class_="eventStatus")
            status_raw = status_tag.get_text(strip=True)

            name_div = div.find("div", class_="eventDesc")
            time_div = div.find("div", class_="eventTime")
            date_div = div.find("div", class_="eventDate")

            meeting_name = name_div.get_text().strip()
            # Remove newlines, spaces, and carriage returns, and replace multiple spaces with a single space
            meeting_name = re.sub(
                r"\s+", " ", meeting_name.replace("\n", "").replace("\r", "")
            )
            if "00282" in url:
                meeting_name = f"[SENATE] {meeting_name}"
            elif "00283" in url:
                meeting_name = f"[HOUSE] {meeting_name}"
            else:
                meeting_name = meeting_name

            meeting_time = time_div.get_text(strip=True)
            meeting_date = date_div.get_text(strip=True)

            start_time = meeting_time.split("-")[0].strip()
            end_time = meeting_time.split("-")[1].strip()
            start_time = f"{meeting_date} {start_time}"
            end_time = f"{meeting_date} {end_time}"

            # Parse the original time string
            start_time = datetime.strptime(start_time, "%a, %b %d, %Y %I:%M %p")
            end_time = datetime.strptime(end_time, "%a, %b %d, %Y %I:%M %p")

            # Desired format
            desired_format = TimeFormatter.desired_format()

            # Convert the datetime object into the desired format
            converted_start_time = start_time.strftime(desired_format)

            # Get time in UTC
            time_formatter = TimeFormatter(converted_start_time, timezone)
            meeting_start_time = time_formatter.get_utc_time()

            link = a_tag.get("href")
            link = domain + link

            soup_new = self.scraper.fetch_with_bs(url=link)
            soup_new = self.scraper.convert_to_soup(string=soup_new)
            agenda_div = soup_new.find("div", id="handoutFile")
            agenda_tag = agenda_div.find("a") if agenda_div else None
            agenda_link = None
            if agenda_tag is not None:
                agenda_link = agenda_div.find("a").get_text(strip=True)

            # Check the time difference
            if "In Progress" in status_raw:
                status = "In progress"
            elif re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Canceled"
            else:
                status = "Upcoming"

            meeting_link = link
            meetings.append(
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_start_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
            )
        return meetings


if __name__ == "__main__":
    run_test(
        url="https://sg001-harmony.sliq.net/00282/Harmony/en/View/UpcomingEvents/%%%https://sg001-harmony.sliq.net/00283/Harmony/en/View/UpcomingEvents/",
        schedule_type="unique_oklahoma_legislature",
        timezone="America/Chicago",
    )
