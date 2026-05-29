"""CDP web extract provider — Phase 1: fetch full HTML via Chrome CDP on port 9222.

Connects to local Chrome DevTools Protocol, opens a tab, navigates to URL,
waits for page load, returns full HTML text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import requests
import websockets

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
PAGE_TIMEOUT = 30


def _get_browser_ws_url() -> str:
    """Get the browser-level WebSocket debugger URL from CDP."""
    resp = requests.get(f"{CDP_URL}/json/version", timeout=5)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


async def _create_target(browser_ws: str) -> tuple[str, str]:
    """Create a new page target, return (target_id, target_ws_url)."""
    async with websockets.connect(browser_ws, max_size=None) as ws:
        msg = {"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}
        await ws.send(json.dumps(msg))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        result = json.loads(resp)
        if "error" in result:
            raise RuntimeError(f"CDP Target.createTarget error: {result['error']}")
        target_id = result["result"]["targetId"]

    # Get target's websocket URL from REST endpoint
    targets_resp = requests.get(f"{CDP_URL}/json", timeout=5)
    for t in targets_resp.json():
        if t["id"] == target_id:
            return target_id, t["webSocketDebuggerUrl"]

    raise RuntimeError(f"Target {target_id} not found in /json list")


def _close_target(target_id: str) -> None:
    """Close a CDP target."""
    try:
        requests.get(f"{CDP_URL}/json/close/{target_id}", timeout=5)
    except Exception as exc:
        logger.warning("Failed to close target %s: %s", target_id, exc)


async def _cdp_send(ws, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    """Send a CDP command and wait for the matching response.

    CDP sends events (no ``id`` field) interleaved with responses.
    We must skip events and only return the response whose ``id`` matches.
    """
    payload: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=PAGE_TIMEOUT)
        msg = json.loads(raw)
        # Events have "method" but no "id"; responses have matching "id"
        if msg.get("id") == msg_id:
            return msg
        # Otherwise it's an event — log and skip
        logger.debug("CDP event (skipped): %s", msg.get("method", msg))


async def _fetch_single_via_cdp(url: str) -> Dict[str, Any]:
    """Open page via CDP, wait for load, return full HTML."""
    result: Dict[str, Any] = {"url": url, "title": "", "content": "", "raw_content": ""}
    browser_ws = _get_browser_ws_url()
    target_id, target_ws = await _create_target(browser_ws)
    msg_id = 1

    try:
        async with websockets.connect(target_ws, max_size=None) as ws:
            # Enable Page events
            await _cdp_send(ws, "Page.enable", msg_id=msg_id)
            msg_id += 1

            # Navigate — page loads automatically; CDP sends events
            # (frameStartedNavigating, frameStoppedLoading) in between
            # commands. No explicit wait needed — Runtime.evaluate below
            # runs after the page settles.
            nav_resp = await _cdp_send(ws, "Page.navigate", {"url": url}, msg_id=msg_id)
            msg_id += 1
            if "error" in nav_resp:
                raise RuntimeError(f"Page.navigate error: {nav_resp['error']}")

            # Get page title
            title_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.title"},
                msg_id,
            )
            msg_id += 1
            title = ""
            if "result" in title_resp and "result" in title_resp["result"]:
                title = title_resp["result"]["result"].get("value", "")

            # Get full HTML
            html_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                msg_id,
            )
            if "error" in html_resp:
                raise RuntimeError(f"Runtime.evaluate error: {html_resp['error']}")

            html = html_resp["result"]["result"].get("value", "")
            result["title"] = title
            result["content"] = html
            result["raw_content"] = html

    except Exception as exc:
        logger.warning("CDP fetch failed for %s: %s", url, exc)
        result["error"] = str(exc)
    finally:
        _close_target(target_id)

    return result


class CDPExtractProvider(WebSearchProvider):
    """CDP-based web content extractor — Phase 1: full HTML via Chrome DevTools."""

    @property
    def name(self) -> str:
        return "cdp-extract"

    @property
    def display_name(self) -> str:
        return "CDP Extract (Chrome DevTools)"

    def is_available(self) -> bool:
        """Return True when local CDP port 9222 is reachable."""
        try:
            resp = requests.get(f"{CDP_URL}/json/version", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch full HTML of each URL via Chrome CDP.

        Args:
            urls: URLs to fetch.
            kwargs: Unused (kept for forward compat).

        Returns:
            List of per-URL result dicts with full HTML in ``content``.
        """
        logger.info("CDP extract: fetching %d URL(s) via Chrome DevTools", len(urls))
        results: List[Dict[str, Any]] = []

        for url in urls:
            logger.info("CDP extract: navigating to %s", url)
            result = await _fetch_single_via_cdp(url)
            results.append(result)

        logger.info(
            "CDP extract: %d URLs processed, %d succeeded",
            len(results),
            sum(1 for r in results if "error" not in r),
        )
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "CDP Extract (Chrome DevTools)",
            "badge": "local · no key",
            "tag": "Full HTML extraction via local Chrome DevTools (port 9222) — no API key needed",
            "env_vars": [],
        }
