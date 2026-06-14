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
import time
from typing import Any, Dict, List

import requests
import websockets

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
PAGE_TIMEOUT = 30  # 单次 CDP 命令超时
TUNNEL_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "scripts", "cdp_tunnel.sh",
)
READ_DOWN_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "read_down", "index.js",
)


# ---------------------------------------------------------------------------
# CDP 连接管理
# ---------------------------------------------------------------------------


def _load_cdp_config() -> dict:
    """从 config.yaml 读取 plugins.cdp_extract 配置。"""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg.get("plugins", {}).get("cdp_extract", {}) or {}
    except Exception:
        return {}


def _cdp_url_from_config() -> str:
    """返回实际的 CDP URL（可配置）。"""
    cfg = _load_cdp_config()
    return cfg.get("cdp_url", CDP_URL)


def _build_tunnel_env(cfg: dict) -> dict:
    """根据配置构造隧道脚本的环境变量。"""
    env = {}
    mapping = {
        "remote_host": "CDP_TUNNEL_REMOTE_HOST",
        "remote_user": "CDP_TUNNEL_REMOTE_USER",
        "ssh_key": "CDP_TUNNEL_SSH_KEY",
        "remote_port": "CDP_TUNNEL_REMOTE_PORT",
        "local_port": "CDP_TUNNEL_LOCAL_PORT",
        "remote_debug_port": "CDP_TUNNEL_REMOTE_DEBUG_PORT",
        "tunnel_tool": "CDP_TUNNEL_TOOL",
        "remote_chrome_bin": "CDP_TUNNEL_REMOTE_CHROME_BIN",
        "remote_chrome_profile": "CDP_TUNNEL_REMOTE_CHROME_PROFILE",
        "remote_chrome_args": "CDP_TUNNEL_REMOTE_CHROME_ARGS",
        "agent_browser_bin": "CDP_TUNNEL_AGENT_BROWSER_BIN",
        "hermes_py": "CDP_TUNNEL_HERMES_PY",
    }
    for key, var in mapping.items():
        val = cfg.get(key)
        if val is not None and val != "":
            env[var] = str(val)
    return env


def _check_local_cdp(cdp_url: str = CDP_URL) -> bool:
    """检查本地 CDP 端口是否可达。"""
    try:
        resp = requests.get(f"{cdp_url}/json/version", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _try_hermes_local_chrome() -> bool:
    """细粒度复用 Hermes 浏览器探测模块, 启动本地 Chromium-family 浏览器。

    设计:
      - 不调 Hermes 的 try_launch_chrome_debug() (黑盒, 写死 ~/.hermes/chrome-debug
        + 不加 --ozone-platform=wayland → 在 Sway/Wayland-only 桌面会失败
        + 同 user-data-dir 不能起两个实例, 单 instance lock 会让新 Chrome
        join 现有 session 然后退出 ("Opening in existing browser session."))
      - 用 config 指定的独立 user-data-dir (默认 ~/.hermes/cdp-chrome),
        跟你 PWA Chrome (~/.hermes/chrome-debug) 物理隔开
      - Wayland 自动检测 (Sway 没 DISPLAY 必须加, X11 桌面跳过)
      - 复用 Hermes 3 个原子: get_chrome_debug_candidates, DEFAULT_BROWSER_CDP_PORT,
        is_browser_debug_ready (在 _check_local_cdp)
      - 进程用 start_new_session=True 脱离父进程 (Hermes 的 fire-and-forget 策略)

    Returns True if a Chromium-family browser is running and serving CDP.
    """
    import platform
    try:
        from hermes_cli.browser_connect import (
            get_chrome_debug_candidates,
            DEFAULT_BROWSER_CDP_PORT,
        )
    except ImportError as exc:
        logger.warning("无法导入 hermes_cli.browser_connect: %s", exc)
        return False

    # --- user-data-dir: config 驱动, 默认 ~/.hermes/cdp-chrome ---
    # 重要: 跟 ~/.hermes/chrome-debug 物理隔开 (Chrome 单 instance lock 是
    # user-data-dir 级别的, 同 user-data-dir 第二个进程会被 lock 拦)
    cfg = _load_cdp_config()
    user_data_dir = cfg.get("local_chrome_profile") or os.path.expanduser(
        "~/.hermes/cdp-chrome"
    )
    os.makedirs(user_data_dir, exist_ok=True)
    logger.info("cdp-extract Chrome user-data-dir: %s", user_data_dir)

    # --- Wayland (Sway 没 DISPLAY 必须加, X11 桌面跳过) ---
    wayland_flag: list[str] = []
    if platform.system() == "Linux" and not os.environ.get("DISPLAY"):
        wayland_flag = ["--ozone-platform=wayland"]

    # --- 端口 (从 CDP_URL 推, 默认 9222) ---
    try:
        port = int(CDP_URL.rsplit(":", 1)[-1].rstrip("/") or DEFAULT_BROWSER_CDP_PORT)
    except ValueError:
        port = DEFAULT_BROWSER_CDP_PORT

    # --- 复用 Hermes 多浏览器探测 ---
    candidates = get_chrome_debug_candidates(platform.system())
    if not candidates:
        logger.warning("未找到任何 Chromium-family 浏览器")
        return False

    # --- 自己包装 Popen (start_new_session=True, 跟 Hermes 策略一致) ---
    for candidate in candidates:
        try:
            subprocess.Popen(
                [candidate,
                 f"--remote-debugging-port={port}",
                 f"--user-data-dir={user_data_dir}",
                 "--no-first-run",
                 "--no-default-browser-check",
                 *wayland_flag,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # 等 CDP 起来 (Hermes 文档说 5s 内, 我们给 10s; 在 Sway 桌面 Chrome
            # 有时 startup 较慢 + 偶发 segfault 重启需要时间)
            for i in range(20):
                if _check_local_cdp(CDP_URL):
                    logger.info("cdp-extract Chrome CDP 已就绪 (尝试 %d 次)", i + 1)
                    return True
                time.sleep(0.5)
            logger.warning("启动 Chrome 10s 后 CDP 仍未就绪")
            return False
        except Exception as exc:
            logger.debug("尝试 %s 失败: %s", candidate, exc)
            continue
    return False


def _ensure_cdp() -> bool:
    """确保 CDP 可用。

    决策链:
      ① 本地 CDP (port 9222) 可达 → 直接用
      ② 调 Hermes 内置 try_launch_chrome_debug 启动本地 Chrome
      ③ 远端隧道 (仅当 remote_host 非空, 兼容旧配置)
    """
    global CDP_URL
    CDP_URL = _cdp_url_from_config()

    if _check_local_cdp(CDP_URL):
        return True

    logger.info("本地 CDP 不可用, 尝试 Hermes 自动启动本地 Chrome")
    if _try_hermes_local_chrome() and _check_local_cdp(CDP_URL):
        return True

    cfg = _load_cdp_config()
    remote_host = (cfg.get("remote_host") or "").strip()
    if not remote_host:
        logger.warning("本地 Chrome 启动失败且未配置 remote_host, CDP 不可用")
        return False

    logger.info("本地 Chrome 启动失败, 回落隧道 %s@%s",
                cfg.get("remote_user"), remote_host)

    if not os.path.isfile(TUNNEL_SCRIPT):
        logger.warning("隧道脚本不存在: %s", TUNNEL_SCRIPT)
        return False

    env = os.environ.copy()
    env.update(_build_tunnel_env(cfg))

    try:
        proc = subprocess.run(
            [TUNNEL_SCRIPT, "start"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        logger.info("隧道脚本返回: %s", proc.stdout.strip()[:200])
        if proc.returncode != 0:
            logger.warning("隧道启动失败: %s", proc.stderr.strip()[:200])
            return False
    except Exception as exc:
        logger.warning("隧道调用失败: %s", exc)
        return False

    return _check_local_cdp(CDP_URL)


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
    """单命令 JS 自控滚动到底 — Python 真正 await 执行完成。

    使用 Runtime.evaluate + awaitPromise: True。
    JS 内部用 setInterval(50ms) 逐屏下滚，到底后 setTimeout(3s) 等懒加载，
    全部完成后 return 最终 scrollHeight。

    注意：awaitPromise 能处理 macrotask（setTimeout），但使用前必须确认
    websocket 中没有残留的导航事件（否则 _cdp_send 需要跳过它们）。
    这里所有 command 都用 _cdp_send（按 msg_id 过滤），不受残留事件影响。

    文档:
      - Runtime.evaluate: https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate
      - awaitPromise: https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate
    """
    scroll_js = """
        (async () => {
            const el = document.scrollingElement;
            const step = 80;
            let pos = 0;
            let bottomCount = 0;

            await new Promise((resolve) => {
                const iv = setInterval(() => {
                    pos += step;
                    window.scrollTo(0, pos);

                    if (window.scrollY + window.innerHeight >= el.scrollHeight) {
                        bottomCount++;
                        if (bottomCount >= 2) {
                            clearInterval(iv);
                            // 到底了，等 3 秒让懒加载渲染
                            setTimeout(() => {
                                const h = el.scrollHeight;
                                resolve(h);
                            }, 3000);
                        }
                    }
                }, 50);
            });

            // 返回最终高度
            return el.scrollHeight;
        })()
    """

    resp = await _cdp_send(ws, "Runtime.evaluate", {
        "expression": scroll_js,
        "awaitPromise": True,
        "returnByValue": True,
    }, msg_id=msg_id)

    if "error" in resp:
        logger.warning("滚动执行失败: %s", resp["error"])
        return None
    if "exceptionDetails" in resp:
        logger.warning("滚动异常: %s", resp["exceptionDetails"])
        return None

    h = resp.get("result", {}).get("result", {}).get("value")
    if h is not None:
        logger.info("滚动完成: 高度=%s", int(h))
        return int(h)
    return None


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
        """检查本地 CDP (port 9222) 和 Node.js 是否可用。
        
        本地不可用时，如果配置了 remote_host，自动尝试远程隧道。
        """
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        except Exception:
            return False
        return _ensure_cdp()

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """对每个 URL: CDP 抓取 → read_down 提取 → 返回结构化结果"""
        logger.info("CDP extract: %d URL(s)", len(urls))
        results: List[Dict[str, Any]] = []

        # 确保 CDP 可用（本地或隧道）
        if not _ensure_cdp():
            logger.warning("CDP 不可用，无法提取")
            for url in urls:
                results.append({
                    "url": url, "text": "", "markdown": None,
                    "error": "CDP 不可用（本地 9222 或远程隧道均不可达）",
                })
            return results

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
