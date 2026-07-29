import os
import sys
import re
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Sfo:
    def __init__(self):
        self.meetings = []
        self.stream_type = "twilio_no_phone_code"
        self.details_url = "https://www.flysfo.com/about/about-sfo/airport-commission/addressing-the-commission"
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_sfo(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)
        now = datetime.now(timezone)
        year = now.year

        try:
            soup_new = self.scraper.fetch_with_bs(url=self.details_url)
            soup_new = self.scraper.convert_to_soup(string=soup_new)
            table = soup_new.find("table", class_="table").find("tbody")
            first_row = table.find("tr")
            main_row = first_row.find_next("tr")

            topic = main_row.find(
                "strong",
                string=lambda text: text and "listen on your phone" in text.lower(),
            )
            details_list = topic.find_next("ol")
            details = details_list.find("li").get_text()
            phone_number, access_string = details.split("|")
            phone_number = phone_number.split(":")[1].replace(".", "").strip()
            access_code, _ = access_string.split("#", 1)
            access_code = access_code.split(":")[1].replace(" ", "").strip()
        except AttributeError:
            phone_number = None
            access_code = None

        # Schedule is in the first tabpanel (current year)
        tabpanel = soup.find(attrs={"role": "tabpanel"})
        if not tabpanel:
            return self.meetings
        p_tag = tabpanel.find("p")
        if not p_tag:
            return self.meetings

        # Each date is separated by <br> tags; iterate stripped_strings
        # to collect date text and cancelled annotations
        raw_strings = list(p_tag.stripped_strings)
        dates = []
        i = 0
        while i < len(raw_strings):
            text = raw_strings[i]
            cancelled = False
            # Check if next string is a parenthetical like "(meeting cancelled)"
            if i + 1 < len(raw_strings) and raw_strings[i + 1].startswith("("):
                if "cancel" in raw_strings[i + 1].lower():
                    cancelled = True
                i += 2
            else:
                i += 1
            dates.append((text, cancelled))

        for date_text, cancelled in dates:
            if cancelled:
                continue

            meeting_date = date_text.strip() + " " + str(year)
            meeting_time = "9:00 am"
            meeting_name = "Commission meeting"

            meeting_date_time_web = f"{meeting_date} {meeting_time}"

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%B %d %Y %I:%M %p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            status = "Upcoming"
            meeting_link = None
            agenda_link = None

            if phone_number and access_code is not None:
                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Stream type": self.stream_type,
                    "Phone number": phone_number,
                    "Access code": access_code,
                    "Status": status,
                }
            else:
                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Stream type": self.stream_type,
                    "Status": status,
                }
            self.meetings.append(dictionary)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.flysfo.com/about/airport-commission/meeting-schedules",
        schedule_type="unique_sfo",
        timezone="America/Los_Angeles",
    )
