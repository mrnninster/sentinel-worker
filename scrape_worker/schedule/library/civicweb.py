# civicweb.py
import re
import pytz
from datetime import datetime
from urllib.parse import urlparse
from dateutil import parser

if __name__ == "__main__":  # for local testing
    import sys
    import os
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test
    from pytz import timezone as pytz_timezone

from utils.scrape_html import HtmlScraper

_CIVICWEB_DATE_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)


def _parse_meeting_name_and_date(meeting_data: str) -> tuple[str, str]:
    date_match = _CIVICWEB_DATE_RE.search(meeting_data)
    if date_match:
        name = meeting_data[: date_match.start()].strip().rstrip("-").strip()
        date = meeting_data[date_match.start() :].strip()
    else:
        parts = meeting_data.split("-", 1)
        name = parts[0].strip()
        date = parts[1].strip() if len(parts) > 1 else ""
    return name, date


class Civicweb:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.render = "true"
        self.wait_for_selector = "div#ctl00_RightSidebar.portal-sidebar-right"

    def civicweb_table(self, url, timezone="America/New_York"):

        scraper = HtmlScraper()
        response = scraper.scrape_html(
            url=url,
            render=self.render,
            wait_for_selector=self.wait_for_selector,
        )
        soup = scraper.convert_to_soup(string=response)

        domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        timezone = pytz.timezone(timezone)

        now = datetime.now(timezone)

        class_names = ["todays-meeting-list", "upcoming-meeting-list"]

        for class_name in class_names:

            div_element = soup.find("div", class_=class_name)
            if div_element is None:
                continue
            listed = div_element.find_all("li")

            for list in listed:
                meeting_data = list.get_text(strip=True)
                meeting_name, meeting_date = _parse_meeting_name_and_date(meeting_data)

                # Check if the pattern is present in the string
                link_element = list.find("a")
                if link_element is not None:
                    href = link_element["href"]
                    url = domain + href
                    wait_for_selector = "iframe.meeting-document"
                    soup_new = scraper.scrape_html(
                        url=url,
                        render=self.render,
                        wait_for_selector=wait_for_selector,
                    )
                    soup_new = scraper.convert_to_soup(string=soup_new)
                    iframe = soup_new.find("iframe", class_="meeting-document")
                    document_link = iframe.get("src")
                    try:
                        document_link = domain + document_link
                    except TypeError:
                        print("TypeError: can't form document link")
                        print(f"document_link: {document_link}")
                        print(f"domain: {domain}")
                        continue

                    soup_doc = scraper.fetch_with_bs(url=document_link)
                    soup_doc = scraper.convert_to_soup(string=soup_doc)
                    meeting_id_paragraph = soup_doc.find(
                        "p",
                        string=lambda text: text and "meeting id" in text.lower(),
                    )
                    passcode_paragraph = soup_doc.find(
                        "p",
                        string=lambda text: text and "passcode" in text.lower(),
                    )
                    by_phone_paragraph = soup_doc.find(
                        "p",
                        string=lambda text: text and "by phone" in text.lower(),
                    )
                    link_tag = soup_doc.find(
                        "span",
                        string=lambda text: text
                        and "https://us06web.zoom.us" in text.lower(),
                    )

                    meeting_link = link_tag.get_text(strip=True) if link_tag else None

                    try:
                        access_id = (
                            meeting_id_paragraph.get_text(strip=True)
                            .split(":")[1]
                            .strip()
                            if meeting_id_paragraph
                            else None
                        )
                    except IndexError:
                        # Handle IndexError by splitting the meeting id paragraph text by spaces
                        meeting_id_text = meeting_id_paragraph.get_text(strip=True)
                        access_id = "".join(filter(str.isdigit, meeting_id_text))

                    if access_id:
                        access_id = access_id.replace(" ", "")
                    try:
                        passcode = (
                            passcode_paragraph.get_text(strip=True)
                            .split(":")[1]
                            .strip()
                            if passcode_paragraph
                            else None
                        )
                    except IndexError:
                        # Handle IndexError by splitting the meeting id paragraph text by spaces
                        passcode_text = passcode_paragraph.get_text(strip=True)
                        passcode = "".join(filter(str.isdigit, passcode_text))

                    try:
                        phone_number = (
                            by_phone_paragraph.get_text(strip=True)
                            .split(":")[1]
                            .strip()
                            if by_phone_paragraph
                            else None
                        )
                    except IndexError:
                        # Handle IndexError by splitting the meeting id paragraph text by spaces
                        phone_number_txt = by_phone_paragraph.get_text(strip=True)
                        phone_number = "".join(filter(str.isdigit, phone_number_txt))

                    if phone_number:
                        phone_number = phone_number.replace(" ", "")

                    elements = soup_new.find_all("div", class_="portal-content")

                    for element in elements:
                        time_div = element.find("div", id="meeting-time-container")
                        if time_div is not None:
                            meeting_time = time_div.find(
                                "span", id="meeting-time"
                            ).get_text(strip=True)
                        else:
                            meeting_time = element.find(
                                "span", id="meeting-time"
                            ).get_text(strip=True)

                        time_pattern = re.compile(r"^\d{2}:\d{2} [APMapm]{2}$")
                        if time_pattern.match(meeting_time):
                            meeting_time = meeting_time
                        else:
                            meeting_time = " "
                        agenda_link = element.find("a", id="document-cover-pdf").get(
                            "href"
                        )
                        agenda_link = domain + agenda_link

                else:
                    continue
                if phone_number is not None:
                    stream_type = "twilio_phone_code"
                elif passcode is None and phone_number and access_id:
                    stream_type = "twilio_no_phone_code"
                else:
                    stream_type = None

                fuzzy_date_string = meeting_date + " " + meeting_time
                meeting_date_time_web = parser.parse(
                    fuzzy_date_string, fuzzy=True, ignoretz=True
                )

                # Convert to the specified timezone
                meeting_date_time_local = timezone.localize(meeting_date_time_web)
                meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                # Convert to JSON-friendly UTC date/time string
                meeting_date_time = (
                    meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                )

                # Check if the time difference is exactly 60 minutes (1 hour)
                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                else:
                    status = "Upcoming"
                meeting_info = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }

                if stream_type == "twilio_phone_code":
                    meeting_info.update(
                        {
                            "Stream type": stream_type,
                            "Access ID": access_id,
                            "Passcode": passcode,
                            "Phone number": phone_number,
                        }
                    )
                elif stream_type == "twilio_no_phone_code":
                    meeting_info.update(
                        {
                            "Stream type": stream_type,
                            "Access ID": access_id,
                            "Phone number": phone_number,
                            "Passcode": passcode,
                        }
                    )

                self.meetings.append(meeting_info)
        return self.meetings


if __name__ == "__main__":

    # url = "https://codb.civicweb.net/Portal/Default.aspx"
    url = "https://washingtonfl.diligent.community/Portal/MeetingSchedule.aspx"
    timezone = "America/New_York"
    schedule_type = "civicweb_table"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)

    run_test(
        url=url,
        timezone=timezone,
        schedule_type=schedule_type,
        # get_date_start=datetime.now(tz) - timedelta(days=10),
        # get_date_end=datetime.now(tz) - timedelta(days=1),
    )
