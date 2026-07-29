# chicago.py
import os
import sys
import pytz
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from dateutil.parser import parse as dateparse
from fuzzywuzzy import fuzz
from schedule.schedule_scraper import run_test
from pytz import timezone as pytz_timezone

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Chicago:
    def __init__(self):
        self.meetings = []
        self.live_meets = []
        self.in_progress_meetings = []
        self.agenda_link = None
        self.meeting_name = None
        self.meeting_link = None
        self.scraper = HtmlScraper()
        self.meeting_date_time = None
        self.main_url = "https://www.chicityclerk.com"
        self.self_contained_parser = True

    def patch_meeting_to_db(self, meeting):
        ...
        """
        Placeholder function to patch the updated meeting or new meeting to the remote DB.
        Replace with actual DB call later.
        """
        log.info(f"Patching meeting to DB: {meeting}")
        meeting["Scheduled time"] = (
            datetime.now(pytz.UTC)
            .replace(
                minute=(datetime.now(pytz.UTC).minute // 15) * 15,
                second=0,
                microsecond=0,
            )
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )
        self.in_progress_meetings.append(meeting)

    def get_in_progress_meetings(self):
        """
        This method is a helper method for `unique_chicago`.
        It is used to get all the meetings that are currently in progress.

        Returns:
            list | None: A list of meetings or None if there are no meetings in progress
        """
        try:
            log.info(f"Starting scrape {self.main_url}")
            in_progress_html = self.scraper.scrape_html(
                url=self.main_url, render="true", wait_for_seconds=5
            )
            soup = self.scraper.convert_to_soup(string=in_progress_html)
            log.info("Converted scraped html to soup")

            # Get all meeting alerts
            alerts = soup.find_all("div", class_="top-alert-item row")
            if not alerts or len(alerts) == 0:
                log.info("No alerts found")
                return None

            # Get all alert URLs
            all_alert_links = set()
            for alert in alerts:
                link_div = alert.find("div").find("a") if alert else None
                alert_link = (
                    "https://www.chicityclerk.com" + link_div.get("href")
                    if link_div
                    else None
                )
                log.info(f"Alert link: {alert_link}")
                all_alert_links.add(alert_link)

            log.info("Fetching live meets")
            for alert_link in all_alert_links:

                meet_page_html = self.scraper.scrape_html(
                    url=alert_link,
                    render="true",
                    wait_for_selector="div.embedded-meeting-video",
                )
                page_soup = self.scraper.convert_to_soup(string=meet_page_html)
                iframe = page_soup.find("iframe")

                meeting_link = iframe.get("src").strip()
                # log.debug(f"Meeting link: {meeting_link}")

                live_name = page_soup.find(
                    "section", id="block-zurb-occ-pagetitle"
                ).get_text(strip=True)
                # log.debug(f"Live name: {live_name}")
                self.live_meets.append(
                    {
                        "meeting_name": live_name.lower(),
                        "meeting_link": meeting_link,
                    }
                )
            if not self.live_meets:
                log.info("no live meets found")
                return None
            # log.info(f"Live meets: {self.live_meets}")
            return self.live_meets

        except Exception as e:
            log.info(f"An error occurred while checking for live meets: {e}")

    def update_meetings(self, timezone):
        """
        Updates the status of the associated meeting to `In progress`
        and adds the stream URL. Uses fuzzy matching if exact matches
        are not found. Ensures that in-progress meetings keep their original name
        in the returned dictionary, even if patched with a new name internally.
        """

        def clean_meeting_name(meeting_name):
            cleaned_name = " ".join(
                meeting_name.replace("meeting", "").strip().lower().split()
            )
            # log.info(f"Cleaned name: {cleaned_name}")
            return cleaned_name

        unmatched_live_meets = self.live_meets[:]  # Initialize with all live meetings
        today = datetime.now(timezone).date()

        for live_meet in self.live_meets:
            # log.info(f"Checking if live meeting matches any scheduled: {live_meet}")
            live_name_clean = clean_meeting_name(live_meet["meeting_name"])

            # log.info("Scheduled meetings:")
            for meeting in self.meetings:
                scheduled_time = dateparse(meeting["Scheduled time"])

                if scheduled_time.date() > today:
                    log.info(f"Skipping meeting: {meeting['Meeting name']}")
                    continue

                # log.info(f"Meeting: {meeting}")
                scheduled_name_clean = clean_meeting_name(meeting["Meeting name"])

                # Check if the live name is in the scheduled name or vice versa
                if (
                    live_name_clean in scheduled_name_clean
                    or scheduled_name_clean in live_name_clean
                ):
                    # log.info(f"Meeting in progress: {meeting.get('Meeting name')}")
                    meeting["Status"] = "In progress"
                    meeting["Meeting link"] = live_meet["meeting_link"]
                    unmatched_live_meets.remove(live_meet)  # Remove matched live meet
                    break

        # Try fuzzy matching for unmatched live meetings
        for live_meet in unmatched_live_meets:
            best_match = None
            highest_ratio = 0
            for meeting in self.meetings:
                if meeting["scheduled_time"].date() > today:
                    continue
                ratio = fuzz.partial_ratio(
                    live_meet["meeting_name"], meeting["Meeting name"]
                )
                if ratio > highest_ratio and ratio > 70:  # Only match if ratio > 70
                    highest_ratio = ratio
                    best_match = meeting

            if best_match:
                log.info(
                    f"Fuzzy match found: {best_match['Meeting name']} -> {live_meet['meeting_name']}"
                )
                best_match["Original name"] = best_match[
                    "Meeting name"
                ]  # Store the original name
                best_match["Meeting name"] = live_meet[
                    "meeting_name"
                ]  # Update with new name
                best_match["Status"] = "In progress"
                best_match["Meeting link"] = live_meet["meeting_link"]
                # Patch the changed meeting name to the remote DB
                self.patch_meeting_to_db(best_match)
            else:
                # Add unmatched live meeting as a new scheduled meeting
                new_meeting = {
                    "Meeting name": live_meet["meeting_name"],
                    "Scheduled time": datetime.now(pytz.UTC).replace(
                        minute=(datetime.now(pytz.UTC).minute // 15) * 15,
                        second=0,
                        microsecond=0,
                    ),
                    "Meeting link": live_meet["meeting_link"],
                    "Agenda link": None,
                    "Status": "In progress",
                }
                log.warning(f"Unmatched live meeting: {new_meeting['Meeting name']}")
                self.patch_meeting_to_db(new_meeting)

    def unique_chicago(self, url: str, timezone: str = "America/Chicago") -> list:
        """
        Scraper that gets all the meetings for https://www.chicityclerk.com/

        Args:
            url (str): The Chicago API URL https://api.chicityclerkelms.chicago.gov/meeting
            timezone (str): The local timezone of the scraped meetings. Expected format: "America/Chicago"

        Returns:
            list: A list of dictionaries, each dictionary representing a meeting in the format:
                {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status
                }
        """
        pytimezone = pytz.timezone(timezone)
        response = requests.get(url=url)
        data = response.json()
        data = data["data"]
        temp_meetings = []

        for meeting in data:
            utc_meet_time_string = meeting["date"]
            date_obj = datetime.fromisoformat(utc_meet_time_string)

            if date_obj.date() >= datetime.now(pytz.UTC).date():
                self.meeting_name = meeting["body"]
                self.meeting_link = (
                    None if meeting["videoLink"] == "" else meeting["videoLink"]
                )
                self.agenda_link = next(
                    (
                        file["path"]
                        for file in meeting["files"]
                        if file["attachmentType"] == "Agenda"
                    ),
                    None,
                )
                self.meeting_date_time = date_obj.strftime("%Y-%m-%dT%H:%M:%S.000Z")

                temp_meetings.append(
                    {
                        "Meeting name": self.meeting_name,
                        "Scheduled time": self.meeting_date_time,
                        "Meeting link": self.meeting_link,
                        "Agenda link": self.agenda_link,
                        "Status": "Upcoming",
                    }
                )

        in_progress_meetings = self.get_in_progress_meetings()
        # log.info(f"In progress meetings: {in_progress_meetings}")
        if in_progress_meetings is not None:
            self.meetings = temp_meetings
            self.update_meetings(pytimezone)
            temp_meetings.extend(self.in_progress_meetings)

        # log.debug(f"Temp meetings: {temp_meetings}")

        # Filter for unique meetings
        unique_meetings = []
        seen_meetings = set()  # Track unique (name, date) pairs

        for meeting in temp_meetings:
            meeting_name = meeting["Meeting name"]
            meeting_date = dateparse(meeting["Scheduled time"]).date()

            # Check if this (meeting_name, meeting_date) has already been added
            if (meeting_name, meeting_date) not in seen_meetings:
                seen_meetings.add((meeting_name, meeting_date))
                unique_meetings.append(meeting)
            else:
                log.info(
                    f"Skipping duplicate meeting: {meeting_name} on {meeting_date}"
                )

        # Only update self.meetings at the end, after filtering duplicates
        self.meetings = unique_meetings
        # log.info(f"Unique meetings: {self.meetings}")
        return self.meetings


if __name__ == "__main__":

    url = "https://api.chicityclerkelms.chicago.gov/meeting"
    timezone = "America/Chicago"
    schedule_type = "unique_chicago"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)

    run_test(
        url=url,
        timezone=timezone,
        schedule_type=schedule_type,
        # get_date_start=datetime.now(tz) - timedelta(days=10),
        # get_date_end=datetime.now(tz) - timedelta(days=1),
    )
