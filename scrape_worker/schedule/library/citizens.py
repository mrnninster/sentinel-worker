from urllib.parse import urlparse
import re
from datetime import datetime
import pytz
from utils.scrape_html import HtmlScraper


class Citizens:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.render = "true"
        self.scraper = HtmlScraper()

    def citizens_table(self, url, timezone="America/Los_Angeles"):

        response = self.scraper.scrape_html(
            url=url, render=self.render, wait_for_seconds=10
        )
        soup = self.scraper.convert_to_soup(string=response)

        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        div = soup.find("div", id="MainWindow")

        table = div.find("table", class_="LayoutTable")

        main_table = table.find("table", id="table1")
        if main_table:
            body = main_table.find("tbody")
            rows = body.find_all("div", class_="MeetingRow")
        else:
            rows = table.find_all("div", class_="MeetingRow")

        for row in rows:
            meeting_name = row.find("div", class_="MainScreenText RowDetails").get_text(
                strip=True
            )
            link_div = row.find("div", class_="RowRight")
            links = link_div.find_all("div")
            try:
                agenda_div = links[0]
                agenda_tag = agenda_div.find(
                    "a", string=lambda text: text and "Agenda" in text
                )
                a_link = agenda_tag.get("href") if agenda_tag else None
                if a_link is not None:
                    agenda_link = domain + "/" + a_link
                else:
                    agenda_link = None
                meeting_link_div = links[4]
                if meeting_link_div is not None:
                    try:
                        meeting_link = meeting_link_div.a["onclick"]
                        start_index = meeting_link.find('OpenWindow("') + len(
                            'OpenWindow("'
                        )
                        end_index = meeting_link.find('");')
                        meeting_link = meeting_link[start_index:end_index]
                        meeting_link = domain + "/" + meeting_link
                    except KeyError:
                        meeting_link = None
                else:
                    meeting_link = None
            except IndexError:
                agenda_link = None
                meeting_link = None

            meeting_date_div = row.find("div", class_="RowLink")
            meeting_date_time_web = meeting_date_div.get_text(strip=True)
            try:
                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%b %d, %Y %I:%M %p"
                )
            except ValueError:
                meeting_date_time_web = datetime.strptime(
                    meeting_date_time_web, "%b %d, %Y"
                )

            # Convert each datetime object to the specified timezone
            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"
            if meeting_date_time_local.date() < now.date():
                continue
            dictionary = {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": meeting_link,
                "Agenda link": agenda_link,
                "Status": status,
            }

            self.meetings.append(dictionary)
        return self.meetings
