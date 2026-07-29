import logging
import requests

from typing import Optional
from pydantic import BaseModel
from fake_useragent import UserAgent

from utils.pdf_text import extract_pdf_text_from_bytes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class RequestParams(BaseModel):
    """Parameters for scanning PDF by link"""

    link: str
    headers: Optional[dict] = None
    cookies: Optional[dict] = None
    use_fake_user_agent: Optional[bool] = False
    extract_from_pages: Optional[int] = 1
    verify: Optional[bool] = True


class PDFScanner:
    def generate_headers(self, use_fake_user_agent: bool) -> dict:
        """
        This method generates HTTP headers, optionally using a fake user-agent.
        It generates headers with a fake user-agent only when the use_fake_user_agent flag is set to True.

        Params:
        ______
        :param use_fake_user_agent: Flag to use fake user agent for headers
        :return: A dictionary with HTTP header.
        """
        headers = {
            "User-Agent": (
                UserAgent().random
                if use_fake_user_agent
                else "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "gzip,deflate,br,zstd",
            "Accept-Language": "en-US,en;q=0.9",
        }

        return headers

    def generate_cookies(self, link: str, headers: dict, verify: bool = True) -> dict:
        """
        Generate cookies

        Params:
        ______
        :param link: Link to the site
        :param headers: Headers to be used in session request
        :param verify: Flag to verify SSL certificate
        :return: A dictionary with cookies
        """
        session = requests.Session()
        session.get(link, headers=headers, verify=verify)
        cookies = session.cookies.get_dict()

        return cookies

    def scan_pdf_by_link(self, params: RequestParams) -> str:
        """
        Fetch PDF content from the given url.

        Params:
        ______
        :param params: Object encapsulating parameters for scanning
        :return: Extracted text from PDF file
        """
        try:
            headers = params.headers or self.generate_headers(
                params.use_fake_user_agent
            )
            cookies = params.cookies or self.generate_cookies(
                params.link, params.headers, params.verify
            )

            pdf_content = self.fetch_pdf_content_in_bytes(
                params.link, headers, cookies, verify=params.verify
            )
            text = self._extract_pdf_text_from_bytes(
                pdf_content, extract_from_pages=params.extract_from_pages
            )

            return text
        except Exception as e:
            log.warning(f"Failed to fetch PDF from {params.link}: {e}")

    def fetch_pdf_content_in_bytes(
        self, link: str, headers: dict, cookies: dict, verify: bool = True
    ) -> bytes:
        """
        Receive PDF content from given link and return it as bytes

        Params:
        _______
        :param link: Link to the pdf file
        :param headers: (optional) Dictionary of HTTP to send with the :class:`Request`.
        :param cookies: (optional) Dict or CookieJar object to send with the :class:`Request`
        :param verify: (optional)  Flag to verify SSL certificate in the :class:`Request
        :return: PDF content in bytes
        """
        response = requests.get(link, headers=headers, cookies=cookies, verify=verify)
        response.raise_for_status()
        return response.content

    @staticmethod
    def _extract_pdf_text_from_bytes(
        pdf_content: bytes, extract_from_pages: int
    ) -> str:
        """
        Extract text from pdf content in bytes.

        Params:
        ______
        :param pdf_content: PDF content as bytes.
        :param extract_from_pages: Number of pages to extract text from.
        :return: Extracted text from PDF content.
        """
        try:
            return extract_pdf_text_from_bytes(
                pdf_content, max_pages=extract_from_pages
            )
        except Exception as e:
            log.warning(f"Failed processing pdf file: {e}")
