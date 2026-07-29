import os
import sys
import re
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

import requests
from io import BytesIO
import pdfplumber
from utils.scrape_html import HtmlScraper
from utils.pdf_text import extract_pdf_text_from_bytes
from utils.playwright_utils import BrowserManager
from schedule.schedule_scraper import run_test


class Orpharm:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.rule_url = (
            "https://www.oregon.gov/pharmacy/Pages/Rulemaking-Information.aspx"
        )
        self.scraper = HtmlScraper()

    def meeting_scraper(self, rule_url, timezone):
        now = datetime.now().replace(tzinfo=pytz.utc)
        rule_soup_str = self.scraper.fetch_with_bs(url=rule_url)
        rule_soup = self.scraper.convert_to_soup(string=rule_soup_str)

        div = rule_soup.find(
            "div", string=lambda text: text and "Rulemaking Hearing" in text
        )
        if not div:
            return None
        date_div = div.find_next("div")
        if not date_div:
            return None
        meeting_detail_div = date_div.find_next("p")
        if not meeting_detail_div:
            return None

        date_text = date_div.get_text()
        detail_text = meeting_detail_div.get_text(strip=True)

        # Regular expression to match the date and time
        name_date_pattern = r"\b\w+ \d{1,2}, \d{4}\b"
        date_pattern = r"\b(\d{1,2}/\d{1,2}/\d{4})\b"
        time_pattern = r"\b(\d{1,2}:\d{2}[APM]{2})\b"

        meeting_name = div.get_text(strip=True)
        meeting_name = re.sub(name_date_pattern, "", meeting_name).strip()

        # Find matches
        date_match = re.search(date_pattern, date_text)
        time_match = re.search(time_pattern, date_text)
        if date_match and time_match:
            # Extract matched groups if found
            meeting_date = date_match.group(1) if date_match else None
            meeting_time = time_match.group(1) if time_match else None

            meeting_date_time_web = meeting_date + " " + meeting_time
            meeting_date_time_web = datetime.strptime(
                meeting_date_time_web, "%m/%d/%Y %I:%M%p"
            )

            meeting_date_time_local = timezone.localize(meeting_date_time_web)

            meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

            meeting_date_time = meeting_date_time_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if meeting_date_time_local.date() < now.date():
                dictionary = None
                return dictionary

            if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                status = "Cancelled"
            else:
                status = "Upcoming"

            # Regular expressions to match the phone number and passcode
            phone_number_pattern = r"\b\d{3}-\d{3}-\d{4}\b"
            passcode_pattern = r"Phone Conference ID: (\d{3} \d{3} \d{3})#"

            # Find matches
            phone_number = re.search(phone_number_pattern, detail_text)
            passcode = re.search(passcode_pattern, detail_text)

            # Extract matched groups if found
            phone_number = (
                phone_number.group(0).replace("-", "") if phone_number else None
            )
            passcode = (
                passcode.group(1).replace(" ", "").replace("#", "")
                if passcode
                else None
            )
            meeting_link = None
            agenda_link = None

            dictionary = {
                "Meeting name": meeting_name,
                "Scheduled time": meeting_date_time,
                "Meeting link": meeting_link,
                "Phone number": phone_number,
                "Access ID": passcode,
                "Agenda link": agenda_link,
                "Status": status,
            }
        else:
            print("No meetings present on Rulemaking Council page")
            dictionary = None
        return dictionary

    async def unique_orpharm(self, url, timezone="America/Los_Angeles"):
        print("In unique_orpharm")
        browser_manager = BrowserManager()
        try:
            await browser_manager.launch_browser()
            page = await browser_manager.context.new_page()
            await page.goto(url)

            await page.query_selector_all("or-calendar-month")

            # Click the first page button
            await page.select_option("select.form-control", value="100")

            # Get the HTML content
            html_content = await page.content()

            # Use Beautiful Soup to parse the HTML
            soup = self.scraper.convert_to_soup(string=html_content)

            timezone_obj = pytz.timezone(timezone)

            now = datetime.now(timezone_obj)

            calendar_meetings = []

            div = soup.find("div", class_="fc-view-container")
            if not div:
                return self.meetings
            content = div.find("div", class_="or-calendar-month")
            if not content:
                return self.meetings
            rows = content.find_all("div", class_="or-calendar-week")

            agenda_wrapper = soup.find("div", class_="ms-rtestate-read ms-rte-wpbox")
            if not agenda_wrapper:
                return self.meetings
            agenda_div = agenda_wrapper.find("data-tables-web-part")
            if not agenda_div:
                return self.meetings
            agenda_table_wrapper = agenda_div.find("table")
            if not agenda_table_wrapper:
                return self.meetings
            agenda_table = agenda_table_wrapper.find("tbody")
            if not agenda_table:
                return self.meetings

            agenda_list = []
            for row in agenda_table.find_all("tr"):
                columns = row.find_all("td")
                if len(columns) < 2:
                    continue
                agenda_date = columns[0].get_text().strip()
                agenda_text = columns[1].get_text().strip()
                if "agenda" in agenda_text.lower():
                    agenda_a = columns[1].find("a")
                    if agenda_a:
                        agenda_link = agenda_a.get("href")
                        agenda_list.append(
                            {"agenda_date": agenda_date, "agenda_link": agenda_link}
                        )

            rulemaking_meeting = self.meeting_scraper(
                rule_url=self.rule_url, timezone=timezone_obj
            )

            for row in rows:
                gridcells = row.find_all("div", class_="or-calendar-day")
                for cell in gridcells:
                    aria_label = cell.get("aria-label")
                    if not aria_label:
                        continue
                    meeting_date = aria_label
                    meeting_date = re.sub(r"(\d)(st|nd|rd|th)", r"\1", meeting_date)

                    button = cell.find("button")
                    if button is not None:
                        meeting_text = button.get_text()
                        parts = meeting_text.split("Starts")
                        if len(parts) < 2:
                            continue
                        meeting_name, meeting_date_text = parts

                        name_date_pattern = r"\b\d{1,2}:\d{2}[aApP][mM]\b"
                        meeting_name = re.sub(name_date_pattern, "", meeting_name).strip()

                        pattern_with_time = r"\b\w+\s\d{1,2}\w{0,2},?\s\d{4}\s(\d{1,2}:\d{2}\s[ap]m)\s(?:and\sends)"

                        pattern_no_time = r"\b\w+\s\d{1,2}\w{0,2},?\s\d{4}\s(?:and\sends\s\w+\s\d{1,2}\w{0,2},?\s\d{4})"

                        meeting_time = ""
                        # Check for the first two formats
                        match_with_time = re.search(pattern_with_time, meeting_date_text)
                        if match_with_time:
                            meeting_time = match_with_time.group(1)

                        match_no_time = re.search(pattern_no_time, meeting_date_text)
                        if match_no_time:
                            meeting_time = ""
                        meeting_date_time_web = (meeting_date + " " + meeting_time).strip()
                        try:
                            meeting_date_time_web = datetime.strptime(
                                meeting_date_time_web, "%B %d %Y %I:%M %p"
                            )
                        except ValueError:
                            print(f"Skipping Meeting ({meeting_name}): No time data yet...")
                            continue
                        meeting_date_time_local = timezone_obj.localize(meeting_date_time_web)

                        meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)

                        meeting_date_time = meeting_date_time_utc.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z"
                        )

                        if meeting_date_time_local.date() < now.date():
                            continue

                        if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                            status = "Cancelled"
                        else:
                            status = "Upcoming"
                        meeting_link = None
                        # Normalize the meeting date for comparison
                        meeting_date_normalized = datetime.strptime(
                            meeting_date, "%B %d %Y"
                        ).strftime("%m/%d/%Y")

                        # Find the matching agenda link
                        agenda_link = None
                        for agenda in agenda_list:
                            agenda_date_normalized = datetime.strptime(
                                agenda["agenda_date"], "%m/%d/%Y"
                            ).strftime("%m/%d/%Y")
                            if meeting_date_normalized == agenda_date_normalized:
                                agenda_link = agenda["agenda_link"]
                                break

                        phone_number = None
                        access_code = None

                        if agenda_link is not None:
                            response = requests.get(agenda_link)

                            pdf_content = BytesIO(response.content)

                            # Extract text from the PDF
                            text = extract_pdf_text_from_bytes(response.content)
                            text = text.strip()

                            lines = text.split("\n")
                            # Extract text and links from the PDF
                            with pdfplumber.open(pdf_content) as pdf:
                                text = ""
                                links = []
                                for pdf_page in pdf.pages:
                                    text += pdf_page.extract_text() or ""
                                    links += pdf_page.hyperlinks
                            meeting_link = None
                            for line in lines:
                                if "Virtually via Teams: Link" in line:
                                    # Extract the specific hyperlink
                                    link_text = "teams"
                                    for link in links:
                                        if link_text in link.get("uri", ""):
                                            meeting_link = link["uri"]
                                            break

                            for i, line in enumerate(lines):
                                if " Phone Conference ID" in line:

                                    # Regular expression to extract the audio-only number
                                    phone_number_pattern = r"Audio only:\s*\(?(\d{3})\)?[-.\s]*(\d{3})[-.\s]*(\d{4})"
                                    phone_match = re.search(phone_number_pattern, line)

                                    # Regular expression to extract the phone conference ID
                                    conference_id_pattern = r"Phone Conference ID:\s*(\d{3})\s*(\d{3})\s*(\d{3})#"
                                    conference_id_match = re.search(
                                        conference_id_pattern, line
                                    )

                                    if phone_match and conference_id_match:
                                        # Format the phone number
                                        phone_number = f"+1{phone_match.group(1)}{phone_match.group(2)}{phone_match.group(3)}"

                                        # Format the conference ID
                                        access_code = f"{conference_id_match.group(1)}{conference_id_match.group(2)}{conference_id_match.group(3)}#"
                        if phone_number and access_code:
                            dictionary = {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Stream type": "twilio_phone_no_code",
                                "Access ID": access_code,
                                "Phone number": phone_number,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        else:
                            dictionary = {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_date_time,
                                "Meeting link": meeting_link,
                                "Agenda link": agenda_link,
                                "Status": status,
                            }
                        calendar_meetings.append(dictionary)

            for i, calendar_meeting in enumerate(calendar_meetings[:-1]):
                next_meeting = calendar_meetings[i + 1]

                # Convert date strings to datetime objects
                current_date = datetime.strptime(
                    calendar_meeting["Scheduled time"][:10], "%Y-%m-%d"
                )
                next_date = datetime.strptime(
                    next_meeting["Scheduled time"][:10], "%Y-%m-%d"
                )

                if (
                    calendar_meeting["Meeting name"] == next_meeting["Meeting name"]
                    and next_date - current_date == timedelta(days=1)
                    and calendar_meeting.get("Agenda link") is not None
                ):
                    # Assign agenda link to the next meeting
                    next_meeting["Agenda link"] = calendar_meeting["Agenda link"]
                    next_meeting["Stream type"] = calendar_meeting.get("Stream type")
                    next_meeting["Phone number"] = calendar_meeting.get("Phone number")
                    next_meeting["Access ID"] = calendar_meeting.get("Access ID")
                    next_meeting["Meeting link"] = calendar_meeting["Meeting link"]

            calendar_meetings = [
                meeting
                for meeting in calendar_meetings
                if datetime.strptime(meeting["Scheduled time"][:10], "%Y-%m-%d")
                .replace(tzinfo=timezone_obj)
                .date()
                >= now.date()
            ]

            if rulemaking_meeting is not None:
                combined_meetings = []
                for calendar_meeting in calendar_meetings:
                    if isinstance(rulemaking_meeting, dict):
                        rulemaking_meeting = [rulemaking_meeting]
                    for scraper_meeting in rulemaking_meeting:
                        if (
                            calendar_meeting["Meeting name"]
                            == scraper_meeting["Meeting name"]
                            and calendar_meeting["Scheduled time"]
                            == scraper_meeting["Scheduled time"]
                        ):
                            # Combine the two dictionaries
                            combined_meeting = {
                                **calendar_meeting,
                                **scraper_meeting,
                            }
                            combined_meetings.append(combined_meeting)
                            break  # Break the inner loop once a match is found

                # Update the class attribute with the combined meetings
                self.meetings = combined_meetings
            else:
                self.meetings = calendar_meetings

        finally:
            await browser_manager.close_browser()

        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://www.oregon.gov/pharmacy/pages/meetings.aspx",
        schedule_type="unique_orpharm",
        timezone="America/Los_Angeles",
    )
