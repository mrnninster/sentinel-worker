import os
import re
import sys
import pytz
import time
import json
import asyncio
import logging
from dateutil import parser
from fuzzywuzzy import fuzz
from collections import deque
from dotenv import load_dotenv
from pydub import AudioSegment
from urllib.parse import urlparse
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

load_dotenv()
_p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

from utils.detect import DetectUtils
from utils.scrape_html import HtmlScraper
from utils.format_time import TimeFormatter
from schedule.schedule_scraper import run_test
from stream_control.grab_stream import GrabStream
from utils.audio_classifier import AudioClassifier


class Illinoisgaming:
    def __init__(self):
        self.meetings = []
        self.scraper = HtmlScraper()
        self.generator_queue = deque()
        self.self_contained_parser = True
        self.SILENCE_THRESHOLD_DBFS = -30.0
        self.audio_classifier = AudioClassifier(
            silence_threshold_dbfs=self.SILENCE_THRESHOLD_DBFS
        )
        self.stream_page = "https://multimedia.illinois.gov/igb/igb-live.html"
        self.api_url = "https://igb.illinois.gov/content/soi/igb/en/meetings/upcoming-meetings/jcr:content/responsivegrid/container/container_293684588/container/events_feed_copy.model.json"

    async def generate_chunks(self, stream_type, url):
        grabstream_instance = GrabStream(stream_type="ts_universal-ts", url=url)
        async for chunk in grabstream_instance.fetch_chunks():
            self.generator_queue.append(chunk)

    async def unique_illinoisgaming(self, url, timezone="America/Chicago"):
        self.url = url
        response = self.scraper.scrape_html(url=self.api_url)
        data = json.loads(response)

        event_items = data["eventFeedItemList"]
        for event_item in event_items:
            if event_item["canceledEvent"] == "true":
                continue

            event_time = parser.parse(event_item["start"], fuzzy=True)
            event_time = datetime.strftime(event_time, TimeFormatter.desired_format())
            utc_time = TimeFormatter(event_time, timezone).get_utc_time(
                as_datetime=True
            )
            event_datetime = utc_time.isoformat().replace("+00:00", "Z")

            event_name = event_item["eventTitle"]
            event_stream_link = event_item["virtualList"][0]["link"]
            event_status = "Upcoming"

            current_datetime = datetime.now(pytz.UTC)
            if current_datetime.date() == utc_time.date():
                if utc_time.time() < current_datetime.time():
                    # fresh queue for this attempt
                    self.generator_queue.clear()
                    try:
                        # Python 3.8+ safe; on 3.11+ you could also use `asyncio.timeout(60)`
                        await asyncio.wait_for(
                            self.generate_chunks(
                                stream_type="ts_universal-ts",
                                url=event_stream_link,
                            ),
                            timeout=60,
                        )
                    except asyncio.TimeoutError:
                        log.info("Chunk generation timed out")
                        self.generator_queue.clear()

                    event_status = "In progress" if self.generator_queue else "Upcoming"

            self.meetings.append(
                {
                    "Meeting name": event_name,
                    "Scheduled time": event_datetime,
                    "Meeting link": event_stream_link,
                    "Agenda link": None,
                    "Status": event_status,
                }
            )
        return self.meetings


if __name__ == "__main__":
    run_test(
        url="https://igb.illinois.gov/meetings/upcoming-meetings.html",
        schedule_type="unique_illinoisgaming",
        timezone="America/Chicago",
    )
