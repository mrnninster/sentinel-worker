import os
import re
import pytz
import logging
from datetime import datetime
from utils.scrape_html import HtmlScraper

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None
    from schedule.schedule_scraper import run_test


class Wisconsinlegislature:
    def __init__(self):
        self.meetings = []
        self.self_contained_parser = True
        self.scraper = HtmlScraper()

    async def unique_wisconsinlegislature_async(self, url, timezone):
        response = self.scraper.scrape_html(url=url)
        soup = self.scraper.convert_to_soup(string=response)
        timezone = pytz.timezone(timezone)

        article = soup.find("article", class_="container")
        if not article:
            return self.meetings
        schedule_groups = article.find_all("div", class_="schedule-group")
        for schedule in schedule_groups:
            meeting_date_tag = schedule.find("h4")
            if not meeting_date_tag:
                continue
            meeting_date = meeting_date_tag.get_text(strip=True)
            if "–" in meeting_date:
                meeting_date = meeting_date.split("–")[1].strip()
            else:
                meeting_date = meeting_date.strip()
            rows = schedule.find_all("tr", class_="upcoming")
            for row in rows:
                meeting_div = row.find("td", class_="schedule-title")
                if not meeting_div:
                    continue
                page_link_tag = meeting_div.find("a")
                if not page_link_tag:
                    continue
                page_link = page_link_tag.get("href")
                meeting_name = page_link_tag.get_text(strip=True).replace("–", "-")
                meeting_time_tag = row.find("td", class_="schedule-time")
                if not meeting_time_tag:
                    continue
                meeting_time = meeting_time_tag.get_text(strip=True)

                try:
                    meeting_date_time_web = f"{meeting_date} {meeting_time}"
                    meeting_date_time_web = datetime.strptime(
                        meeting_date_time_web, "%B %d, %Y %I:%M %p"
                    )
                    meeting_date_time_local = timezone.localize(meeting_date_time_web)
                    meeting_date_time_utc = meeting_date_time_local.astimezone(pytz.utc)
                    meeting_date_time = meeting_date_time_utc.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    )
                except Exception:
                    continue

                # Check if meeting_date is today
                meeting_date_obj = datetime.strptime(meeting_date, "%B %d, %Y")
                today_local = datetime.now(timezone).date()
                if meeting_date_obj.date() != today_local:
                    agenda_link = None
                    meeting_link = None
                    status = "Upcoming"
                else:
                    status, agenda_link, meeting_link = await self.in_progress_detect(
                        page_link
                    )

                if re.search(r"Cancel(?:led|ed)", meeting_name, re.IGNORECASE):
                    status = "Cancelled"
                dictionary = {
                    "Meeting name": meeting_name,
                    "Scheduled time": meeting_date_time,
                    "Meeting link": meeting_link,
                    "Agenda link": agenda_link,
                    "Status": status,
                }
                self.meetings.append(dictionary)
        return self.meetings

    def unique_wisconsinlegislature(self, url, timezone):
        import asyncio

        return asyncio.run(self.unique_wisconsinlegislature_async(url, timezone))

    async def in_progress_detect(self, page_link):
        print(f"Checking {page_link} for meeting status")
        try:
            resp = self.scraper.scrape_html(url=page_link, render=True)
            if (
                not isinstance(resp, str)
                or not resp.strip()
                or (isinstance(resp, dict) and resp.get("max_failure"))
            ):
                return "Upcoming", None, None  # add stream_url slot

            page = self.scraper.convert_to_soup(string=resp)

            # --- Check status ---
            status_div = page.find("div", id="inv-player")
            status = "Upcoming"
            if status_div:
                status_text_span = status_div.find("span", class_="status-text")
                if (
                    status_text_span
                    and "live" in status_text_span.get_text(strip=True).lower()
                ):
                    status = "In Progress"

            # --- Extract agenda link ---
            agenda_link = None
            doc_div = page.find("div", class_="inv-tab--content documents")
            if doc_div:
                agenda_a = doc_div.find("a", class_="inv-button__default")
                if agenda_a:
                    agenda_link = agenda_a.get("href")

            # --- Extract stream URL from Direct Link input ---
            stream_url = None
            direct_link_div = page.find("div", class_="invintus__input-label")
            if direct_link_div:
                input_tag = direct_link_div.find_next(
                    "input", class_="form-control", readonly=True
                )
                if input_tag and input_tag.has_attr("value"):
                    value_url = input_tag["value"]
                    # Example: https://wiseye.org/player/?clientID=2789595964&eventID=2025071045
                    match = re.search(r"clientID=(\d+).*eventID=(\d+)", value_url)
                    if match:
                        client_id = match.group(1)
                        event_id = match.group(2)
                        stream_url = f"https://api.v3.invintus.com/StreamURI/hls/{client_id}/{event_id}/media.m3u8"

            return status, agenda_link, stream_url

        except Exception as e:
            logging.exception(f"Exception occurred while checking {page_link}: {e}")
            return "Upcoming", None, None


if __name__ == "__main__":
    run_test(
        url="https://wiseye.org/schedule/",
        schedule_type="unique_wisconsinlegislature",
        timezone="America/Chicago",
        get_full_archive_flag=False,
    )
