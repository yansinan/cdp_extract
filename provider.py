"""
CDP web extract provider — fetch HTML via Chrome CDP, extract via read_down (Node.js).

文档参考:
  - CDP Page domain: https://chromedevtools.github.io/devtools-protocol/tot/Page/
  - CDP Runtime domain: https://chromedevtools.github.io/devtools-protocol/tot/Runtime/
  - MDN scrollingElement: https://developer.mozilla.org/en-US/docs/Web/API/Document/scrollingElement
  - MDN scrollTo: https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollTo
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
PAGE_TIMEOUT = 30  # 单次 CDP 命令超时
READ_DOWN_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "read_down", "index.js",
)


# ---------------------------------------------------------------------------
# read_down 桥接层
# ---------------------------------------------------------------------------


def _call_readdown(html: str, url: str = "", debug: bool = False) -> Dict[str, Any]:
    """调用 read_down (Node.js) 处理原始 HTML，返回结构化结果。

    CLI 接口:
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
# CDP 底层辅助
# ---------------------------------------------------------------------------


def _get_browser_ws_url() -> str:
    """获取浏览器级 WebSocket URL。

    文档: https://chromedevtools.github.io/devtools-protocol/tot/#endpoints
    GET /json/version → webSocketDebuggerUrl (ws://.../devtools/browser/<id>)
    """
    resp = requests.get(f"{CDP_URL}/json/version", timeout=5)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


async def _create_target(browser_ws: str) -> tuple[str, str]:
    """创建新标签页（页面目标）。

    文档: https://chromedevtools.github.io/devtools-protocol/tot/Target/#method-createTarget
    通过浏览器 WS 发送 Target.createTarget，返回 targetId。
    然后从 GET /json 列表中找到对应的 page WebSocket URL。
    """
    async with websockets.connect(browser_ws, max_size=None) as ws:
        msg = {"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}
        await ws.send(json.dumps(msg))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        result = json.loads(resp)
        if "error" in result:
            raise RuntimeError(f"CDP Target.createTarget error: {result['error']}")
        target_id = result["result"]["targetId"]

    # 从 REST 接口获取对应 page 的 WS URL
    targets_resp = requests.get(f"{CDP_URL}/json", timeout=5)
    for t in targets_resp.json():
        if t["id"] == target_id:
            return target_id, t["webSocketDebuggerUrl"]

    raise RuntimeError(f"Target {target_id} not found in /json list")


def _close_target(target_id: str) -> None:
    """关闭标签页。

    文档: https://chromedevtools.github.io/devtools-protocol/tot/#endpoints
    GET /json/close/<targetId>
    """
    try:
        requests.get(f"{CDP_URL}/json/close/{target_id}", timeout=5)
    except Exception as exc:
        logger.warning("关闭标签页失败 %s: %s", target_id, exc)


async def _cdp_send(ws, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    """发送 CDP 命令并等待匹配的响应。

    CDP 协议中，事件（有 method 无 id）和响应（id 匹配）交错到达。
    该函数忽略事件，只返回与 msg_id 匹配的响应。

    文档: https://chromedevtools.github.io/devtools-protocol/tot/#protocol
    """
    payload: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send(json.dumps(payload))

    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=PAGE_TIMEOUT)
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg
        logger.debug("CDP 事件（跳过）: %s", msg.get("method", msg))


async def _scroll_to_bottom(ws, msg_id: int) -> int | None:
    """通过 Runtime.evaluate 分步滚动到底部。

    不使用 awaitPromise+IIFE 的单命令方式，而是 Python 循环控制：
    每步一个 Runtime.evaluate（window.scrollTo），中间 asyncio.sleep 控制间隔。
    这样每一步的 CDP 响应都能独立确认，不会被 Promise reject 吞掉。

    文档:
      - Runtime.evaluate: https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate
      - window.scrollTo: https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollTo
      - Document.scrollingElement: https://developer.mozilla.org/en-US/docs/Web/API/Document/scrollingElement
    """
    # 第一步：获取页面实际可滚动高度
    # 使用 document.scrollingElement.scrollHeight 而非 document.body.scrollHeight
    # 因为现代网页（标准模式）的滚动容器是 <html> 而非 <body>。
    # 参考: https://developer.mozilla.org/en-US/docs/Web/API/Document/scrollingElement
    get_height_js = "document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight"
    resp = await _cdp_send(ws, "Runtime.evaluate",
                           {"expression": get_height_js, "returnByValue": True},
                           msg_id=msg_id)
    msg_id += 1
    if "error" in resp:
        logger.warning("获取页面高度失败: %s", resp["error"])
        return None

    total = resp.get("result", {}).get("result", {}).get("value", 0)
    if not isinstance(total, (int, float)) or total <= 0:
        logger.warning("无效页面高度: %s", total)
        return None

    total = int(total)
    steps = 15
    step = max(total // steps, 80)
    logger.info("开始滚动: 总高度=%d, 步长=%d", total, step)

    # 分步滚动，每步 Python 层面等待 200ms
    for y in range(0, total + 1, step):
        scroll_js = f"window.scrollTo(0, {y})"
        resp = await _cdp_send(ws, "Runtime.evaluate",
                               {"expression": scroll_js},
                               msg_id=msg_id)
        msg_id += 1
        if "error" in resp:
            logger.warning("滚动到 %d 失败: %s", y, resp["error"])
        await asyncio.sleep(0.2)

    # 到底后等待 3 秒，触发懒加载
    logger.info("已滚到底部，等待 3 秒让懒加载内容渲染...")
    await asyncio.sleep(3)

    # 返回最终高度
    resp = await _cdp_send(ws, "Runtime.evaluate",
                           {"expression": get_height_js, "returnByValue": True},
                           msg_id=msg_id)
    msg_id += 1
    if "error" not in resp:
        h = resp.get("result", {}).get("result", {}).get("value")
        logger.info("滚动完成，最终高度=%s", h)
        return h
    return total


async def _fetch_raw_html(url: str) -> Dict[str, Any]:
    """打开页面 → 等完全加载 → 滚动到底 → 抓取 HTML。

    流程（每步标注文档来源）:
      1. Page.enable — 开启页面域事件通知
         https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-enable
      2. Page.setLifecycleEventsEnabled({enabled: true}) — 开启生命周期事件
         https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-setLifecycleEventsEnabled
      3. Page.navigate(url) — 导航到目标 URL
         https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-navigate
      4. 等待 Page.lifecycleEvent(name='load') — 页面所有资源加载完毕
         https://chromedevtools.github.io/devtools-protocol/tot/Page/#event-lifecycleEvent
         name='load' 对应 window.onload，确保图片/脚本全部完成
      5. Runtime.evaluate — 执行滚动脚本（document.scrollingElement + window.scrollTo）
      6. Runtime.evaluate — 取 document.title
      7. Runtime.evaluate — 取 document.documentElement.outerHTML
    """
    result: Dict[str, Any] = {"url": url, "html": "", "title": "", "error": None}
    browser_ws = _get_browser_ws_url()
    target_id, target_ws = await _create_target(browser_ws)
    msg_id = 1

    try:
        async with websockets.connect(target_ws, max_size=None) as ws:
            # ---- 步骤 1: 开启 Page 域事件 ----
            await _cdp_send(ws, "Page.enable", msg_id=msg_id)
            msg_id += 1

            # ---- 步骤 2: 开启生命周期事件（必须有才能收到 load 事件） ----
            await _cdp_send(
                ws, "Page.setLifecycleEventsEnabled",
                {"enabled": True},
                msg_id=msg_id,
            )
            msg_id += 1

            # ---- 步骤 3: 导航到目标 URL ----
            await ws.send(json.dumps({
                "id": msg_id,
                "method": "Page.navigate",
                "params": {"url": url},
            }))

            # ---- 步骤 4: 等待导航响应 + lifecycle load 事件 ----
            # 不使用 _cdp_send，因为需要同时接收事件和响应
            navigate_ok = False
            load_ok = False

            while not (navigate_ok and load_ok):
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                mid = msg.get("id")
                method = msg.get("method")

                if mid == msg_id:
                    # Page.navigate 的响应
                    navigate_ok = True
                    if "error" in msg:
                        raise RuntimeError(f"导航失败: {msg['error']}")
                    logger.debug("导航响应 OK")
                elif method == "Page.lifecycleEvent":
                    # 参考: https://chromedevtools.github.io/devtools-protocol/tot/Page/#event-lifecycleEvent
                    evt_name = msg.get("params", {}).get("name", "")
                    if evt_name == "load":
                        load_ok = True
                        logger.debug("页面加载完成 (lifecycle load)")
                    elif evt_name == "DOMContentLoaded":
                        logger.debug("DOM 解析完成")
                else:
                    logger.debug("导航中事件: %s", method or mid)

            msg_id += 1  # 已消耗 navigate 的 id

            # ---- 步骤 5: 滚动到底部，触发懒加载 ----
            await _scroll_to_bottom(ws, msg_id)
            msg_id += 1

            # ---- 步骤 6: 取页面标题 ----
            title_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.title"},
                msg_id,
            )
            msg_id += 1
            if "result" in title_resp and "result" in title_resp["result"]:
                result["title"] = title_resp["result"]["result"].get("value", "")

            # ---- 步骤 7: 取完整 HTML ----
            html_resp = await _cdp_send(
                ws, "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                msg_id,
            )
            if "error" in html_resp:
                raise RuntimeError(f"取 HTML 失败: {html_resp['error']}")
            result["html"] = html_resp["result"]["result"].get("value", "")

    except Exception as exc:
        logger.warning("CDP 抓取失败 %s: %s", url, exc)
        result["error"] = str(exc)
    finally:
        _close_target(target_id)

    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CDPExtractProvider(WebSearchProvider):
    """CDP 网页内容提取器 — Chrome DevTools → Readability → Turndown Markdown"""

    @property
    def name(self) -> str:
        return "cdp-extract"

    @property
    def display_name(self) -> str:
        return "CDP Extract (Chrome DevTools + Readability + Turndown)"

    def is_available(self) -> bool:
        """检查本地 CDP (port 9222) 和 Node.js 是否可用"""
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
        """对每个 URL: CDP 抓取 → read_down 提取 → 返回结构化结果"""
        logger.info("CDP extract: %d URL(s)", len(urls))
        results: List[Dict[str, Any]] = []

        for url in urls:
            logger.info("CDP extract: fetching %s", url)

            # 步骤 1: CDP → 原始 HTML
            raw = await _fetch_raw_html(url)

            if raw.get("error") or not raw.get("html"):
                results.append({
                    "url": url,
                    "text": "",
                    "markdown": None,
                    "error": raw.get("error", "empty-html"),
                })
                continue

            # 步骤 2: read_down → 结构化结果（Markdown + 元数据）
            rd_result = _call_readdown(
                html=raw["html"],
                url=url,
                debug=bool(kwargs.get("debug")),
            )

            # 合并：CDP 标题作 fallback，read_down 字段优先
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
            # 去掉 None 值（匹配 PageExtractionResult 可选字段语义）
            merged = {k: v for k, v in merged.items() if v is not None}
            results.append(merged)

        ok = sum(1 for r in results if r.get("error") is None)
        logger.info("CDP extract: %d/%d 成功", ok, len(results))
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "CDP Extract (Chrome DevTools + Readability + Turndown)",
            "badge": "local · no key",
            "tag": "Chrome DevTools (port 9222) → Readability → Turndown Markdown",
            "env_vars": [],
        }
