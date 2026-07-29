import os
import re
import sys
import pytz
import logging
import traceback
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from io import StringIO, BytesIO
from urllib.parse import urlparse
from utils.pdf_text import extract_pdf_text_from_bytes


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import ReturnType
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test


class Michigangaming:
    """
    This is a unique scraper for the Michigan Gaming Commission schedule.

    Here is what the scraper is expected to look like

    Request sample
    -------------
        - refresh_schedule :
            ```
                {
                    "geodicts": [
                        {
                            "geoID": "1700005967507x454349218326133300",
                            "schedule_type": "unique_michigangaming",
                            "url": "https://www.michigan.gov/mgcb/about/board-meeting-schedule-and-agendas",
                            "timezone": "America/New_York",
                            "glitch_meetings": [],
                            "debug": null,
                            "channel_url": ""
                        }
                    ],
                    "version": "test"
                }
            ```

        - stream_request:
            ```
                {
                    "schedule_url": "https://www.michigan.gov/mgcb/about/board-meeting-schedule-and-agendas",
                    "stream_type": "twilio_phone_no_code",
                    "meeting_title": "sample meeting title",
                    "location": "Michigan",
                    "session_ID": "1700005967507x454349218326133300",
                    "timezone": "America/New_York",
                    "schedule_type": "unique_michigangaming",
                    "demo_time_str": null,
                    "single_player_url": "",
                    "version": "test",
                    "glitch_meetings": [],
                    "meeting_id": "",
                    "passcode": "",
                    "dial_in_number": "",
                    "twilio_number": "+18882942357",
                    "is_restart": true,
                    "last_status": "Upcoming",
                    "channel_url": "",
                    "test_stream_url": null,
                    "has_recess": false,
                    "youtube_restart_ID": "",
                    "detect_start_method": "autostart",
                    "detect_end_method": "stream_detect",
                    "detect_end_ocr_string": ""
                }
            ```
    """

    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.max_iter_count = 20
        self.scraper = HtmlScraper()
        self.default_relevant_text_length = 400
        self.stream_type = "twilio_phone_no_code"

    def extract_text_from_pdf_pages(
        self, pdf_content: bytes, max_pages: int = 2
    ) -> str:
        """
        Extract text from the first N pages of a PDF for efficient processing.

        Args:
            pdf_content: bytes: The raw PDF content.
            max_pages: int: Maximum number of pages to extract (default: 2).

        Returns:
            str: Extracted text from the specified pages.
        """
        return extract_pdf_text_from_bytes(pdf_content, max_pages=max_pages)

    def extract_meeting_details(self) -> dict | None:
        """
        Extract date, time, meeting ID, and phone number using regex + dateutil.parser.parse.
        Works even if the text varies in structure or formatting (e.g. "October 21, 2025 10am").

        Returns:
            dict | None: A dictionary containing the date, time, meeting ID, and phone number.
            None if the meeting details cannot be extracted.

        Raises:
            ValueError: If the meeting details cannot be extracted from the PDF.
            Exception: If an error occurs while extracting the meeting details.
        """
        iter_count = 0
        initial_text_length = self.default_relevant_text_length

        while iter_count < self.max_iter_count:
            try:
                # Part of the PDF to consider for extracting meeting details
                text = self.pdf_text[: self.default_relevant_text_length]

                # Match a sequence that looks like "Month Day, Year, Time" or separated components
                datetime_pattern = re.compile(
                    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
                    r"[\s\-\.]+\d{1,2}(?:st|nd|rd|th)?(?:,)?[\s\-\.]*\d{4}"
                    r"(?:[^\n]*?(?:\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?))?",
                    re.IGNORECASE,
                )

                dt_match = datetime_pattern.search(text)
                parsed_date = parsed_time = None

                if dt_match:
                    try:
                        dt_parsed = parser.parse(dt_match.group(), fuzzy=True)
                        parsed_date = dt_parsed.date().isoformat()
                        parsed_time = dt_parsed.strftime("%H:%M")
                    except Exception as e:
                        log.warning(f"Failed to parse datetime: {e}")

                # Meeting/Webinar ID
                meeting_id_pattern = re.compile(
                    r"\b(?:Meeting|Webinar)\s+ID\s*:\s*([\d\s\-]+)\b|zoom\.us/(?:j|my)/(\d+)",
                    re.IGNORECASE,
                )
                id_match = meeting_id_pattern.search(text)
                meeting_id = None
                if id_match:
                    raw = id_match.group(1) or id_match.group(2)
                    meeting_id = re.sub(r"\D", "", raw)

                # Phone Number
                phone_pattern = re.compile(
                    r"(?:\+?1[\s\-\.]?)?(?:\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})"
                )
                phone_match = phone_pattern.search(text)
                phone = None

                if phone_match:
                    digits = re.sub(r"\D", "", phone_match.group())

                    if len(digits) == 10:
                        phone = f"+1{digits}"
                    elif len(digits) == 11 and digits.startswith("1"):
                        phone = f"+{digits}"
                    else:
                        phone = None  # Invalid phone number

                # Result dictionary
                data_dict = {
                    "date": parsed_date,
                    "time": parsed_time,
                    "meeting_id": meeting_id,
                    "phone": phone,
                }

                # If all values are extracted, return the result
                if None not in data_dict.values():
                    return data_dict

                # Otherwise, increase text length and try again
                iter_count += 1
                self.default_relevant_text_length = initial_text_length + (
                    iter_count * 100
                )

            except ValueError:
                traceback.print_exc()
                log.warning(f"Error extracting meeting details from PDF")
                return None
            except Exception as e:
                traceback.print_exc()
                log.warning(f"Error extracting meeting details: {e}")
                return None

        # If we've exhausted all iterations, return what we have
        return data_dict if "data_dict" in locals() else None

    def unique_michigangaming(
        self, url: str, timezone: str = "America/New_York"
    ) -> list:
        """
        Extracts the meeting details from the PDF and returns a list of meetings.

        Args:
            url: str: The URL of the page.
            timezone: str: The timezone of the page.

        Returns:
            list: A list of meetings.
            None if the meetings cannot be extracted.
        """
        # Fetch HTML
        html_content = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(html_content)

        # Reset instance variables to prevent state from persisting across multiple calls
        self.default_relevant_text_length = 400
        self.pdf_text = ""

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        schedule_utc_time = None

        # Get current year
        current_datetime = datetime.now(tz=pytz.UTC)
        current_year = current_datetime.year

        all_items = soup.find("ul", class_="items")

        if not all_items:
            log.warning("Could not find items list in HTML")
            return self.meetings

        item_by_year = all_items.find_all("li", class_="item")

        for item in item_by_year:

            # find the appropriate year
            heading_div = item.find("div", class_="field-heading")
            if not heading_div:
                continue
            heading_year = heading_div.get_text(strip=True)
            if int(heading_year) != current_year:
                continue

            # loop through items in the ul_item
            for li in item.find_all("li"):
                li_text = li.get_text(strip=True)

                if li_text:
                    li_text = li_text.lower().split("agenda")[0].strip()
                    datetime_obj = parser.parse(li_text, fuzzy=True)
                    schedule_date_time = datetime.strftime(
                        datetime_obj, TimeFormatter.desired_format()
                    )
                    schedule_utc_time = TimeFormatter(
                        schedule_date_time, timezone
                    ).get_utc_time(as_datetime=True)

                    if (
                        schedule_utc_time
                        and schedule_utc_time.date() >= current_datetime.date()
                    ):
                        link_element = li.find("a")
                        if not link_element or not link_element.get("href"):
                            continue
                        li_link = link_element.get("href")

                        if li_link.startswith("http"):
                            agenda_link = li_link
                        else:
                            agenda_link = f"{domain}{li_link}"
                        agenda_link = agenda_link.split("?rev")[0]

                        try:
                            # Get the pdf text
                            # Handle the request and fail gracefully
                            response = self.scraper.scrape_html(
                                url=agenda_link,
                                return_type=ReturnType.RESPONSE,
                            )

                            # Validate response
                            if not response or response.status_code != 200:
                                log.warning(
                                    f"Failed to fetch PDF from {agenda_link}: status code {response.status_code if response else 'None'}"
                                )
                                continue

                            # Validate Content-Type
                            content_type = response.headers.get(
                                "Content-Type", ""
                            ).lower()
                            if (
                                "application/pdf" not in content_type
                                and "application/octet-stream" not in content_type
                            ):
                                log.warning(
                                    f"Unexpected Content-Type for PDF at {agenda_link}: {content_type}"
                                )
                                # Continue anyway as some servers may not set Content-Type correctly

                            # Use page-based extraction for efficiency (only first 2 pages)
                            self.pdf_text = self.extract_text_from_pdf_pages(
                                response.content, max_pages=2
                            )

                            data_dict = self.extract_meeting_details()

                            # Meeting time
                            data_datetime = f"{data_dict['date']} {data_dict['time']}"
                            data_datetime_obj = parser.parse(data_datetime, fuzzy=True)
                            meeting_date_time = datetime.strftime(
                                data_datetime_obj,
                                TimeFormatter.desired_format(),
                            )
                            meeting_utc_time = TimeFormatter(
                                meeting_date_time, timezone
                            ).get_utc_time(as_datetime=True)
                            meeting_event_date = meeting_utc_time.isoformat().replace(
                                "+00:00", "Z"
                            )

                            # Meeting name with date
                            Date_format = "%B %d, %Y"
                            meet_date = datetime.strftime(
                                data_datetime_obj, Date_format
                            )
                            meeting_name = f"Michigan Gaming Control Board {meet_date}"

                            meeting = {
                                "Meeting link": "",
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_event_date,
                                "Agenda link": agenda_link,
                                "Stream type": self.stream_type,
                                "Phone number": data_dict["phone"] or "",
                                "Access ID": data_dict["meeting_id"],
                                "Status": "Upcoming",
                            }
                            self.meetings.append(meeting)

                        except Exception as e:
                            traceback.print_exc()
                            log.warning("Error getting pdf text: {}".format(e))
                else:
                    continue
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.michigan.gov/mgcb/about/board-meeting-schedule-and-agendas",
        schedule_type="unique_michigangaming",
        timezone="America/New_York",
    )
