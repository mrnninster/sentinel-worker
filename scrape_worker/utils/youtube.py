import os
import re
import pytz
from tqdm import tqdm
from datetime import datetime
from pytubefix import YouTube as YT
from googleapiclient.discovery import build
import requests
from dotenv import load_dotenv

from logging_config import get_dedicated_debug_logger, LOG_LEVEL

log = get_dedicated_debug_logger(__name__)
log.setLevel(LOG_LEVEL)

load_dotenv()


# =============================================================================
# Fuzzy Matching Utilities — compatibility facade over youtube_core.matching
# =============================================================================

from youtube_core.matching import (  # noqa: E402
    find_best_match,
    fuzzy_match_confidence,
    normalize_for_matching,
    title_match_details,
)

# Re-export for legacy `from utils.youtube import …` callers.
__all_matching__ = (
    "normalize_for_matching",
    "fuzzy_match_confidence",
    "title_match_details",
    "find_best_match",
)


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

    def get_live_videos(self, soup=None, channel_url: str | None = None):
        """
        Live videos on the channel Live tab (metadata only).

        Prefers ``channel_url`` (Playwright + modern lockup UI). When only
        ``soup`` is provided (legacy calendar parsers), parses ytInitialData
        via ``youtube_core`` so lockupViewModel cards are not skipped.
        """
        from youtube_core.service import YouTubeService

        service = YouTubeService()
        url = channel_url or (self.url if self.is_valid_youtube_streams_url() else None)
        live = service.get_live_videos(channel_url=url, soup=soup)
        self.live_videos_data = live or None
        if not live:
            log.info("No live Video Detected")
            return None
        return live

    def match_meet(self, use_time_check=True):
        """
        Matches the meet using restart video id, title, or start-time proximity.

        Returns:
        -------
        video_id: The id of the qualifying live stream or None / "terminated".
        """
        if not self.live_videos_data:
            return None

        if self.youtube_restart_ID:  # substitute known ID (monitor restart continuity)
            for video_data in self.live_videos_data:
                if self.youtube_restart_ID == video_data["video_id"]:
                    return self.youtube_restart_ID
            os.environ.pop("ARG_YOUTUBE_RESTART_ID", None)
            return "terminated"

        base = float(3600 * 3)
        stream_id = None
        current_time = datetime.now(pytz.utc)

        # Prefer modern title ranking before the Data API time check.
        best, confidence, match_type = find_best_match(
            self.meeting_title, self.live_videos_data, threshold=0.3
        )
        if best and match_type:
            log.info(
                "Detected meeting by title (%s, %.2f): %s",
                match_type,
                confidence,
                best.get("video_title"),
            )
            return best["video_id"]

        if self.meeting_title:
            for video_data in self.live_videos_data:
                if self.meeting_title in video_data.get("video_title", ""):
                    log.info(
                        "Detected meeting by name: %s", video_data["video_title"]
                    )
                    return video_data["video_id"]

        if not use_time_check:
            return None

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
            if not response.get("items"):
                continue
            if "liveStreamingDetails" in response["items"][0]:
                video_data["start_time"] = datetime.fromisoformat(
                    response["items"][0]["liveStreamingDetails"][
                        "actualStartTime"
                    ].replace("Z", "+00:00")
                )

            if "start_time" not in video_data:
                continue
            log.debug(
                "video_id => %s, start_time => %s",
                video_data["video_id"],
                video_data["start_time"],
            )
            time_diff = abs((current_time - video_data["start_time"]).total_seconds())
            if time_diff < base:
                base = time_diff
                stream_id = video_data["video_id"]

        return stream_id
