import os
import json
from dotenv import load_dotenv
from datetime import datetime
from dateutil import parser, relativedelta

if __name__ == "__main__":
    import sys

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter


class Westpalmbeach:
    """
    This is a self contained scraper.

    Here is what the request is expect to look like
    {
        "geodicts": [
            {
                "schedule_type": "unique_westpalmbeach",
                "url": "https://www.wpb.org/ocapi/calendars/getcalendaritems",
                "timezone": "America/New_York",
                "glitch_meetings": [],
                "debug": null,
                "channel_url": "https://www.youtube.com/@WestPalmTV/streams"
            }
        ],
        "version": "test"
    }
    """

    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.self_contained_parser = True

        # Generate start and stop dates
        start_date = datetime.today()
        end_date = start_date + relativedelta.relativedelta(
            months=1
        )  # Exactly one month from today

        self.start_date = start_date.strftime("%Y-%m-%d")  # YYYY-MM-DD
        self.stop_date = end_date.strftime("%Y-%m-%d")  # YYYY-MM-DD

        # Calendar ID
        self.calendar_id = "377cef33-6775-4638-9348-735c16c754e8"

        # Agenda page
        self.agenda_page = "https://www.wpb.org/Our-City/Meetings-Agendas"

    def unique_westpalmbeach(self, url, timezone="America/New_York"):
        data = {
            "LanguageCode": "en-US",
            "Ids": [self.calendar_id],
            "StartDate": self.start_date,
            "EndDate": self.stop_date,
        }

        # Get api data
        response = self.scraper.post_with_scraperapi(url, data)
        json_response = json.loads(response)

        # Generate meetings
        data = json_response["data"]
        meetings = [item for day in data for item in day["Items"]]
        # print(meetings)

        agendapage_html = self.scraper.scrape_html(
            schedule_type="unique_westpalmbeack", url=self.agenda_page
        )
        agendapage_soup = self.scraper.convert_to_soup(agendapage_html)
        divs = agendapage_soup.find_all(
            "div", class_="list-item-container homepage-show"
        )
        cancelled_meets = []
        for div in divs:
            is_cancelled = div.find("span", class_="canceled-tag")
            if is_cancelled:
                div_strings = []
                for string in div.find("h2", class_="list-item-title").stripped_strings:
                    div_strings.append(string)
                event_date = div.find(
                    "p", class_="event-date published-on small-text"
                ).string
                event_date = event_date.split(" to ")[0]
                meeting_date_time = parser.parse(event_date, fuzzy=True)
                meeting_date_time = datetime.strftime(
                    meeting_date_time, TimeFormatter.desired_format()
                )
                utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                    as_datetime=True
                )
                event_date = utc_time.isoformat().replace("+00:00", "Z")
                cancelled_meets.append({"name": div_strings[1], "date": event_date})

        print(f"Cancelled meets => {cancelled_meets}")

        for meet in meetings:
            meeting_date_time = parser.parse(meet["DateTime"], fuzzy=True)
            meeting_date_time = datetime.strftime(
                meeting_date_time, TimeFormatter.desired_format()
            )
            utc_time = TimeFormatter(meeting_date_time, timezone).get_utc_time(
                as_datetime=True
            )
            schedule_time = utc_time.isoformat().replace("+00:00", "Z")

            title = meet["Name"].replace("\n", "")

            for cancelled_meet in cancelled_meets:
                if (
                    title == cancelled_meet["name"]
                    and schedule_time == cancelled_meet["date"]
                ):
                    status = "Cancelled"
                    break
                else:
                    status = "Upcoming"

            # Create meeting object
            meeting = {
                "Meeting name": title,
                "Scheduled time": schedule_time,
                "Meeting link": None,
                "Agenda link": None,
                "Status": status,
            }

            # Add meeting to list
            self.meetings.append(meeting)

        # Return meetings
        return self.meetings


if __name__ == "__main__":
    from pytz import timezone as pytz_timezone
    from schedule.schedule_scraper import run_test

    url = f"https://www.wpb.org/ocapi/calendars/getcalendaritems"
    timezone = "America/New_York"
    schedule_type = "unique_westpalmbeach"

    # Make datetime.now() timezone aware
    tz = pytz_timezone(timezone)

    run_test(url, timezone, schedule_type)
