# schedule_scraper.py
import os
import sys

if __name__ == "__main__":
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv()
    _p = os.getenv('LOCAL_PROJECT_PATH'); sys.path.append(_p) if _p else None

import asyncio  # noqa: E402
import importlib  # noqa: E402
import inspect  # noqa: E402
import pytz  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone as dt_timezone  # noqa: E402
from dateutil.parser import parse  # noqa: E402
from dateutil.relativedelta import relativedelta  # noqa: E402
from logging_config import LOG_LEVEL  # noqa: E402

from logging_config import get_dedicated_debug_logger  # noqa: E402

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)


def clean_meeting_titles(meetings):
    if meetings is not None:
        for meeting in meetings:
            # Remove trailing parentheticals, e.g. "Board Meeting (Hybrid)" ->
            # "Board Meeting"
            meeting["Meeting name"] = re.sub(
                r"(?<=\S)\s*\([^)]*\)\s*$", "", meeting["Meeting name"]
            )
    return meetings


async def _call_parser_method(fn, *args, **kwargs):
    """
    Call a parser function that may be sync or async, and return its concrete result.
    - If async: await it directly
    - If sync: run it in a worker thread
    - If the result itself is awaitable (edge-case decorators): await it
    """
    if inspect.iscoroutinefunction(fn):
        result = await fn(*args, **kwargs)
    else:
        result = await asyncio.to_thread(fn, *args, **kwargs)

    if inspect.isawaitable(result):
        result = await result

    return result


async def _actual_parse_schedule(
    url: str,
    schedule_type: str,
    timezone: str,
    agenda_url: str = None,
):
    exception_occurred = False
    schedule_type = schedule_type.lower()

    # Get the appropriate parser function for the given format type
    if schedule_type.startswith("unique"):
        _, module_name = schedule_type.split("_", 1)
    else:
        module_name, _ = schedule_type.split("_", 1)

    try:
        # Dynamically import the module
        module = importlib.import_module(f"schedule.library.{module_name}")

        # Instantiate the class from the module
        class_name = module_name.capitalize()
        class_instance = getattr(module, class_name)()

        # Invoke the method
        parser_method = getattr(class_instance, schedule_type)
        log.debug(f"parser method => {parser_method}")
        log.debug(f"url => {url}")

        if agenda_url:
            meetings = await _call_parser_method(
                parser_method,
                url=url,
                timezone=timezone,
                agenda_url=agenda_url,
            )
        else:
            meetings = await _call_parser_method(parser_method, url, timezone)

    except Exception as e:
        exception_occurred = True
        if "module" in str(e) or "class" in str(e):
            log.error(f"Class for schedule_type '{schedule_type}' not found.", exc_info=True)
        elif "method" in str(e):
            log.error(f"Method for schedule_type '{schedule_type}' not found.", exc_info=True)
        else:
            log.warning(f"An error occurred while parsing the schedule: {e}.", exc_info=True)
        meetings = []

    # Normalize 'meetings' to a concrete list before cleaning
    if meetings is None:
        meetings = []
    elif inspect.isawaitable(meetings):
        meetings = await meetings
    elif not isinstance(meetings, (list, tuple)):
        try:
            meetings = list(meetings)
        except TypeError:
            meetings = [meetings]

    meetings = clean_meeting_titles(meetings)

    return meetings, exception_occurred


#  TODO: Use utils.retry_api to manage retries
default_factor = 2
default_max_retries = 2
default_initial_delay = 5  # in seconds


# wrapper for managing retries
async def parse_schedule(
    url,
    schedule_type,
    timezone,
    browser_manager=None,  # Deprecated: kept for backward compatibility
    retry_if_no_meetings=True,
    factor=default_factor,
    max_retries=default_max_retries,
    initial_delay=default_initial_delay,
    return_exception_flag=False,
    page=None,  # Deprecated: kept for backward compatibility
    local_html_file=None,  # Deprecated: kept for backward compatibility
    get_full_archive_flag=False,
    get_date_start=False,
    get_date_end=False,
    agenda_url=None,
):
    if not retry_if_no_meetings:
        meetings, exception_occurred = await _actual_parse_schedule(
            url,
            schedule_type,
            timezone,
            agenda_url,
        )
    else:
        retries = 0
        wait_time = default_initial_delay
        while retries < max_retries:
            meetings, exception_occurred = await _actual_parse_schedule(
                url,
                schedule_type,
                timezone,
                agenda_url,
            )

            # If the meetings set is not empty, return the result
            if meetings:
                break

            # If no meetings, wait for the exponential backoff time and then
            # retry
            else:
                wait_time = initial_delay * (factor**retries)
                retries += 1
                log.warning(f"No meetings found. Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
        if retries == max_retries:
            log.warning(f"Max retries reached. Exiting after {max_retries} attempts.")

    if not get_full_archive_flag:
        tz = pytz.timezone(timezone)
        if not get_date_start:
            get_date_start = datetime.now(tz)
        get_date_start = get_date_start.astimezone(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Filter meetings to only include those after get_date_start
        get_date_end = (
            get_date_end.astimezone(tz).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            + relativedelta(days=1)
            if get_date_end
            else datetime.max
        )
        get_date_end = get_date_end.replace(tzinfo=dt_timezone.utc)
        log.debug(f"get_date_start => {get_date_start}")
        log.debug(f"get_date_end => {get_date_end}")

        if meetings is not None:
            filtered_meetings = []
            for meeting in meetings:
                scheduled_time = parse(meeting["Scheduled time"])
                if get_date_start <= scheduled_time <= get_date_end:
                    filtered_meetings.append(meeting)
            meetings = filtered_meetings

    if return_exception_flag:
        return meetings, exception_occurred
    else:
        return meetings


# test function
async def main(
    url=None,
    agenda_url=None,
    timezone=None,
    schedule_type=None,
    get_full_archive_flag=False,
    get_date_start=None,
    get_date_end=None,
):
    log.info(f"{url},{agenda_url},{timezone},{schedule_type}")

    # Hardcoded arguments if used directly
    if not url:
        url = "https://kissimmeefl.portal.civicclerk.com"

    if not schedule_type:
        schedule_type = "civicclerk_table"

    if not timezone:
        timezone = "America/New_York"

    meetings = await parse_schedule(
        url=url,
        agenda_url=agenda_url,
        schedule_type=schedule_type,
        timezone=timezone,
        get_full_archive_flag=get_full_archive_flag,
        get_date_start=get_date_start,
        get_date_end=get_date_end,
    )

    for meeting in meetings:
        meeting_time = meeting.get("Scheduled time")
        if meeting_time:
            meeting_time = parse(meeting_time)
            meeting_time = meeting_time.replace(tzinfo=pytz.utc)
            local_time = meeting_time.astimezone(pytz.timezone(timezone))
            log.info(f"{meeting}")
            log.info(
                f"{local_time.strftime('%m/%d/%y at %-I:%M %p')} ({local_time.tzname()})"
            )


def run_test(
    url="https://www.youtube.com/@durhampublicschoolsboardof2290/streams",
    timezone="America/Denver",
    schedule_type="youtube_table",
    agenda_url=None,
    get_full_archive_flag=False,
    get_date_start=None,
    get_date_end=None,
):
    asyncio.run(
        main(
            url=url,
            timezone=timezone,
            agenda_url=agenda_url,
            schedule_type=schedule_type,
            get_full_archive_flag=get_full_archive_flag,
            get_date_start=get_date_start,
            get_date_end=get_date_end,
        )
    )


if __name__ == "__main__":
    run_test()
