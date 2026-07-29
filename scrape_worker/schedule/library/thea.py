import logging
import re
import pytz
from datetime import datetime

from schedule.schedule_scraper import run_test
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter

log = logging.getLogger(__name__)


class Thea:
    self_contained_parser = True

    def __init__(self):
        self._scraper = HtmlScraper()

    def extract_date_from_element(self, p_element) -> datetime | None:
        """Extract date from paragraph element, skipping dates inside <s> tags"""
        date_matches = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", p_element.get_text())
        if not date_matches:
            return None

        for date_str in date_matches:
            for text_node in p_element.find_all(string=True):
                if date_str in text_node:
                    parent = text_node.parent
                    is_in_strike = False
                    while parent and parent.name != "p":
                        if parent.name == "s":
                            is_in_strike = True
                            break
                        parent = parent.parent

                    if not is_in_strike:
                        try:
                            return datetime.strptime(date_str, "%m/%d/%Y")
                        except Exception:
                            continue

        try:
            return datetime.strptime(date_matches[-1], "%m/%d/%Y")
        except Exception:
            return None

    def extract_title_and_time(self, text: str) -> tuple[str, str]:

        working_text = text

        # Extract and remove any date patterns (MM/DD/YYYY)
        date_pattern = r"\b\d{1,2}/\d{1,2}/\d{4}\b"
        working_text = re.sub(date_pattern, "", working_text)

        # Extract first time occurrence
        time_pattern = r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?|[AP]M)?\b"
        time_match = re.search(time_pattern, working_text, re.IGNORECASE)
        time_token = ""
        if time_match:
            time_token = time_match.group(0)
            # Remove the time from the title
            working_text = working_text.replace(time_token, "")

        # Normalize time: "p.m." -> "PM", "a.m." -> "AM"
        if time_token:
            time_token = re.sub(
                r"\b([ap])\.?m\.?\b",
                lambda m: m.group(1).upper() + "M",
                time_token,
                flags=re.IGNORECASE,
            )

        # Clean up extra spaces
        title = " ".join(working_text.split())

        return title, time_token

    def unique_thea(self, url: str, timezone: str = "America/New_York") -> list:
        meetings = []
        tz_info = pytz.timezone(timezone)

        response = self._scraper.scrape_html(url=url, render="true")
        soup = self._scraper.convert_to_soup(string=response)
        container = soup.find("div", id="meeting")
        if not container:
            return meetings

        rows = container.find_all(
            "div", class_="vc_row wpb_row vc_inner vc_row-fluid meet_sec"
        )

        for row in rows:
            columns = row.find_all(
                "div", class_="wpb_column vc_column_container vc_col-sm-4"
            )
            for col in columns:
                wrapper = col.find("div", class_="wpb_wrapper")
                if not wrapper:
                    continue

                _month_header = wrapper.find("h3")

                for p in wrapper.find_all("p"):
                    try:
                        tokens = [t.strip() for t in p.stripped_strings if t.strip()]
                        if not tokens:
                            continue
                        info_line = " ".join(tokens[1:]) if len(tokens) > 1 else ""

                        raw_text = p.get_text(" ", strip=True)
                        is_cancelled = bool(
                            re.search(r"cancelled", raw_text, re.IGNORECASE)
                        )

                        date_obj = self.extract_date_from_element(p)
                        title, time_token = self.extract_title_and_time(info_line)
                        if not date_obj or not time_token:
                            continue

                        # After normalization, expect AM/PM format; skip if not present
                        if not re.search(r"AM|PM", time_token):
                            log.warning(
                                f"Time token missing AM/PM after normalization: '{time_token}'. Skipping entry."
                            )
                            continue

                        meeting_dt_naive = datetime.strptime(
                            f"{date_obj.strftime('%m/%d/%Y')} {time_token}",
                            "%m/%d/%Y %I:%M %p",
                        )

                        formatted_local = meeting_dt_naive.strftime(
                            TimeFormatter.desired_format()
                        )
                        utc_dt = TimeFormatter(formatted_local, timezone).get_utc_time(
                            as_datetime=True
                        )
                        meeting_iso = utc_dt.isoformat().replace("+00:00", "Z")

                        meeting_name = title
                        status = "Upcoming"
                        if is_cancelled:
                            status = "Cancelled"

                        if meeting_dt_naive.date() < datetime.now(tz_info).date():
                            continue

                        meetings.append(
                            {
                                "Meeting name": meeting_name,
                                "Scheduled time": meeting_iso,
                                "Meeting link": None,
                                "Agenda link": None,
                                "Status": status,
                            }
                        )
                    except Exception as exception:
                        log.warning(f"Error parsing meeting entry: {exception}")

        return meetings


if __name__ == "__main__":
    run_test(
        url="https://www.tampa-xway.com/agency-info/board/meeting-schedule/",
        schedule_type="unique_thea",
        timezone="America/New_York",
    )
