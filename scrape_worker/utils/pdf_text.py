"""PDF text extraction helpers using pdfplumber (with PyMuPDF fallback if installed)."""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)


def extract_pdf_text_from_bytes(content: bytes, max_pages: Optional[int] = None) -> str:
    """Extract text from PDF bytes.

    Args:
        content: PDF file bytes.
        max_pages: Optional max page count to extract.

    Returns:
        Extracted text.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(stream=content, filetype="pdf") as doc:
            page_count = len(doc)
            if max_pages is None or max_pages <= 0:
                max_pages = page_count
            for page_index in range(min(max_pages, page_count)):
                page = doc.load_page(page_index)
                page_text = page.get_text("text")
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts)
    except ImportError:
        log.debug("PyMuPDF not installed, using pdfplumber")
    except Exception as exc:
        log.warning("PyMuPDF extraction failed: %s, falling back to pdfplumber", exc)

    try:
        import pdfplumber

        text_parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = pdf.pages
            if max_pages is None or max_pages <= 0:
                max_pages = len(pages)
            for page in pages[:max_pages]:
                text = page.extract_text() or ""
                if text:
                    text_parts.append(text)
        return "\n\n".join(text_parts)
    except Exception as exc:
        raise ValueError(f"Failed to extract text from PDF: {exc}") from exc
