"""CDP web extract plugin — user plugin for testing web_extract provider override.

Minimal implementation that fetches URLs via requests and extracts readable text.
Uses html.parser / lxml for content extraction — no external SDK required.
"""

from __future__ import annotations

from plugins.web.cdp_extract.provider import CDPExtractProvider


def register(ctx) -> None:
    """Register the CDP extract provider with the plugin context."""
    ctx.register_web_search_provider(CDPExtractProvider())
