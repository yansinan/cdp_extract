"""CDP web extract provider — HTTP fetch + text extraction.

Basic implementation: fetches URLs via requests, extracts readable text
from HTML using Python's built-in HTMLParser. No external SDK required.

Extend later with proper Readability / trafilatura extraction.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import urlparse

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal HTML → text extractor (no external deps)
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Strips HTML tags, extracts visible text with paragraph spacing."""

    def __init__(self) -> None:
        super().__init__()
        self._text: List[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        tag_lower = tag.lower()
        if tag_lower in {"script", "style", "noscript"}:
            self._skip = True
        if tag_lower in {"p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._text.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._text).strip()


def _extract_text(html: str) -> str:
    """Extract readable text from raw HTML."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def _fetch_url(url: str, timeout: int = 15) -> Dict[str, Any]:
    """Fetch a single URL and return extracted content."""
    import requests

    result: Dict[str, Any] = {"url": url, "title": "", "content": "", "raw_content": ""}

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("CDP fetch failed for %s: %s", url, exc)
        result["error"] = str(exc)
        return result

    # Detect content type
    ct = resp.headers.get("content-type", "").lower()
    raw_text = resp.text
    result["raw_content"] = raw_text

    if "text/html" in ct or "text/plain" in ct:
        # Try to extract title from HTML
        title = ""
        content = ""
        if "text/html" in ct:
            import re

            m = re.search(r"<title[^>]*>(.*?)</title>", raw_text, re.IGNORECASE | re.DOTALL)
            if m:
                title = m.group(1).strip()
            content = _extract_text(raw_text)
        else:
            content = raw_text

        result["title"] = title
        result["content"] = content
    else:
        # Non-HTML — just note the content type
        result["title"] = url
        result["content"] = f"[Content-Type: {ct}] {raw_text[:200]}"

    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CDPExtractProvider(WebSearchProvider):
    """CDP-based web content extractor — fetch + text extraction via requests."""

    @property
    def name(self) -> str:
        return "cdp-extract"

    @property
    def display_name(self) -> str:
        return "CDP Extract (requests)"

    def is_available(self) -> bool:
        """Always available — requests is a Hermes core dependency."""
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch and extract content from one or more URLs.

        Args:
            urls: URLs to extract.
            kwargs: Optional ``timeout`` (int, seconds per URL).

        Returns:
            List of per-URL result dicts.
        """
        timeout = kwargs.get("timeout", 15)
        results: List[Dict[str, Any]] = []

        for url in urls:
            logger.info("CDP extract: fetching %s", url)
            result = _fetch_url(url, timeout=int(timeout))
            results.append(result)

        logger.info("CDP extract: %d URLs processed", len(results))
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "CDP Extract (requests)",
            "badge": "test · no key",
            "tag": "Basic HTTP fetch + HTML text extraction via requests — no API key needed",
            "env_vars": [],
        }
