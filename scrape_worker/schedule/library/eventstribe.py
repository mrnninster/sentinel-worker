import os
import sys
import re
import calendar
import pytz
import logging
from datetime import datetime, UTC
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter

log = logging.getLogger(__name__)


class Eventstribe:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    def eventstribe_1_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        meetings = []

        timezone = pytz.timezone(timezone)

        now = datetime.now(UTC)

        table = soup.find("div", class_="tribe-events-pro-summary")

        divs = table.find_all("div", class_="tribe-events-pro-summary__event-row")

        for div in divs:
            articles = div.find_all("article")
            for article in articles:
                time_div = article.find("time")
                time = time_div.get("title")
                # Split the date string into two parts using the '::' separator
                date_parts = time.split("::")

                # Trim any leading or trailing spaces from each part
                date_parts = [part.strip() for part in date_parts]

                # Convert each part to a datetime object
                meeting_date_time = datetime.strptime(
                    date_parts[0], "%Y-%m-%d %H:%M:%S"
                )
                meeting_end_time = datetime.strptime(date_parts[1], "%Y-%m-%d %H:%M:%S")

                # Convert each datetime object to the specified timezone
                meeting_date_time = timezone.localize(meeting_date_time)
                meeting_end_time = timezone.localize(meeting_end_time)

                formatted_date_time = meeting_date_time.astimezone(pytz.utc)
                formatted_end_time = meeting_end_time.astimezone(pytz.utc)
                # Format the datetime objects to the desired output string
                meeting_date_time = formatted_date_time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )
                meeting_end_time = formatted_end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                name_div = article.find("h3")
                meeting_name = name_div.get_text(strip=True)

                if re.search(r"Cancel(?:led|ed", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                href = name_div.find("a").get("href")
                soup_n = self.scraper.fetch_with_bs(url=href)
                soup_n = self.scraper.convert_to_soup(string=soup_n)
                div_n = soup_n.find("div", class_="entry-content-wrap")
                webex_link_tag = soup_n.find(
                    "a",
                    string=lambda text: text
                    and "Join from the meeting online via WebEx" in text,
                )
                webex_link = webex_link_tag.get("href") if webex_link_tag else None

                zoom_link_tag = div_n.find(
                    "a",
                    rel="nofollow",
                    string=lambda text: text
                    and "Click here to join the meeting" in text,
                )
                zoom_link = zoom_link_tag.get("href") if zoom_link_tag else None

                meeting_id_tag = div_n.find(
                    "p", string=lambda text: text and "Meeting ID:" in text
                )

                passcode_tag = div_n.find(
                    "p", string=lambda text: text and "Passcode:" in text
                )
                passcode = (
                    passcode_tag.get_text(strip=True).split(":")[1].strip()
                    if passcode_tag
                    else None
                )

                meeting_id = (
                    meeting_id_tag.get_text(strip=True).split(":")[1].strip()
                    if meeting_id_tag
                    else None
                )
                if meeting_id:
                    meeting_id = meeting_id.replace(" ", "")
                    meeting_id = meeting_id.replace("-", "")

                if passcode and meeting_id:
                    stream_type = "twilio_phone_code"
                else:
                    stream_type = "twilio_no_phone_code"
                # Find all elements containing the word "Agenda"
                a_tag = soup_n.find("a", class_="kb-button")
                if a_tag:
                    agenda_link = a_tag.get("href")
                else:
                    agenda_link = None
                if "Hillsborough County Planning Commission" in meeting_name:
                    agenda_link = (
                        "https://planhillsborough.org/planning-commission-agendas/"
                    )
                meeting_link = None
                if zoom_link is not None:
                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": zoom_link,
                            "Agenda link": agenda_link,
                            "Stream type": stream_type,
                            "Access ID": meeting_id,
                            "Passcode": passcode,
                            "Status": status,
                        }
                    )
                elif webex_link is not None:
                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": webex_link,
                            "Stream type": stream_type,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
                else:
                    continue
        return meetings

    def eventstribe_2_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        meetings = []

        timezone = pytz.timezone(timezone)

        now = datetime.now(UTC)

        table = soup.find("div", class_="tribe-events-calendar-list")

        divs = table.find_all("div", class_="tribe-events-calendar-list__event-row")
        for div in divs:
            articles = div.find_all("article")
            for article in articles:
                time_div = article.find("time")
                date = time_div.get("datetime")

                time_data = time_div.get_text(strip=True)
                time_data = time_data.split("@")[1].strip()
                start_time = time_data.split("-")[0].strip()
                end_time = time_data.split("-")[1].strip()
                meeting_date_time = f"{date} {start_time}"
                meeting_end_time = f"{date} {end_time}"

                # Convert the input string to a datetime object
                meeting_date_time = datetime.strptime(
                    meeting_date_time, "%Y-%m-%d %I:%M %p"
                )
                meeting_end_time = datetime.strptime(
                    meeting_end_time, "%Y-%m-%d %I:%M %p"
                )

                # Convert each datetime object to the specified timezone
                meeting_date_time = timezone.localize(meeting_date_time)
                meeting_end_time = timezone.localize(meeting_end_time)

                formatted_date_time = meeting_date_time.astimezone(pytz.utc)
                formatted_end_time = meeting_end_time.astimezone(pytz.utc)

                # Format the datetime objects to the desired output string
                meeting_date_time = formatted_date_time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )
                meeting_end_time = formatted_end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                name_div = article.find("h3")
                meeting_name = name_div.get_text(strip=True)

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                href = article.find("h3").find("a").get("href")

                soup_new = self.scraper.fetch_with_bs(url=href)
                soup_new = self.scraper.convert_to_soup(string=soup_new)
                target_p = soup_new.find(
                    "p", string=lambda text: text and "View the Agenda" in text
                )
                div = soup_new.find("div", class_="tribe-events-content")

                youtube_tag = div.find(
                    "a",
                    string=lambda text: text and "https://www.youtube.com" in text,
                )
                zoom_tag = div.find(
                    "p",
                    string=lambda text: text and "https://us02web.zoom.us/" in text,
                )
                meeting_id_tag = div.find(
                    "p", string=lambda text: text and "Meeting ID:" in text
                )
                passcode_tag = div.find(
                    "p", string=lambda text: text and "Passcode:" in text
                )

                youtube_link = youtube_tag.get_text(strip=True) if youtube_tag else None
                zoom_link = zoom_tag.get_text(strip=True) if zoom_tag else None
                meeting_id = (
                    meeting_id_tag.get_text(strip=True).split(":")[1].strip()
                    if meeting_id_tag
                    else None
                )
                if meeting_id:
                    meeting_id = meeting_id.replace(" ", "")
                passcode = (
                    passcode_tag.get_text(strip=True).split(":")[1].strip()
                    if passcode_tag
                    else None
                )
                phone_number = "17193594580"

                if youtube_link is not None:
                    stream_type = "ts_youtube"
                    meeting_link = youtube_link
                elif zoom_link is not None:
                    stream_type = "twillio_phone_code"
                    meeting_link = zoom_link
                elif passcode is None and phone_number and meeting_id:
                    stream_type = "twilio_no_phone_code"
                else:
                    stream_type = None
                    meeting_link = None

                if target_p is not None:
                    agenda_url = target_p.find("a").get("href")
                else:
                    agenda_url = None

                if agenda_url is not None:
                    agenda_soup = self.scraper.fetch_with_bs(url=agenda_url)
                    agenda_soup = self.scraper.convert_to_soup(string=agenda_soup)
                    table = agenda_soup.find("table", class_="table")

                    tbody = table.find("tbody")
                    rows = tbody.find_all("tr", class_="__dt_row")
                    for row in rows:
                        agenda_name = row.find("a", class_="package-title").get_text(
                            strip=True
                        )

                        # Define a single regular expression pattern for extracting month and year
                        pattern = re.compile(
                            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b\s+(\d{4})",
                            re.IGNORECASE,
                        )

                        # Find matches in the text
                        match = pattern.search(agenda_name)
                        extracted_date = None

                        if match:
                            words = match.group(0).split()

                            if len(words) == 2 and words[0] in calendar.month_name:
                                month_name = words[0]
                                year = words[1]

                            # Map month names to month numbers
                            month_number = str(
                                list(calendar.month_name).index(month_name.capitalize())
                            )

                            # Format the result as "YYYY-MM"
                            extracted_date = f"{year}-{month_number.zfill(2)}"

                        year_month = date[:7]
                        if extracted_date == year_month:
                            agenda_link = row.find("a", class_="download-on-click").get(
                                "data-downloadurl"
                            )
                        else:
                            agenda_link = None
                else:
                    agenda_link = None

                if stream_type == "twillio_phone_code":
                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Stream type": stream_type,
                            "Access ID": meeting_id,
                            "Phone number": phone_number,
                            "Passcode": passcode,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
                elif stream_type == "twilio_no_phone_code":
                    self.meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Stream type": stream_type,
                            "Access ID": meeting_id,
                            "Phone number": phone_number,
                            "Passcode": passcode,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
                elif stream_type == "ts_youtube":
                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Stream type": stream_type,
                            "Status": status,
                        }
                    )
                else:
                    meetings.append(
                        {
                            "Meeting name": meeting_name,
                            "Scheduled time": meeting_date_time,
                            "Meeting link": meeting_link,
                            "Agenda link": agenda_link,
                            "Status": status,
                        }
                    )
        return meetings

    def eventstribe_3_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        meetings = []
        tz = pytz.timezone(timezone)

        event_rows = soup.find_all(
            "li",
            class_="tribe-common-g-row tribe-events-calendar-list__event-row",
        )

        for row in event_rows:
            # Extract date and time
            date_tag = row.find(
                "time", class_="tribe-events-calendar-list__event-datetime"
            )
            if not date_tag:
                continue
            date = date_tag.get("datetime")

            time_tag = row.find("span", class_="tribe-event-date-start")
            if not time_tag:
                continue
            time_text = time_tag.get_text(strip=True)

            if "@" in time_text:
                time_text = time_text.split("@")[1].strip()
            else:
                continue

            meeting_date_time = f"{date} {time_text}"
            try:
                meeting_date_time = datetime.strptime(
                    meeting_date_time, "%Y-%m-%d %I:%M %p"
                )
            except ValueError as e:
                log.warning(f"Failed to parse date '{meeting_date_time}': {e}")
                continue  # Skip this meeting
            meeting_date_time = meeting_date_time.replace(tzinfo=tz).astimezone(
                pytz.utc
            )
            meeting_date_time = meeting_date_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # Extract meeting title (site uses h3 or h4 depending on version)
            title_tag = row.find(
                ["h3", "h4"],
                class_="tribe-events-calendar-list__event-title",
            )
            if not title_tag:
                continue
            meeting_name = title_tag.get_text(strip=True)

            # Extract description and zoom info
            description_tag = row.find(
                "div",
                class_="tribe-events-calendar-list__event-description tribe-common-b2 tribe-common-a11y-hidden",
            )
            description = (
                description_tag.get_text(" ", strip=True) if description_tag else ""
            )

            # Initialize Zoom details
            zoom_link = None
            meeting_id = None
            passcode = None

            # Match Zoom URL
            zoom_match = re.search(r"https?://[^\s]*zoom\.us[^\s]*", description)
            if zoom_match:
                zoom_link = zoom_match.group(0)

            # Match Meeting ID
            id_match = re.search(
                r"Meeting ID[:\s]*([\d ]+)", description, re.IGNORECASE
            )
            if id_match:
                meeting_id = id_match.group(1).replace(" ", "")

            # Match Passcode
            passcode_match = re.search(
                r"Passcode[:\s]*([A-Za-z0-9]+)", description, re.IGNORECASE
            )
            if passcode_match:
                passcode = passcode_match.group(1).strip()
            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"

            if zoom_link and passcode and meeting_id:
                stream_type = "twilio_phone_code"
            else:
                stream_type = None
            meeting_link = zoom_link
            if stream_type == "twilio_phone_code":
                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Status": status,
                        "Meeting link": meeting_link,
                        "Access ID": meeting_id,
                        "Passcode": passcode,
                        "Agenda link": None,
                        "Stream type": stream_type,
                    }
                )
            else:
                meetings.append(
                    {
                        "Meeting name": meeting_name,
                        "Scheduled time": meeting_date_time,
                        "Status": status,
                        "Meeting link": meeting_link,
                        "Agenda link": None,
                    }
                )
        return meetings


if __name__ == "__main__":
    run_test(
        url="https://tbrpc.org/events/",
        schedule_type="eventstribe_3_table",
        timezone="America/New_York",
    )
