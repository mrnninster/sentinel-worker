import os
import sys
import re
from urllib.parse import urlparse
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Seattleport:
    def __init__(self):
        self.meetings = []
        self.stream_type = "twilio_no_phone_code"
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def unique_seattleport(self, url, timezone="America/Los_Angeles"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        timezone = pytz.timezone(timezone)
        now = datetime.now(timezone)
        year = now.year
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        table = soup.find("table", id="meetingList")
        if not table:
            return self.meetings
        tbody = table.find("tbody")
        rows = (tbody or table).find_all("tr")
        for row in rows:
            if "rowMY" in row.get("class", []):
                continue
            columns = row.find_all("td")
            if len(columns) < 6:
                continue
            meeting_name = columns[1].get_text().strip()

            meeting_data = columns[0].get_text().strip()
            meeting_data = meeting_data.replace("\xa0", " ")
            if "at" not in meeting_data:
                continue
            meeting_date, meeting_time = meeting_data.split("at")

            meeting_date = re.sub(r"(st|th|nd|rd)", r"", meeting_date)

            meeting_date_time_web = f"{meeting_date} {str(year)} {meeting_time}"

            meeting_date_time_web = meeting_date_time_web.replace("\xa0", " ")

            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%b %d  %Y  %I:%M %p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                continue

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            meeting_link = None

            agenda_tag = columns[5].find("a")
            agenda_href = agenda_tag.get("href") if agenda_tag else None
            if agenda_href is not None:
                agenda_page_url = domain + agenda_href
                soup_new = self.scraper.fetch_with_bs(url=agenda_page_url)
                soup_new = self.scraper.convert_to_soup(string=soup_new)
                docu_list = soup_new.find("ul", class_="documents-list")

                agenda_div = (
                    docu_list.find(
                        "li",
                        string=lambda text: text and "agenda" in text.lower(),
                    )
                    if docu_list
                    else None
                )

                agenda_link = (
                    agenda_div.find("span")["data-pdf"] if agenda_div else None
                )
            else:
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
        url="https://meetings.portseattle.org",
        schedule_type="unique_seattleport",
        timezone="America/Los_Angeles",
    )
