import os
import sys
import re
from datetime import datetime, timedelta
import pytz
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Txpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_txpharm(self, url, timezone="America/Chicago"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Find "Upcoming Meetings" section
        anchor = soup.find(
            "h5", string=lambda t: t and "Upcoming Meetings" in t
        )
        if not anchor:
            return self.meetings

        # Parse date and time from <p><strong>Date:</strong> ... </p> etc.
        meeting_date = None
        meeting_time = None
        for p in anchor.find_next_siblings("p"):
            text = p.get_text(strip=True)
            if text.startswith("Date:"):
                meeting_date = text.split(":", 1)[1].strip()
            elif text.startswith("Time:"):
                raw_time = text.split(":", 1)[1].strip()
                # Clean: "9:00 a.m. – Conclusion" → "9:00 am"
                raw_time = raw_time.split("–")[0].split("\u2013")[0].strip()
                raw_time = raw_time.replace(".", "").strip()
                meeting_time = raw_time
            if meeting_date and meeting_time:
                break

        if not meeting_date or not meeting_time:
            return self.meetings

        try:
            dt_str = f"{meeting_date} {meeting_time}"
            meeting_dt = datetime.strptime(dt_str, "%B %d, %Y %I:%M %p")
        except ValueError:
            try:
                meeting_dt = datetime.strptime(dt_str, "%B %d, %Y %I %p")
            except ValueError:
                return self.meetings

        localized = tz.localize(meeting_dt)
        if localized.date() < now.date():
            return self.meetings

        utc_time = localized.astimezone(pytz.utc)
        meeting_date_time = utc_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Fetch agenda from the agendas page
        agenda_link = None
        try:
            agendas_url = f"{domain}/about/board-meetings/board-meeting-agendas.asp"
            agenda_html = self.scraper.fetch_with_bs(url=agendas_url)
            agenda_soup = self.scraper.convert_to_soup(string=agenda_html)
            # Find h5 matching the meeting date text
            date_headers = agenda_soup.find_all("h5")
            for h5 in date_headers:
                if meeting_date.strip() in h5.get_text(strip=True):
                    agenda_a = h5.find_next(
                        "a", string=lambda t: t and "Agenda" in t
                    )
                    if agenda_a:
                        href = agenda_a.get("href", "")
                        agenda_link = domain + href if not href.startswith("http") else href
                    break
        except Exception:
            agenda_link = None

        meeting_name = "Board Meetings"

        self.meetings.append(
            {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": None,
                "Agenda link": agenda_link,
                "Status": "Upcoming",
            }
        )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.pharmacy.texas.gov/about/board-meetings/index.asp",
        schedule_type="unique_txpharm",
        timezone="America/Chicago",
    )
