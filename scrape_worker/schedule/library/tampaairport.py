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


class Tampaairport:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_tampaairport(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Find agenda PDF link and extract the date it covers
        agenda_href = None
        agenda_date = None
        pdf_link = soup.find("a", href=re.compile(r"\.pdf"))
        if pdf_link:
            agenda_href = pdf_link.get("href")
            date_match = re.search(
                r"(\w+ \d+, \d{4})", pdf_link.get_text(strip=True)
            )
            if date_match:
                agenda_date = date_match.group(1)

        # Find the "Dates:" label, then grab the next <p> with the date list
        dates_label = soup.find("strong", string=re.compile(r"Dates", re.IGNORECASE))
        if not dates_label:
            return self.meetings
        dates_p = dates_label.find_parent("p")
        if not dates_p:
            return self.meetings
        date_list_p = dates_p.find_next_sibling("p")
        if not date_list_p:
            return self.meetings

        # Each date is separated by <br> tags; use stripped_strings
        for date_string in date_list_p.stripped_strings:
            # Format: "Thursday, February 5, 2026"
            match = re.search(r"\w+,\s+(\w+ \d+, \d{4})", date_string)
            if not match:
                continue

            extracted_date = match.group(1)
            try:
                meeting_dt = datetime.strptime(
                    f"{extracted_date} 9:00 AM", "%B %d, %Y %I:%M %p"
                )
            except ValueError:
                continue

            localized_time = tz.localize(meeting_dt)
            if localized_time.date() < now.date():
                continue

            utc_time = localized_time.astimezone(pytz.utc)
            meeting_date_time = utc_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            agenda_link = agenda_href if agenda_date == extracted_date else None

            self.meetings.append(
                {
                    "Meeting name": "Board meeting",
                    "Scheduled time": meeting_date_time,
                    "Meeting link": None,
                    "Agenda link": agenda_link,
                    "Status": "Upcoming",
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.tampaairport.com/business/hillsborough-county-aviation-authority-board-meetings",
        schedule_type="unique_tampaairport",
        timezone="America/New_York",
    )
