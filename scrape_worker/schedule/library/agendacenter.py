import re
import os
import sys
import pytz
import logging
import requests
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse
from utils.pdf_text import extract_pdf_text_from_bytes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.youtube import Youtube
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Agendacenter:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.channel_url = os.getenv("ARG_CHANNEL_URL")
        self.special_geos = ["lakealfred"]

    def get_agenda_text_and_link(self, item):
        # Initialize variables
        agenda_selector_link = None
        pdf_text = None

        try:
            # Get the pdf link
            downloads_btn = item.find("div", class_="popoutBtm")
            if downloads_btn:  # Check if downloads_btn exists
                pdf_btn = downloads_btn.find("a", class_="pdf")
                if pdf_btn and pdf_btn.text.strip().lower() == "pdf":
                    btn_url = pdf_btn.get("href")
                    if btn_url:
                        agenda_selector_link = f"{self.domain}{btn_url}"

            # If the pdf link is not found, try to get the link from the item
            if (
                any(geo in self.url for geo in self.special_geos)
                or agenda_selector_link is None
            ) and hasattr(self, "link_element"):
                link_url = self.link_element.get("href")
                if link_url:  # Check if link_url exists
                    agenda_selector_link = f"{self.domain}{link_url}"

                    # If the link is a html link, get the pdf link
                    if agenda_selector_link.endswith("html=true"):
                        agenda_selector_soup_str = self.scraper.scrape_html(
                            url=agenda_selector_link
                        )
                        agenda_selector_soup = self.scraper.convert_to_soup(
                            agenda_selector_soup_str
                        )
                        link_element = agenda_selector_soup.find("a", class_="file")
                        if link_element:  # Check if link_element exists
                            link_url = link_element.get("href")
                            if link_url:  # Check if link_url exists
                                agenda_selector_link = f"{self.domain}{link_url}"

        except Exception as e:
            # traceback.print_exc()
            log.warning(f"Error getting agenda link: {e}")

        # Get the pdf text
        if agenda_selector_link:
            try:
                # log.info(f"Agenda selector link: {agenda_selector_link}")
                response = requests.get(agenda_selector_link)

                # Check if the request was successful
                if response.status_code == 200:
                    # log.info(f"Response: {response.content[:500]}")
                    pdf_text = extract_pdf_text_from_bytes(response.content)
                else:
                    log.warning(f"Failed to fetch PDF: HTTP {response.status_code}")

            except Exception as e:
                # traceback.print_exc()
                log.warning(f"Error getting pdf text: {e}")

        return pdf_text, agenda_selector_link

    def get_relevant_text(self, pdf_text):
        # Get the relevant text
        relevant_text = str(pdf_text[:500]).replace("\n", " ").strip()

        # log.info(f"Relevant text: {relevant_text}")
        relevant_text = " ".join(relevant_text.split()).lower()

        # Multiple regex patterns to try for finding date and time
        regex_patterns = [
            # Pattern 1: Day of week, month day, year time (current pattern)
            r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*,?\s*"
            r"[a-z]+\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
            # Pattern 2: Month day, year time (like "august 19, 2025 1:30 p.m.")
            r"[a-z]+\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
            # Pattern 3: Month day, year with separate time (like "august 19, 2025" + "1:30 p.m.")
            r"([a-z]+\s+\d{1,2},?\s+\d{4}).*?(\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.))",
            # Pattern 4: Date format with time (like "8/19/2025 1:30 PM")
            r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
            # Pattern 5: Date format with time (like "8-19-2025 1:30 PM")
            r"\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
            # Pattern 6: ISO-like format (like "2025-08-19 1:30 PM")
            r"\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
            # Pattern 7: Just time if no date found (like "1:30 p.m.")
            r"\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)",
        ]

        # Try each pattern until we find a match
        for i, pattern in enumerate(regex_patterns):
            match = re.search(pattern, relevant_text, re.IGNORECASE)

            if match:
                if i == 2:  # Pattern 3: combine date and time groups
                    date_part = match.group(1)
                    time_part = match.group(2)
                    relevant_text = f"{date_part} {time_part}"
                else:
                    relevant_text = match.group(0)

                log.info(f"Found match using pattern {i+1}: {relevant_text}")
                break
        else:
            # If no pattern matches, log and return None
            log.warning(
                f"No date/time pattern found in text: {relevant_text[:100]}..."
            )
            return None

        return relevant_text

    def agendacenter_table(self, url, timezone="America/New_York"):
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Get website domain
        self.url = url
        parsed = urlparse(url)
        live_youtube_meetings = []
        self.domain = f"{parsed.scheme}://{parsed.netloc}"

        # Parse the channel url
        if self.channel_url:
            youtube = Youtube(url=self.channel_url, meeting_title="")
            if youtube.is_valid_youtube_streams_url():
                soup_str = self.scraper.scrape_html(url=self.channel_url)
                youtube_soup = self.scraper.convert_to_soup(soup_str)
                live_youtube_meetings = youtube.get_live_videos(youtube_soup)

        # Get the current datetime on the given timezone
        local_timezone = pytz.timezone(timezone)
        current_time_local = datetime.now(local_timezone)

        # Get the meeting items from the soup
        items = soup.find_all("tr", class_="catAgendaRow")
        for item in items:
            date = item.find("h3", class_="noMargin").text.strip()
            date = date.split("—")[0]
            date = date.strip()
            parsed_date_time = parser.parse(date)
            date = parsed_date_time.date()

            # Local date on the given timezone
            current_date = current_time_local.date()

            # If the date is greater or equal to the current date, get the meeting details
            if date >= current_date:
                # Initialize variables
                pdf_text = None
                relevant_text = None

                # Get the link element
                self.link_element = item.find_all("a")[1]

                # Get the title
                title = self.link_element.text.strip().split(" (")[0]

                # get pdf_text
                pdf_text, agenda_selector_link = self.get_agenda_text_and_link(item)

                # log.info(f"Agenda selector link: {agenda_selector_link}, pdf_text: {pdf_text}")

                # Get the relevant text
                if pdf_text is not None:
                    relevant_text = self.get_relevant_text(pdf_text)

                # Get the date and time
                if relevant_text is not None:
                    date_time = parser.parse(relevant_text, fuzzy=True)
                    time = date_time.time()
                    meeting_date_time = datetime.combine(date, time)

                else:
                    meeting_date_time = parsed_date_time.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )

                # Desired format
                desired_format = TimeFormatter.desired_format()

                # Convert the datetime object into the desired format
                converted_datetime = meeting_date_time.strftime(desired_format)

                # Get time in UTC
                time_formatter = TimeFormatter(converted_datetime, timezone)
                meeting_utc_time = time_formatter.get_utc_time()

                # Set the meeting status
                if re.search(r"Cancel(?:led|ed)", title, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"

                dictionary = {
                    "Meeting name": title,
                    "Scheduled time": meeting_utc_time,
                    "Meeting link": None,
                    "Agenda link": agenda_selector_link,
                    "Status": status,
                }
                self.meetings.append(dictionary)

        # Match meet by date and title
        if live_youtube_meetings:
            for youtube_meet in live_youtube_meetings[:]:
                for meeting in self.meetings:
                    meet_title = meeting["Meeting name"]
                    meet_date = parser.parse(meeting["Scheduled time"])
                    if (
                        meet_date.date() == current_date
                        and fuzz.token_set_ratio(
                            youtube_meet["video_title"], meet_title
                        )
                        > 85
                    ):
                        meeting["Status"] = "In Progress"
                        meeting["Meeting link"] = (
                            f"https://www.youtube.com/watch?v={youtube_meet['video_id']}"
                        )
                        live_youtube_meetings.remove(youtube_meet)
                        break

            # if there is only 1 live stream and 1 expected meet today
            in_progress_meetings = [
                meeting
                for meeting in self.meetings
                if meeting["Status"] == "In Progress"
            ]
            if not in_progress_meetings:
                today_meetings = [
                    meeting
                    for meeting in self.meetings
                    if parser.parse(meeting["Scheduled time"]).date() == current_date
                ]
                if len(today_meetings) == 1 and len(live_youtube_meetings) == 1:
                    video_id = live_youtube_meetings[0]["video_id"]
                    meeting_index = self.meetings.index(today_meetings[0])
                    self.meetings[meeting_index]["Status"] = "In Progress"
                    self.meetings[meeting_index][
                        "Meeting link"
                    ] = f"https://www.youtube.com/watch?v={video_id}"

        return self.meetings


if __name__ == "__main__":
    # os.environ["ARG_CHANNEL_URL"] = "https://www.youtube.com/@cityofhialeahgov/streams"
    run_test(
        url="https://www.hialeahfl.gov/AgendaCenter",
        schedule_type="agendacenter_table",
        timezone="America/New_York",
    )
