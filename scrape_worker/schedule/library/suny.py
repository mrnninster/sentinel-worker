import os
import re
import sys
import pytz
from urllib.parse import urlparse
from datetime import datetime, UTC
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Suny:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_suny(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)

        now = datetime.now(UTC)

        content_div = soup.find("div", class_="content")
        parsed_url = urlparse(url)
        domain = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path.rsplit('/', 1)[0]}/"
        # Initialize a list to store divs with inline styles
        divs_with_styles = []

        # Check if content_div exists
        if content_div:
            # Find all divs within the content_div
            divs = content_div.find_all("div")

            # Loop through each div
            for div in divs:
                # Check if the div has a style attribute
                if div.has_attr("style"):
                    # Append the div to the list if it has a style attribute
                    divs_with_styles.append(div)

        # Print the divs with inline styles
        for div in divs_with_styles:
            try:
                # Extract meeting date and time
                date_p = div.find("p", class_="large-5")
                if not date_p:
                    continue
                meeting_date_time_web = date_p.text.strip()

                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%m/%d/%Y %I:%M %p"
                )

                # Convert each datetime object to the specified timezone
                meeting_date_time_local = timezone.localize(meeting_date_time_web)

                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                # Format the datetime objects to the desired output string
                meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                # Extract meeting name
                name_p = div.find("p", class_="large-19")
                if not name_p:
                    continue
                meeting_name = name_p.text.strip()

                # Extract status and meeting link from the span inside the div
                status_img = div.select_one("div > span > table img")
                status_text = status_img["alt"] if status_img else None

                meeting_link = div.select_one("div > span > table a")
                meeting_link = meeting_link["href"] if meeting_link else None

                # Extract agenda link from the table outside the span
                agenda_a = div.select_one('div > table tr[style="font-size:.8em;"] a')
                agenda_link = agenda_a["href"] if agenda_a else None
                agenda_link = domain + agenda_link if agenda_link else None

                if status_text is not None:
                    if status_text.lower() == "click for live webcast":
                        status = "In progress"
                    elif re.search(r"Cancelled", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                else:
                    status = "Upcoming"

                if now.date() > meeting_date_time_local.date():
                    continue
                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
            except (ValueError, AttributeError, TypeError):
                continue

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.suny.edu/about/leadership/board-of-trustees/meetings/meetings/",
        schedule_type="unique_suny",
        timezone="America/New_York",
    )
