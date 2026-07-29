# retry_api.py
import time
import asyncio
import requests
import logging
from random import uniform
from functools import wraps
from httpx import HTTPStatusError
from logging_config import get_dedicated_debug_logger, LOG_LEVEL

httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)


class AttrDict(dict):
    """
    Dict subclass that allows both key and attribute-style access.

    Example:
        d = AttrDict({"status_code": 500})
        d["status_code"] == d.status_code == 500
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        # Preserve normal attribute setting for private / internals
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self[name] = value


# TODO: To be refactored to support all retry and group all retry types to make decorators


def async_api_retry(max_attempts=5, backoff_factor=2, initial_delay=0.1):
    """
    Retry decorator for async functions.

    Parameters:
    - max_attempts: Maximum number of retry attempts.
    - backoff_factor: Factor by which the delay is increased after each attempt.
    - initial_delay: Initial delay before retrying.
    """

    def decorator(api_call):
        @wraps(api_call)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_attempts):
                try:
                    response = await api_call(*args, **kwargs)
                    return response
                except Exception as e:
                    # Check if this is a 4xx error (except 429) - don't retry
                    # these
                    should_retry = True
                    if hasattr(e, "response") and hasattr(e.response, "status_code"):
                        status_code = e.response.status_code
                        if 400 <= status_code < 500 and status_code != 429:
                            log.warning(
                                f"API call '{api_call.__name__}' received "
                                f"{status_code} error - not retrying (client error)"
                            )
                            should_retry = False

                    log.warning(
                        f"API call '{api_call.__name__}' failed on attempt {attempt + 1}: {e}"
                    )
                    log.debug(f"Args: {args}, Kwargs: {kwargs}")

                    if not should_retry or attempt == max_attempts - 1:
                        if not should_retry:
                            log.warning(
                                f"API call '{api_call.__name__}' failed with "
                                "non-retryable 4xx error"
                            )
                        else:
                            log.warning(
                                f"API request final failure after {max_attempts} attempts."
                            )
                        raise e

                    jitter = uniform(0.0, delay)
                    sleep_duration = delay + jitter
                    log.warning(f"Retrying in {sleep_duration:.2f} seconds...")
                    await asyncio.sleep(sleep_duration)
                    delay *= backoff_factor

        return wrapper

    return decorator


# Revert original retry_on_failure back to synchronous def
def retry_on_failure(
    api_call,
    *args,
    max_attempts=5,
    backoff_factor=2,
    initial_delay=0.1,
    **kwargs,
):
    """
    Retry an API call if it fails.

    Args:
        api_call: The API call to execute.
        *args: Positional arguments to pass to the API call.
        max_attempts: The maximum number of attempts to make.
        backoff_factor: The factor by which the delay increases.
        initial_delay: The initial delay.
        **kwargs: Keyword arguments to pass to the API call.

    Returns:
        The result of the API call, if successful, or a dictionary with a max_failure flag if unsuccessful after max_attempts.

    Raises:
        The exception raised by the API call, if it still fails after max_attempts and does not have the specific error message from OpenAI.
    """
    delay = initial_delay
    for attempt in range(max_attempts):
        try:
            # Original synchronous call
            response = api_call(*args, **kwargs)
            if isinstance(response, requests.Response):
                # Original synchronous call
                response.raise_for_status()
            # print(f"API call '{api_call.__name__}' completed successfully")
            return response
        except Exception as e:
            log.warning(f"API call '{api_call.__name__}' failed: {str(e)}")
            # Optionally, if you still want to log the response content when available:
            error_response = getattr(e, "response", None)
            status_code = (
                getattr(error_response, "status_code", 600) if error_response else 601
            )  # custom status code of 6XX if no status code is provided
            error_text = (
                getattr(error_response, "text", "No response text")
                if error_response
                else "Error text not provided"
            )
            if error_response and hasattr(error_response, "text"):
                log.warning(
                    f"API request failed with response content: {error_response.text}"
                )

            # Don't retry on 401 Unauthorized (authentication required, not just API key)
            if status_code == 401:
                log.warning(
                    f"API call '{api_call.__name__}' received 401 Unauthorized - "
                    "not retrying (authentication required)"
                )
                return AttrDict(
                    {
                        "max_failure": True,
                        "status_code": status_code,
                        "text": error_text,
                    }
                )
            # Don't retry on 403 Forbidden (permission denied, won't change with retries)
            if status_code == 403:
                log.warning(
                    f"API call '{api_call.__name__}' received 403 Forbidden - "
                    "not retrying (permission denied)"
                )
                return AttrDict(
                    {
                        "max_failure": True,
                        "status_code": status_code,
                        "text": error_text,
                    }
                )
            # Don't retry on 405 Method Not Allowed (endpoint doesn't exist)
            if status_code == 405:
                log.warning(
                    f"API call '{api_call.__name__}' received 405 Method Not Allowed - "
                    "not retrying (endpoint unavailable)"
                )
                return AttrDict(
                    {
                        "max_failure": True,
                        "status_code": status_code,
                        "text": error_text,
                    }
                )

            if attempt == max_attempts - 1:
                log.warning("API request final failure")
                return AttrDict(
                    {
                        "max_failure": True,
                        "status_code": status_code,
                        "text": error_text,
                    }
                )

            # No stop event check in the original synchronous version
            # if stop_event and stop_event.is_set():
            # print(f"Stop event set during retry for {api_call.__name__}.
            # Aborting retries.")
            #     return {"stopped": True}

            jitter = uniform(0.0, delay)
            sleep_duration = delay + jitter
            log.warning(f"API request failed. Retrying in {sleep_duration:.2f} seconds...")
            # Original synchronous time.sleep
            time.sleep(sleep_duration)
            delay *= backoff_factor


# Keep the NEW async version added previously
async def async_retry_on_failure(
    api_call,
    *args,
    max_attempts=5,
    backoff_factor=2,
    initial_delay=0.1,
    stop_event: asyncio.Event = None,
    **kwargs,
):
    """
    Asynchronously retry an API call if it fails, checking a stop event, using asyncio.sleep and asyncio.to_thread.

    Args:
        api_call: The potentially synchronous API call to execute (will be run in a thread).
        *args: Positional arguments to pass to the API call.
        max_attempts: The maximum number of attempts to make.
        backoff_factor: The factor by which the delay increases.
        initial_delay: The initial delay.
        stop_event: An optional asyncio.Event to signal stopping the retry loop.
        **kwargs: Keyword arguments to pass to the API call.

    Returns:
        The result of the API call, if successful.
        A dictionary {"max_failure": True} if unsuccessful after max_attempts.
        A dictionary {"stopped": True} if stop_event was set during retry waits.

    Raises:
        The exception raised by the API call if it's re-raised after final failure (currently returns dict).
    """
    delay = initial_delay
    for attempt in range(max_attempts):
        try:
            # Run the potentially blocking API call in a separate thread
            response = await asyncio.to_thread(api_call, *args, **kwargs)
            if isinstance(response, requests.Response):
                # Run potentially blocking raise_for_status in thread as well
                await asyncio.to_thread(response.raise_for_status)
            return response
        except Exception as e:
            log.warning(f"Async API call '{api_call.__name__}' failed: {str(e)}")
            error_response = getattr(e, "response", None)
            status_code = (
                getattr(error_response, "status_code", 600) if error_response else 601
            )
            error_text = (
                getattr(error_response, "text", "No response text")
                if error_response
                else "Error text not provided"
            )
            if error_response and hasattr(error_response, "text"):
                log.warning(
                    f"API request failed with response content: {error_response.text}"
                )

            if attempt == max_attempts - 1:
                log.warning(f"Async API request final failure for {api_call.__name__}")
                return AttrDict(
                    {
                        "max_failure": True,
                        "status_code": status_code,
                        "text": error_text,
                    }
                )

            # Check stop event BEFORE sleeping
            if stop_event and stop_event.is_set():
                log.warning(
                    f"Stop event set during async retry for {api_call.__name__}. Aborting retries."
                )
                return AttrDict({"stopped": True})

            jitter = uniform(0.0, delay)
            sleep_duration = delay + jitter
            log.warning(
                f"Async API request failed. Retrying in {sleep_duration:.2f} seconds..."
            )
            # Use asyncio.sleep instead of time.sleep
            await asyncio.sleep(sleep_duration)
            delay *= backoff_factor


# NOTE: Added support for continous retry when response if false
# This function uses the original synchronous retry_on_failure internally
# It should remain synchronous as well to maintain compatibility where it's
# used
def retry_on_false(
    api_call,
    false_conditions,
    *args,
    max_attempts=5,
    backoff_factor=2,
    initial_delay=0.1,
    **kwargs,
):  # Removed stop_event and async
    """
    Retries when a defined failure condition has been met

    Args:
    -----
    api_call: The API call to execute.
    false_conditions: A list of conditions that has to be satisfied to the false_condition to be triggered.

        Example: dict = {"status": False}
        Example: request.Response = {"status": "true"}

    *args: Positional arguments to pass to the API call.
    max_attempts: The maximum number of attempts to make.
    backoff_factor: The factor by which the delay increases.
    initial_delay: The initial delay.
    **kwargs: Keyword arguments to pass to the API call.

    Returns:
        The result of the API call, if successful, or a dictionary with a max_failure flag if unsuccessful after max_attempts.

    Raises:
        The exception raised by the API call, if it still fails after max_attempts and does not have the specific error message from OpenAI.
    """
    delay = initial_delay
    for attempt in range(max_attempts):
        # Call the original synchronous retry_on_failure
        response = retry_on_failure(
            api_call, *args, max_attempts=1, **kwargs
        )  # Use original sync version

        # Handle potential failure from inner retry (no stop check needed)
        if isinstance(response, dict) and response.get("max_failure"):
            return response  # Propagate failure signal

        # Assuming response is now a successful requests.Response object
        try:
            # Use synchronous .json()
            response_data = response.json()
        except Exception as json_err:
            log.warning(f"Error decoding JSON response in retry_on_false: {json_err}")
            response_data = {}  # Treat as non-match to retry

        # Check if response_data matches any of the false_conditions
        is_false_condition = any(
            (isinstance(cond, dict) and cond.items() <= response_data.items())
            or response_data == cond
            for cond in false_conditions
        )

        if not is_false_condition:
            return response  # Condition not met, return successful response

        # Condition met, proceed to retry logic
        if attempt == max_attempts - 1:
            log.warning(
                f"API request final failure after condition met for {max_attempts} attempts"
            )
            return AttrDict({"max_failure": True})

        # No stop event check needed
        # if stop_event and stop_event.is_set():
        # print(f"Stop event set during retry_on_false for {api_call.__name__}.
        # Aborting retries.")
        #     return {"stopped": True}

        jitter = uniform(0.0, delay)
        sleep_duration = delay + jitter
        log.warning(f"False condition met. Retrying in {sleep_duration:.2f} seconds...")
        time.sleep(sleep_duration)  # Use sync sleep
        delay *= backoff_factor


# NOTE: Retry decorator for monitor_stream
# This one needs to remain async, but the stop_event check needs fixing if
# added previously
def retry_with_backoff(
    max_retries=5, backoff_factor=1, exceptions=(Exception,)
):  # Removed stop_event from signature
    def retry_decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    log.info(
                        f"starting retry loop for {func.__name__}. Attempt no. {retries + 1}"
                    )
                    return await func(*args, **kwargs)  # Attempt to run the function
                except exceptions as e:
                    log.warning(f"retry exception caught: {e}", exc_info=True)
                    retries += 1
                    if max_retries is not None and retries >= max_retries:
                        log.warning(f"{func.__name__} failed after {max_retries} retries.")
                        raise  # After max retries, re-raise the exception

                    # Remove stop event check here as it wasn't part of the
                    # original plan for this decorator
                    # if stop_event and stop_event.is_set():
                    # print(f"Stop event set during retry_with_backoff for
                    # {func.__name__}. Aborting retries.")
                    # raise asyncio.CancelledError("Stop event triggered retry
                    # cancellation") # Raise CancelledError

                    sleep_time = backoff_factor**retries
                    log.warning(f"Retrying {func.__name__} in {sleep_time:.2f} seconds...")
                    await asyncio.sleep(sleep_time)  # Wait before retrying

        return wrapper

    return retry_decorator


# NOTE Retry decorator for grab_ts
# This one likely doesn't need stop_event check as it's for lower-level stream
# grabbing
def retry_async(
    infinite_retries=False,
    max_retries=5,
    delay=0.1,
    backoff_factor=3,
    max_delay=60,
    retry_timeout=5 * 60 * 60,
):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            retries = 0
            current_delay = delay
            start_time = time.time()
            result = None

            while infinite_retries or retries < max_retries:
                try:
                    if retries > 0:
                        log.warning(f"Attempting {fn.__name__} - Try {retries + 1}")
                    result = await fn(*args, **kwargs)
                    if retries > 0:
                        log.info(f"{fn.__name__} succeeded on attempt {retries + 1}.")
                    return result
                except Exception as e:
                    if type(e) is HTTPStatusError:
                        if str(e.response.status_code).startswith("4"):
                            log.warning(
                                f"Breaking due to HTTP {e.response.status_code} error."
                            )
                            raise
                    elapsed_time = time.time() - start_time
                    if retry_timeout and elapsed_time > retry_timeout:
                        log.warning(
                            f"Retry timeout of {retry_timeout} seconds reached for {fn.__name__}. Ending retries."
                        )
                        raise
                    log.warning(f"Exception caught in retry_async for {fn.__name__}: {e}", exc_info=True)
                    if infinite_retries or retries < max_retries:
                        await asyncio.sleep(current_delay)
                        retries += 1
                        current_delay = min(current_delay * backoff_factor, max_delay)
                    else:
                        log.warning(
                            f"Function {fn.__name__} failed after {max_retries} retries."
                        )
                        raise
            return result

        return wrapper

    return decorator
