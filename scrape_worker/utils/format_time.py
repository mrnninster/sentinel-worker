import pytz
from typing import Union
from datetime import datetime

from logging_config import get_dedicated_debug_logger, LOG_LEVEL

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)


class TimeFormatter:

    def __init__(self, datetime_string, timezone):
        """
        datetime_string: The date and time in string format, it should be represent in the following format
            DD-MM-YYYY, HH:MM:SS AM or PM, an example would be 01-04-2024, 10:31:30 AM

        timezone: The timezone associated with the geo of the meeting, this should be a string format of the
            timezones under pytz, e,g "America/New_York"
        """
        self.timezone = timezone
        self.datetime_string = datetime_string

    @staticmethod
    def desired_format() -> str:
        """returns the string format that the datetime_string value should be expressed in"""
        return "%d-%m-%Y, %I:%M:%S %p"

    def convert_datetime_format(
        input_datetime: str,
        input_format: str = "%d-%b-%Y, %I:%M:%S %p",
        output_format: str = "%d-%m-%Y, %I:%M:%S %p",
    ) -> str:
        """
        Converts a datetime string from one format to another.
        Args:
            input_datetime (str): The datetime string to be converted.
            input_format (str, optional): The format of the input datetime string. Defaults to "%d-%b-%Y, %I:%M:%S %p".
            output_format (str, optional): The desired format of the output datetime string. Defaults to "%d-%m-%Y, %I:%M:%S %p".
        Returns:
            str: The datetime string in the desired output format.
        """
        # Parse the input string into a datetime object
        datetime_obj = datetime.strptime(input_datetime, input_format)

        # Format the datetime object into the desired output format
        return datetime_obj.strftime(output_format)

    def verify_date_format(self) -> bool:
        """verifies that datetime_string is expressed in the correct format"""
        try:
            # Try to parse the date string using the provided format
            datetime.strptime(self.datetime_string, TimeFormatter.desired_format())
            return True
        except ValueError:
            # Expected to fail for invalid formats, no need to log
            return False

    def get_utc_time(self, as_datetime=False) -> Union[datetime, str]:
        """
        Convert a naive datetime string to a UTC timezone-aware datetime.

        This method first verifies the format of the datetime string, parses it into a
        naive datetime object, localizes it to a specific timezone, and then converts
        it to UTC. Optionally, it returns the datetime in ISO format or as a
        timezone-aware datetime object.

        Parameters:
        as_datetime (bool): If True, returns a datetime object. If False, returns a
                            string in ISO 8601 format with 'Z' denoting UTC time.

        Returns:
        Union[datetime, str]: Timezone-aware datetime object in UTC or ISO formatted string,
                              depending on the `as_datetime` parameter.

        Raises:
        ValueError: If the datetime string does not match the expected format.
        """

        # Check that datetime string is in the correct format
        if self.verify_date_format():

            # Parse the date string into a datetime object
            meeting_time = datetime.strptime(
                self.datetime_string, TimeFormatter.desired_format()
            )

            # Creates a pytz instance of the local timezone
            local_timezone = pytz.timezone(self.timezone)

            # Localize the datetime object to the desired pytz timezone
            localized_meeting_time = local_timezone.localize(meeting_time)

            # Convert the localized datetime to UTC
            utc_meeting_time = localized_meeting_time.astimezone(pytz.utc)

            if as_datetime:
                return utc_meeting_time

            else:
                isotime = utc_meeting_time.isoformat().replace("+00:00", "Z")
                return isotime

        else:
            raise Exception("Invalid datetime string")
