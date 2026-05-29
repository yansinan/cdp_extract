"""CDP web extract provider — fetch HTML via Chrome CDP, extract via read_down (Node.js).

Phase 2: CDP fetch + Readability + Turndown pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List

import requests
import websockets

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
PAGE_TIMEOUT = 30
READ_DOWN_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "read_down", "index.js",
)


# ---------------------------------------------------------------------------
# read_down bridge
# ---------------------------------------------------------------------------


def _call_readdown(html: str, url: str = "", debug: bool = False) -> Dict[str, Any]:
    """Run read_down (Node.js) on raw HTML, return structured result.

    CLI interface:
      stdin  → {"html": "...", "url": "...", "options": {"debugTrace": bool}}
      stdout → {"markdown": "...", "text": "...", "title": "...", ...}
    """
    payload = {
        "html": html,
        "url": url,
        "options": {"debugTrace": debug},
    }

    try:
        proc = subprocess.run(
            ["node", READ_DOWN_INDEX],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {"text": "", "error": "node-not-found"}
    except subprocess.TimeoutExpired:
        return {"text": "", "error": "read-down-timeout"}
    except Exception as exc:
        return {"text": "", "error": f"read-down-exec-error: {exc}"}

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        return {"text": "", "error": f"read-down-exit-{proc.returncode}: {stderr[:200]}"}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"text": "", "error": f"read-down-json-error: {exc}"}


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------


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
    """Send a CDP command and wait for the matching response."""
    payload: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=PAGE_TIMEOUT)
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg
        logger.debug("CDP event (skipped): %s", msg.get("method", msg))


async def _fetch_raw_html(url: str) -> Dict[str, Any]:
    """Open page via CDP → load → scroll to bottom → grab HTML."""
    result: Dict[str, Any] = {"url": url, "html": "", "title": "", "error": None}
    browser_ws = _get_browser_ws_url()
    target_id, target_ws = await _create_target(browser_ws)
    msg_id = 1

    try:
        async with websockets.connect(target_ws, max_size=None) as ws:
            # Step 1: Navigate + wait for frame to load
            await _cdp_send(ws, "Page.enable", msg_id=msg_id)
            msg_id += 1

            nav_resp = await _cdp_send(ws, "Page.navigate", {"url": url}, msg_id=msg_id)
            msg_id += 1
            if "error" in nav_resp:
                raise RuntimeError(f"Page.navigate error: {nav_resp['error']}")

            # Wait for frame to finish loading by reading events
            # (Page.frameStoppedLoading is pushed, not command-based)
            for _ in range(30):  # max 30s
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw)
                if msg.get("method") == "Page.frameStoppedLoading":
                    break
                # Page.frameStartedLoading, Page.frameNavigated, etc. — skip
                logger.debug("CDP load event: %s", msg.get("method"))

            # Step 2: Scroll to bottom — 200ms intervals
            scroll_script = """
                (async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const total = document.body.scrollHeight;
                    const step = Math.max(Math.floor(total / 15), 80);
                    for (let y = 0; y <= total; y += step) {
                        window.scrollTo(0, y);
                        await delay(200);
                    }
                    // Step 3: Wait 3 seconds at bottom
                    await delay(3000);
                    return document.body.scrollHeight;
                })()
            """
            await ws.send(json.dumps({
                "id": msg_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": scroll_script,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }))
            msg_id += 1
            scroll_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if "error" in scroll_resp:
                logger.warning("Scroll eval error: %s", scroll_resp["error"])
            else:
                h = scroll_resp.get("result", {}).get("result", {}).get("value", "?")
                logger.info("CDP scroll done, height=%s", h)

            # Step 4: Grab HTML
            title_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.title"},
                msg_id,
            )
            msg_id += 1
            if "result" in title_resp and "result" in title_resp["result"]:
                result["title"] = title_resp["result"]["result"].get("value", "")

            html_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                msg_id,
            )
            if "error" in html_resp:
                raise RuntimeError(f"Runtime.evaluate error: {html_resp['error']}")
            result["html"] = html_resp["result"]["result"].get("value", "")

    except Exception as exc:
        logger.warning("CDP fetch failed for %s: %s", url, exc)
        result["error"] = str(exc)
    finally:
        _close_target(target_id)

    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CDPExtractProvider(WebSearchProvider):
    """CDP-based web content extractor — Chrome DevTools + Readability + Turndown."""

    @property
    def name(self) -> str:
        return "cdp-extract"

    @property
    def display_name(self) -> str:
        return "CDP Extract (Chrome DevTools + Readability + Turndown)"

    def is_available(self) -> bool:
        """Return True when local CDP port 9222 and Node.js are reachable."""
        try:
            resp = requests.get(f"{CDP_URL}/json/version", timeout=3)
            if resp.status_code != 200:
                return False
            subprocess.run(["node", "--version"], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch each URL via Chrome CDP, then extract via read_down.

        Returns:
            List of per-URL result dicts with markdown, text, title, etc.
        """
        logger.info("CDP extract: %d URL(s)", len(urls))
        results: List[Dict[str, Any]] = []

        for url in urls:
            logger.info("CDP extract: fetching %s", url)

            # Step 1: CDP → raw HTML
            raw = await _fetch_raw_html(url)

            if raw.get("error") or not raw.get("html"):
                results.append({
                    "url": url,
                    "text": "",
                    "markdown": None,
                    "error": raw.get("error", "empty-html"),
                })
                continue

            # Step 2: read_down → structured result
            rd_result = _call_readdown(
                html=raw["html"],
                url=url,
                debug=bool(kwargs.get("debug")),
            )

            # Merge: carry over CDP metadata, overwrite with read_down fields
            merged = {
                "url": url,
                "text": rd_result.get("text", ""),
                "markdown": rd_result.get("markdown"),
                "html": rd_result.get("html"),
                "title": rd_result.get("title") or raw.get("title"),
                "byline": rd_result.get("byline"),
                "dir": rd_result.get("dir"),
                "length": rd_result.get("length"),
                "lang": rd_result.get("lang"),
                "error": rd_result.get("error"),
            }
            # Strip None values for optional fields (matches PageExtractionResult)
            merged = {k: v for k, v in merged.items() if v is not None}
            results.append(merged)

        ok = sum(1 for r in results if r.get("error") is None)
        logger.info("CDP extract: %d/%d succeeded", ok, len(results))
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "CDP Extract (Chrome DevTools + Readability + Turndown)",
            "badge": "local · no key",
            "tag": "Full pipeline: Chrome DevTools (port 9222) → Readability → Turndown Markdown",
            "env_vars": [],
        }
