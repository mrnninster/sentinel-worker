import os
import re
import sys
import pytz
from urllib.parse import urlparse
from dotenv import load_dotenv

from bs4 import BeautifulSoup
from dateutil import tz
from datetime import datetime, timedelta

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from schedule.schedule_scraper import run_test


class Novus:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def novus_1_table(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)
        timezone = pytz.timezone(timezone)

        # Extract the domain from the URL
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Define the search attributes
        search_attributes = [{"class": "rgMasterTable"}]

        # Get the current date in Eastern timezone
        now = datetime.now(timezone)

        for attr in search_attributes:
            table = soup.find("table", attr)
            if table is not None:
                rows = table.tbody.find_all("tr")

                for i, row in enumerate(rows):
                    columns = row.find_all("td")
                    # Ensure there are at least 8 elements in the columns list
                    if len(columns) >= 1:
                        agenda_link = None
                        meeting_time = ""
                        # Extract meeting name
                        meeting_name = columns[2].get_text(strip=True)
                        # Extract meeting date and time
                        meeting_date = columns[1].get_text(strip=True)
                        agenda_link_tag = columns[4].find("a", onclick=True)
                        if agenda_link_tag:
                            onclick_text = agenda_link_tag.get("onclick")
                            link_match = re.search(r"'([^']+)'", onclick_text)
                            if link_match:
                                agenda_link = (
                                    domain + "/agendapublic/" + link_match.group(1)
                                )
                            else:
                                agenda_link = None
                        if agenda_link is not None:
                            soup_new = self.scraper.fetch_with_bs(url=agenda_link)
                            soup_new = self.scraper.convert_to_soup(string=soup_new)
                            columns = soup_new.find_all("td", id="column2")
                            for column in columns:
                                text = column.get_text()
                                # Define a regular expression pattern to match the date and time
                                pattern = (
                                    r"(\b\w+ \d{1,2}, \d{4})(\d{1,2}:\d{2} [APMapm]{2})"
                                )

                                # Search for the pattern in the text
                                match = re.search(pattern, text)

                                if match:
                                    # Extract the matched date and time
                                    meeting_time = match.group(2)
                        meeting_link = None
                        try:
                            meeting_date_time_web = datetime.strptime(
                                meeting_date + " " + meeting_time,
                                "%m/%d/%y %I:%M %p",
                            )
                        except ValueError:
                            try:
                                meeting_date_time_web = datetime.strptime(
                                    meeting_date + " " + meeting_time,
                                    "%m/%d/%Y %H:%M",
                                )
                            except ValueError:
                                meeting_date_time_web = datetime.strptime(
                                    (meeting_date + " " + meeting_time).strip(),
                                    "%m/%d/%y",
                                )

                        # Convert to the specified timezone
                        meeting_date_time_local = timezone.localize(
                            meeting_date_time_web
                        )
                        meeting_date_time_utc = meeting_date_time_local.astimezone(
                            pytz.utc
                        )
                        # If the meeting date is not today or in the future, skip it

                        if meeting_date_time_local.date() < now.date():
                            continue

                        # Convert to JSON-friendly UTC date/time string

                        meeting_date_time = (
                            meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                            + "Z"
                        )

                        if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                            status = "Cancelled"
                        else:
                            status = "Upcoming"
                        self.meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        )
        return self.meetings

    def novus_2_table(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)
        timezone = pytz.timezone(timezone)

        # Extract the domain from the URL
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Define the search attributes
        search_attributes = [{"class": "rgRow"}, {"class": "rgAltRow"}]

        # Get the current date in Eastern timezone
        now = datetime.now(timezone)

        for attr in search_attributes:
            rows = soup.find_all("tr", attr)

            for i, row in enumerate(rows):
                columns = row.find_all("td")
                # Ensure there are at least 8 elements in the columns list
                if len(columns) >= 1:
                    div_id = row.get("id", "")
                    if "radGridItems" in div_id:
                        continue
                    # Extract meeting name
                    meeting_name = columns[1].get_text(strip=True)
                    # Extract meeting date and time
                    meeting_date = columns[0].get_text(strip=True)
                    link_tag = columns[3].find("a", onclick=True)
                    link = None
                    if link_tag:
                        onclick_text = link_tag.get("onclick")
                        link_match = re.search(r"'([^']+)'", onclick_text)
                        if link_match:
                            link = domain + "/agendapublic/" + link_match.group(1)
                        else:
                            link = None

                    meeting_time = ""
                    if link:
                        soup_string = self.scraper.fetch_with_bs(url=link)
                        soup_new = self.scraper.convert_to_soup(string=soup_string)

                        datetime_pattern = re.compile(
                            r"(\b\w+\s+\d{1,2}, \d{4})[,\s-]+(\d{1,2}:\d{2} [APMapm]{2})"
                        )
                        strong_datetime_pattern = re.compile(
                            r"(?:Date\s*:\s*)?(.+?\d{4})\s*(?:Time:\s*)?(\d{1,2}:\d{2}\s*[APMapM]{2})"
                        )
                        all_text = soup_string.strip()

                        soup_parser = BeautifulSoup(all_text, "html.parser")
                        all_text_without_tags = soup_parser.get_text(separator=" ")

                        match = datetime_pattern.search(all_text_without_tags)
                        match = (
                            strong_datetime_pattern.search(all_text_without_tags)
                            if match is None
                            else match
                        )
                        if match:
                            meeting_time = match.group(2)
                        if not meeting_time:
                            divs = soup_new.find_all("td", id="column3")
                            for div in divs:
                                text = div.get_text().strip()
                                # Define a regular expression pattern to match the date and time
                                pattern = r"(\b\w+ \d{1,2}, \d{4}), (\d{1,2}:\d{2} [APMapm]{2})"

                                # Search for the pattern in the text
                                match = re.search(pattern, text)

                                if match:
                                    # Extract the matched date and time
                                    meeting_time = match.group(2)
                                    break
                        if not meeting_time:
                            strongs = soup_new.find_all("strong")
                            for strong in strongs:
                                text = strong.get_text().strip()
                                # Define a regular expression pattern to match the date and time
                                pattern = r"(\b\w+ \d{1,2}, \d{4}), (\d{1,2}:\d{2} [APMapm]{2})"

                                # Search for the pattern in the text
                                match = re.search(pattern, text)

                                if match:
                                    # Extract the matched date and time
                                    meeting_time = match.group(2)
                                    break
                    meeting_link = None
                    try:
                        meeting_date_time_web = datetime.strptime(
                            meeting_date + " " + meeting_time,
                            "%m/%d/%y %I:%M %p",
                        )
                    except ValueError:
                        meeting_date_time_web = datetime.strptime(
                            meeting_date + " " + meeting_time, "%m/%d/%y "
                        )
                    # Convert to the specified timezone
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)
                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                    # If the meeting date is not today or in the future, skip it

                    if meeting_date_time_local.date() < now.date():
                        continue
                    agenda_link_tag = columns[4].find("a", href=True)
                    if agenda_link_tag:
                        href = agenda_link_tag.get("href")
                        agenda_link = domain + "/agendapublic/" + href
                    else:
                        agenda_link = None
                    # Convert to JSON-friendly UTC date/time string

                    meeting_date_time = (
                        meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                        + "Z"
                    )

                    if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                        status = "Cancelled"
                    else:
                        status = "Upcoming"
                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
        return self.meetings

    def novus_3_table(self, url, timezone="America/New_York"):
        # Fetch HTML with JS rendering (rendered list)
        html_content = self.scraper.scrape_html(url=url, render="true")
        soup = self.scraper.convert_to_soup(html_content)
        timezone = pytz.timezone(timezone)

        # Extract the domain from the URL
        domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        # Get the current date in Eastern timezone
        now = datetime.now(timezone)
        form = soup.find("form")
        div = form.find("div", id="meetings")
        table = div.find("table")
        rows = table.tbody.find_all("tr")

        for i, row in enumerate(rows):
            columns = row.find_all("td")
            # Ensure there are at least 8 elements in the columns list
            if len(columns) >= 2:
                # Extract meeting name
                meeting_time = ""
                meeting_name_div = columns[2].find("div", class_="mobile-table-td-div")
                meeting_name = meeting_name_div.get_text(strip=True)
                # Extract meeting date and time
                meeting_date = columns[1].get_text(strip=True)
                link_tag = columns[4].find("a", onclick=True)
                if link_tag:
                    onclick_text = link_tag.get("onclick")
                    link_match = re.search(r"'([^']+)'", onclick_text)
                    if link_match:
                        link = domain + "/agendapublic/" + link_match.group(1)
                    else:
                        link = None
                if link is not None:
                    soup_new = self.scraper.fetch_with_bs(url=link)
                    soup_new = self.scraper.convert_to_soup(string=soup_new)
                    divs = soup_new.find_all("strong")
                    for div in divs:
                        text = div.get_text()
                        # Define a regular expression pattern to match the date and time
                        pattern = r"(\b\d{1,2}:\d{2} [APMapm]{2}\b)"

                        # Search for the pattern in the text
                        match = re.search(pattern, text)

                        if match:
                            meeting_time = match.group()

                meeting_link = None
                try:
                    meeting_date_time_web = datetime.strptime(
                        meeting_date + " " + meeting_time, "%m/%d/%y %I:%M %p"
                    )
                except ValueError:
                    meeting_date_time_web = datetime.strptime(
                        meeting_date + " " + meeting_time, "%m/%d/%y "
                    )

                # Convert to the specified timezone
                meeting_date_time_local = timezone.localize(meeting_date_time_web)
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                # If the meeting date is not today or in the future, skip it

                if meeting_date_time_local.date() < now.date():
                    continue

                # Convert to JSON-friendly UTC date/time string

                meeting_date_time = (
                    meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                )

                agenda_link_tag = columns[5].find("a", href=True)
                if agenda_link_tag:
                    href = agenda_link_tag.get("href")
                    agenda_link = domain + "/agendapublic/" + href
                else:
                    agenda_link = None

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                self.meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Meeting link": meeting_link,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                )
        return self.meetings


if __name__ == "__main__":
    # Test with Orlando (novus_3_table)
    run_test(
        url="https://orlando.novusagenda.com/agendapublic/meetingsresponsive.aspx",
        schedule_type="novus_3_table",
        timezone="America/New_York",
    )
