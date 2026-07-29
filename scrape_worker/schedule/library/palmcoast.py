# palmcoast.py
import os
import re
import pytz
from datetime import datetime
from urllib.parse import urlparse

from utils.scrape_html import HtmlScraper, HTMLTags, HTMLAttributes
from schedule.schedule_scraper import run_test

DATE_FORMATS = [
    " %B %d, %Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%B %d, %Y %I:%M %p",
]
DATE_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"



class Palmcoast:
    def __init__(self):
        self.timezone = None
        self.base_url = None
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

    def palmcoast_table(self, url: str, local_timezone: str) -> list:
        self.timezone = local_timezone
        self.base_url = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Make year dynamic - replace any year in URL with current year
        current_year = datetime.now().year
        # Replace year pattern in URL (e.g., /2025, /2024, etc.)
        url = re.sub(r"/\d{4}$", f"/{current_year}", url)

        # Use simple render=true (works without premium features)
        page_html = self.scraper.scrape_html(url=url, render="true")
        detail_page_soup = self.scraper.convert_to_soup(string=page_html)

        cards = detail_page_soup.find_all(
            HTMLTags.DIV_TAG,
            class_="col-12 col-sm-6 col-xl-12 mb-4 position-relative meeting-card",
        )

        for card in cards:
            try:
                meeting_name_tag = card.find(HTMLTags.H1_TAG, {"class": "titlecard"})
                if not meeting_name_tag:
                    continue
                meeting_name = meeting_name_tag.text.strip()

                meeting_date_tag = card.find(HTMLTags.H2_TAG, {"class": "cardDate"})
                if not meeting_date_tag:
                    continue
                meeting_date = meeting_date_tag.text.strip()

                meeting_date_time = None
                for date_format in DATE_FORMATS:
                    try:
                        meeting_date_time = datetime.strptime(meeting_date, date_format)
                        break
                    except ValueError:
                        continue

                if not meeting_date_time:
                    continue

                meeting_date = self._convert_to_utc(meeting_date_time, self.timezone)

                link_row = card.find(HTMLTags.LINK_TAG)

                link = link_row[HTMLAttributes.LINK_ATTRIBUTE] if link_row else None

                # If link exists, scrape the detail page to get agenda link
                agenda_link = None
                if link:
                    detail_url = f"{self.base_url}{link}"
                    try:
                        detail_html = self.scraper.scrape_html(
                            url=detail_url, render="true"
                        )
                        detail_soup = self.scraper.convert_to_soup(string=detail_html)

                        # Find the agenda download div
                        agenda_div = detail_soup.find(
                            HTMLTags.DIV_TAG,
                            class_="col-12 col-sm-6 col-xl-12 mb-4 position-relative",
                            style=lambda value: value and "z-index: 15" in value,
                        )

                        if agenda_div:
                            agenda_link_tag = agenda_div.find(HTMLTags.LINK_TAG)
                            if agenda_link_tag:
                                agenda_link = agenda_link_tag.get(
                                    HTMLAttributes.LINK_ATTRIBUTE
                                )
                    except Exception:
                        pass

                card_text_row = card.find(HTMLTags.PARAGRAPH_TAG)

                card_text = card_text_row.text.strip() if card_text_row else None

                # Determine meeting status
                # Note: Status values must match what detect.py expects (uses .lower() for comparison)
                # Standard values: "In progress", "Cancelled", "Upcoming"
                if card_text and re.search(
                    r"Cancel(?:led|ed)", card_text, re.IGNORECASE
                ):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                # Note: "In progress" detection not implemented for palmcoast
                # Meetings are detected as "In progress" by detect.py via calendar_detect method

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date.strftime(DATE_UTC_FORMAT),
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
            except Exception as e:
                continue

        return self.meetings

    def _convert_to_utc(self, date_time: datetime, local_timezone: str) -> datetime:
        local_tz = pytz.timezone(local_timezone)
        local_dt = local_tz.localize(date_time)
        return local_dt.astimezone(pytz.UTC)


if __name__ == "__main__":
    run_test(
        url="https://www.palmcoast.gov/agendas/meetings/city-council/2025",
        schedule_type="palmcoast_table",
        timezone="America/New_York",
    )
