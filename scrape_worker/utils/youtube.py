import os
import re
import json
import pytz
from tqdm import tqdm
from datetime import datetime
from pytubefix import YouTube as YT
from googleapiclient.discovery import build
import requests
from fuzzywuzzy import fuzz
from dotenv import load_dotenv

from logging_config import get_dedicated_debug_logger, LOG_LEVEL

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)

load_dotenv()


# =============================================================================
# Fuzzy Matching Utilities (used by detect.py and youtube_watcher.py)
# =============================================================================


def normalize_for_matching(text: str) -> str:
    """
    Normalize text for fuzzy matching by removing common noise.

    Handles:
    - Lowercase conversion
    - & → and
    - Punctuation removal
    - Common noise words (committee, subcommittee, senate, house, of, the, virginia)
    - Legislative prefixes like "Senate of Virginia:"
    - Date suffixes like "on 2026-01-27"
    - Status tags like [Finished], [Live]

    Example:
        'Senate of Virginia: Finance & Appropriations on 2026-01-27 [Finished]'
        -> 'finance appropriations'
    """
    if not text:
        return ""

    text = text.lower()

    # Remove common legislative prefixes
    prefixes_to_remove = [
        r"^senate of virginia:\s*",
        r"^virginia senate:\s*",
        r"^house of delegates:\s*",
        r"^virginia house:\s*",
        r"^commonwealth of virginia:\s*",
    ]
    for prefix in prefixes_to_remove:
        text = re.sub(prefix, "", text, flags=re.IGNORECASE)

    # Remove date suffixes like "on 2026-01-27" or "on January 27, 2026"
    text = re.sub(r"\s+on\s+\d{4}-\d{2}-\d{2}.*$", "", text)
    text = re.sub(
        r"\s+on\s+[a-z]+\s+\d{1,2},?\s+\d{4}.*$", "", text, flags=re.IGNORECASE
    )

    # Remove status tags like [Finished], [Live], [Ended]
    text = re.sub(r"\s*\[.*?\]\s*", " ", text)

    # Replace & with 'and'
    text = re.sub(r"&", " and ", text)

    # Remove punctuation (keep alphanumeric and spaces)
    text = re.sub(r"[^\w\s]", " ", text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Remove common noise words
    noise_words = {
        "committee",
        "subcommittee",
        "senate",
        "house",
        "of",
        "the",
        "virginia",
        "commonwealth",
        "joint",
        "special",
        "select",
        "standing",
    }
    tokens = [t for t in text.split() if t not in noise_words]

    return " ".join(tokens)


def fuzzy_match_confidence(calendar_name: str, youtube_title: str) -> float:
    """
    Calculate fuzzy match confidence between a calendar meeting name and YouTube title.

    Uses Jaccard similarity on normalized tokens.

    Args:
        calendar_name: Meeting name from calendar (e.g., "Finance and Appropriations")
        youtube_title: YouTube stream title (e.g., "Senate of Virginia: Finance &
                       Appropriations on 2026-01-27 [Finished]")

    Returns:
        float: Confidence score 0.0 to 1.0. Threshold of 0.3 is recommended for
               lenient matching.

    Example:
        >>> fuzzy_match_confidence("Finance and Appropriations",
        ...     "Senate of Virginia: Finance & Appropriations on 2026-01-27")
        1.0  # Perfect match after normalization
    """
    cal_normalized = normalize_for_matching(calendar_name)
    yt_normalized = normalize_for_matching(youtube_title)

    if not cal_normalized or not yt_normalized:
        return 0.0

    # Token-based Jaccard similarity
    cal_tokens = set(cal_normalized.split())
    yt_tokens = set(yt_normalized.split())

    if not cal_tokens or not yt_tokens:
        return 0.0

    overlap = len(cal_tokens & yt_tokens)
    union = len(cal_tokens | yt_tokens)

    return overlap / union if union > 0 else 0.0


def title_match_details(
    calendar_name: str,
    youtube_title: str,
    *,
    jaccard_threshold: float = 0.3,
    keyword_threshold: float = 0.6,
    fuzzy_threshold: float = 0.7,
) -> tuple[float, str | None]:
    """Match titles using exact, containment, keyword, Jaccard, and fuzzy signals.

    Every strategy is evaluated. The strongest accepted strategy is returned as
    ``(confidence, match_type)``. Requiring two shared keywords for non-exact
    matches prevents generic one-word overlaps such as "Council" from attaching
    the wrong video.
    """
    calendar = normalize_for_matching(calendar_name)
    youtube = normalize_for_matching(youtube_title)
    if not calendar or not youtube:
        return 0.0, None

    calendar_tokens = set(calendar.split())
    youtube_tokens = set(youtube.split())
    common = calendar_tokens & youtube_tokens

    if calendar == youtube:
        return 1.0, "exact"

    shorter, longer = sorted((calendar, youtube), key=len)
    if (
        shorter in longer
        and (len(shorter.split()) >= 2 or calendar_tokens == youtube_tokens)
    ):
        return 0.98, "containment"

    fuzzy = fuzz.token_set_ratio(calendar, youtube) / 100.0

    # A high fuzzy score can recover misspellings even when tokens differ.
    typo_threshold = 0.82 if not common else 0.92
    if (
        len(common) < 2
        and len(calendar_tokens) >= 2
        and len(youtube_tokens) >= 2
        and fuzzy >= max(fuzzy_threshold, typo_threshold)
    ):
        return fuzzy, "fuzzy"

    # Other non-exact strategies need at least two meaningful shared tokens.
    if len(common) < 2:
        return 0.0, None

    candidates: list[tuple[float, str]] = []
    union = calendar_tokens | youtube_tokens
    jaccard = len(common) / len(union) if union else 0.0
    if jaccard >= jaccard_threshold:
        candidates.append((jaccard, "token_jaccard"))

    keyword_coverage = len(common) / min(len(calendar_tokens), len(youtube_tokens))
    if keyword_coverage >= keyword_threshold:
        candidates.append((keyword_coverage, "keyword_intersection"))

    if fuzzy >= fuzzy_threshold:
        candidates.append((fuzzy, "fuzzy"))

    return max(candidates, default=(0.0, None), key=lambda candidate: candidate[0])


def find_best_match(
    meeting_title: str, live_videos: list, threshold: float = 0.3
) -> tuple:
    """
    Find the best matching live video for a meeting title.

    Args:
        meeting_title: The calendar meeting title to match
        live_videos: List of dicts with 'video_id' and 'video_title' keys
        threshold: Minimum confidence threshold (default 0.3 for lenient matching)

    Returns:
        tuple: (video_data, confidence, match_type) or (None, 0.0, None) if no match

    Match types:
        - 'exact': meeting_title found verbatim in video_title
        - 'fuzzy': fuzzy match above threshold
        - None: no match found
    """
    if not live_videos or not meeting_title:
        return None, 0.0, None

    best_match = None
    best_confidence = 0.0
    match_type = None

    for video_data in live_videos:
        video_title = video_data.get("video_title", "")

        confidence, candidate_type = title_match_details(
            meeting_title,
            video_title,
            jaccard_threshold=threshold,
        )
        if confidence > best_confidence:
            best_confidence = confidence
            best_match = video_data
            match_type = candidate_type

    if best_confidence >= threshold:
        return best_match, best_confidence, match_type

    return None, best_confidence, None


if os.getenv("ENV", "").lower() == "local":
    import ssl  # noqa: E402

    ssl._create_default_https_context = ssl._create_stdlib_context


class Youtube:

    def __init__(self, url, meeting_title, proxy_first=False):
        self.url = url
        self.live_videos_data = None
        self.meeting_title = meeting_title
        self.youtube_restart_ID = os.getenv("ARG_YOUTUBE_RESTART_ID")
        self.use_proxy = proxy_first
        self.proxies = self.configure_proxies(proxy_first)

    def configure_proxies(self, use_proxy):
        """Configures a proxy dictionary for pytubefix."""
        if use_proxy:
            https_proxy = os.environ.get("IPB_HTTPS")
            http_proxy = os.environ.get("IPB_HTTP")
            return {
                "http": http_proxy,
                "https": https_proxy,
            }
        return None

    def print_ip_address(self):
        """Fetches and prints the public IP address of the current session."""
        try:
            response = requests.get(
                "https://api.ipify.org?format=json",
                proxies=self.proxies,
                verify=True,
            )
            ip_address = response.json().get("ip")
            log.debug(f"Current public IP: {ip_address}")
        except Exception as e:
            log.warning(f"Failed to fetch IP address. Error: {e}")

    def is_valid_youtube_streams_url(self):
        """Checks if a YouTube channel URL is valid."""
        patterns = [
            r"https://www\.youtube\.com/@[a-zA-Z0-9]+/streams",
            r"https://www\.youtube\.com/[a-zA-Z0-9]+/streams",
            r"https://www\.youtube\.com/@[a-zA-Z0-9]",
            r"https://www\.youtube\.com/[a-zA-Z0-9]",
        ]
        return any([re.match(pattern, self.url) for pattern in patterns])

    def is_valid_youtube_archive_url(self):
        """Checks if a YouTube archive video URL is valid."""
        patterns = [
            r"https://www\.youtube\.com/watch\?v=[a-zA-Z0-9]+&ab_channel=[a-zA-Z0-9]",
            r"https://www\.youtube\.com/watch\?v=[a-zA-Z0-9]",
            r"https://youtu\.be/[a-zA-Z0-9]",
        ]
        return any([re.match(pattern, self.url) for pattern in patterns])

    def extract_channel_url(self):
        """Extracts the channel URL from various YouTube URL formats."""
        if "/live" in self.url:
            # Remove /live from the end
            return self.url.replace("/live", "")
        elif self.url.startswith("https://www.youtube.com/channel/"):
            # Already a channel URL
            return self.url
        elif "/watch?v=" in self.url:
            # Extract channel from watch URL (would need more complex parsing)
            # For now, return the URL as-is since we can't easily extract channel
            return self.url
        else:
            # Assume it's already a valid channel URL
            return self.url

    def get_channel_handle_url(self):
        """Converts channel ID URLs to @handle URLs using YouTube API."""
        channel_id = None

        # Extract channel ID from URL
        if "/channel/" in self.url:
            # URL format: https://www.youtube.com/channel/UCfJyhBZqT52JZCkoWmgtO8w
            channel_id = self.url.split("/channel/")[-1].split("/")[0]
        elif "/live" in self.url and "/channel/" in self.url:
            # URL format: https://www.youtube.com/channel/UCfJyhBZqT52JZCkoWmgtO8w/live
            channel_id = self.url.split("/channel/")[-1].split("/")[0]

        if not channel_id:
            # If no channel ID found, return the original URL
            return self.url

        try:
            # Use YouTube API to get channel information
            youtube_api_key = os.getenv("ARG_YOUTUBE_API_KEY")
            if not youtube_api_key:
                log.warning("YouTube API key not found, returning original URL")
                return self.url

            youtube = build(
                serviceName="youtube",
                version="v3",
                developerKey=youtube_api_key,
                cache_discovery=False,
            )

            # Get channel information
            request = youtube.channels().list(part="snippet", id=channel_id)
            response = request.execute()

            if response["items"]:
                channel_data = response["items"][0]
                custom_url = channel_data["snippet"].get("customUrl")

                if custom_url:
                    # Return @handle format URL
                    return f"https://www.youtube.com/@{custom_url}"

            # If no custom URL found, return original URL
            return self.url

        except Exception as e:
            log.warning(f"Error fetching channel handle for {channel_id}: {e}")
            return self.url

    def on_progress(self, stream, chunk, bytes_remaining, pbar):
        """
        This function is used to create the download progress bar using
        pytube and tqdm.
        """
        size = stream.filesize
        progress = size - bytes_remaining
        pbar.update(progress - pbar.n)  # Update tqdm bar by difference of progress

    def download_archive_audio(self, output_dir, file_name):
        """
        This method downloads and saves the audio files for YouTube archive.

        Params:
        -------
        output_dir: The directory to write the downloaded audio
        file_name: The audio file name

        Returns:
        --------
        response: (str) download failed or download success
        """
        try:
            # Print the IP address before downloading
            if not self.use_proxy:
                log.info("Attempting download without proxy:")
            else:
                log.info("Attempting download with proxy:")
            self.print_ip_address()

            yt = YT(self.url, proxies=self.proxies)
            audio_stream = yt.streams.get_audio_only()

            log.debug(f"Audio stream => {audio_stream}")
            log.info("Found audio stream")

            if not audio_stream:
                log.warning("No audio stream found")
                return "download failed"
            else:
                os.makedirs(output_dir, exist_ok=True)
                pbar = tqdm(
                    total=audio_stream.filesize,
                    unit="B",
                    unit_scale=True,
                    desc="Downloading",
                )

                # Register the progress function with the progress bar passed
                # as an argument
                yt.register_on_progress_callback(
                    lambda stream, chunk, bytes_remaining: self.on_progress(
                        stream, chunk, bytes_remaining, pbar
                    )
                )

                # Download the audio stream (AAC format)
                audio_stream.download(output_path=output_dir, filename=file_name)

                pbar.close()

                log.info(
                    f"Download completed! Audio file saved in the '{output_dir}/{file_name}' directory."
                )
                return "download success"
        except Exception as e:
            if not self.use_proxy:
                log.warning(f"First attempt failed, retrying with proxy. Error: {e}")

                # Enable proxy for retry
                self.use_proxy = True
                self.proxies = self.configure_proxies(True)

                # Print the IP address after switching to proxy
                log.info("Retrying download with proxy:")
                self.print_ip_address()

                return self.download_archive_audio(output_dir, file_name)
            else:
                log.warning(f"Second attempt failed. Error: {e}", exc_info=True)
                return "download failed"

    def get_live_videos(self, soup):
        """
        This function returns a list of live video objects
        or None when there are no live videos.

        Params:
        -------
        soup: The scraper object.

        Returns:
        -------
        live video object | None:
            Object sample:
                [
                    {
                        "video_id": video_id,
                        "video_title": title
                    }
                ]
        """
        self.live_videos_data = []
        script_tags = soup.find_all("script")
        pattern = re.compile(r"var ytInitialData = ")

        for script in script_tags:
            if script.string and pattern.search(script.string):
                start_pos = script.string.find("var ytInitialData = ") + len(
                    "var ytInitialData = "
                )
                json_str = script.string[start_pos:]

                # Find the end of the JSON object
                brace_count = 0
                for i, char in enumerate(json_str):
                    if char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                    if brace_count == 0 and i > 0:
                        json_str = json_str[: i + 1]
                        break

                try:
                    yt_initial_data = json.loads(json_str)
                    data = yt_initial_data["contents"][
                        "twoColumnBrowseResultsRenderer"
                    ]["tabs"]

                    for element_i in data:
                        if (
                            element_i[list(element_i.keys())[0]]["title"].lower()
                            == "live"
                        ):
                            log.info("Found LIVE tab")
                            live_tab = element_i
                            live_tab_content = live_tab["tabRenderer"]["content"][
                                "richGridRenderer"
                            ]["contents"]

                            for content in live_tab_content:
                                if "continuationItemRenderer" not in content.keys():
                                    try:
                                        # Defensive check: ensure we have the
                                        # expected structure
                                        if "richItemRenderer" not in content:
                                            log.warning(
                                                f"Skipping item - missing richItemRenderer. Keys found: {list(content.keys())}"
                                            )
                                            continue

                                        rich_content = content["richItemRenderer"].get(
                                            "content", {}
                                        )

                                        # Check for videoRenderer (standard
                                        # videos)
                                        if "videoRenderer" not in rich_content:
                                            # Log the actual renderer type
                                            # found for debugging
                                            renderer_types = list(rich_content.keys())
                                            log.warning(
                                                f"Skipping non-video content. Renderer type(s) found: {renderer_types}"
                                            )
                                            continue

                                        video_data = rich_content["videoRenderer"]

                                        # Safely check if video is live
                                        is_live = False
                                        if (
                                            "thumbnailOverlays" in video_data
                                            and len(video_data["thumbnailOverlays"]) > 0
                                        ):
                                            overlay = video_data["thumbnailOverlays"][0]
                                            if (
                                                "thumbnailOverlayTimeStatusRenderer"
                                                in overlay
                                            ):
                                                status_text = overlay[
                                                    "thumbnailOverlayTimeStatusRenderer"
                                                ].get("text", {})
                                                accessibility = status_text.get(
                                                    "accessibility", {}
                                                )
                                                accessibility_data = accessibility.get(
                                                    "accessibilityData", {}
                                                )
                                                label = accessibility_data.get(
                                                    "label", ""
                                                ).lower()
                                                is_live = label == "live"

                                    except Exception as e:
                                        log.warning(
                                            f"Error parsing video live status: {e}",
                                            exc_info=True,
                                        )
                                        continue

                                    if is_live:
                                        video_id = video_data["videoId"]
                                        title = video_data["title"]["runs"][0]["text"]
                                        video_object = {
                                            "video_id": video_id,
                                            "video_title": title,
                                        }
                                        log.debug(video_object)
                                        self.live_videos_data.append(video_object)

                            if self.live_videos_data.count == 0:
                                self.live_videos_data = None
                                log.info("No live Video Detected")

                except json.JSONDecodeError as e:
                    log.warning(f"Error parsing JSON: {e}", exc_info=True)

        return self.live_videos_data

    def match_meet(self, use_time_check=True):
        """
        Matches the meet using time or meet title.

        Returns:
        -------
        video_id: The id of the qualifying live stream or None.
        """

        if self.youtube_restart_ID:  # substitute known ID
            for video_data in self.live_videos_data:
                if self.youtube_restart_ID == video_data["video_id"]:
                    return self.youtube_restart_ID
            os.environ.pop("ARG_YOUTUBE_RESTART_ID")
            return "terminated"

        base = float(3600 * 3)
        stream_id = None
        time_diff = None
        current_time = datetime.now(pytz.utc)

        # Get LiveStream starttime
        Utube = build(
            serviceName="youtube",
            version="v3",
            developerKey=os.getenv("ARG_YOUTUBE_API_KEY"),
            cache_discovery=False,
        )

        for video_data in self.live_videos_data:
            request = Utube.videos().list(
                part="snippet,liveStreamingDetails", id=video_data["video_id"]
            )
            response = request.execute()

            # Add Stream Start Time
            if "liveStreamingDetails" in response["items"][0]:
                video_data["start_time"] = datetime.fromisoformat(
                    response["items"][0]["liveStreamingDetails"][
                        "actualStartTime"
                    ].replace("Z", "+00:00")
                )

            # Direct Name check
            if self.meeting_title in video_data["video_title"]:
                log.info(f"Detected meeting by name: {video_data['video_title']}")
                return video_data["video_id"]

            # Time Check
            if use_time_check:
                log.debug(
                    f"video_id => {video_data['video_id']}, start_time => {video_data['start_time']}"
                )
                time_diff = abs(
                    (current_time - video_data["start_time"]).total_seconds()
                )
                if time_diff < base:
                    base = time_diff
                    stream_id = video_data["video_id"]

        return stream_id
