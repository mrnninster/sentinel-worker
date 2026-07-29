import os
import sys
import json
import logging
from fuzzywuzzy import fuzz
from dateutil import parser
from datetime import datetime
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.library.youtube import Youtube
from schedule.schedule_scraper import run_test


class Miamibeach:
    """
    This is a self contained parser for miamibeach.

    Here is what the request is expected to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_miamibeach",
                "url": "https://miamibeachfl.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings?_=1752211229807",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": "https://www.youtube.com/@CityofMiamiBeachTV/streams"
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True
        self.stream_url = "https://www.youtube.com/@CityofMiamiBeachTV/streams"

    def unique_miamibeach(self, url, timezone="America/New_York"):
        try:
            data = self.scraper.scrape_html(url=url)
            data = json.loads(data)

        except Exception as e:
            log.warning(f"Error scraping html: {e}")
            return []

        # Youtube meetings
        youtube = Youtube()
        youtube_meetings = youtube.youtube_table(self.stream_url, timezone)

        # Primegov meetings
        primegov_meetings = []
        if len(data) > 0:
            for datum in data:
                try:
                    status = "Upcoming"
                    title = datum.get("title", "Unknown Meeting")

                    # Safe bounds checking for documentList
                    document_list = datum.get("documentList", [])
                    if not document_list:
                        log.warning(f"No documentList found for meeting: {title}")
                        continue

                    template_id = document_list[0].get("templateId")
                    if not template_id:
                        log.warning(f"No templateId found for meeting: {title}")
                        continue

                    agenda_link = f"https://miamibeachfl.primegov.com/Portal/Meeting?meetingTemplateId={template_id}"

                    date_time_string = f"{datum['date']} {datum['time']}"
                    date_time = parser.parse(date_time_string, fuzzy=True)
                    date_time = datetime.strftime(
                        date_time, TimeFormatter.desired_format()
                    )
                    meeting_datetime = TimeFormatter(date_time, timezone).get_utc_time(
                        as_datetime=True
                    )
                    schedule_time = meeting_datetime.isoformat().replace("+00:00", "Z")

                    meeting = {
                        "Meeting name": title,
                        "Scheduled time": schedule_time,
                        "Meeting link": None,
                        "Agenda link": agenda_link,
                        "Status": status,
                    }
                    primegov_meetings.append(meeting)

                except (KeyError, IndexError, ValueError) as e:
                    log.warning(f"Error processing meeting datum: {e}, datum: {datum}")
                    continue

        # Combine meetings
        meetings_to_remove = []
        for primegov_meeting in primegov_meetings:
            primegov_date = parser.parse(
                primegov_meeting["Scheduled time"], fuzzy=True
            ).date()
            for youtube_meeting in youtube_meetings:
                youtube_date = parser.parse(
                    youtube_meeting["Scheduled time"], fuzzy=True
                ).date()
                if (
                    fuzz.token_set_ratio(
                        primegov_meeting["Meeting name"],
                        youtube_meeting["Meeting name"],
                    )
                    > 85
                    and primegov_date == youtube_date
                ):
                    youtube_meeting["Agenda link"] = primegov_meeting["Agenda link"]
                    meetings_to_remove.append(primegov_meeting)
                    break

        # Remove matched meetings from primegov_meetings
        for meeting in meetings_to_remove:
            primegov_meetings.remove(meeting)

        self.meetings.extend(primegov_meetings)
        self.meetings.extend(youtube_meetings)
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://miamibeachfl.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings?_=1752211229807",
        schedule_type="unique_miamibeach",
        timezone="America/New_York",
        get_full_archive_flag=False,
    )
