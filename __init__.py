"""CDP web extract plugin — Hermes Agent 的 web_extract provider。

通过 Chrome DevTools Protocol (CDP) 打开网页、滚动到底触发懒加载，
获取完整 HTML，再经 Readability + Turndown 管道输出结构化 Markdown。

输出接口对齐 hermes-sidebar 的 PageExtractionResult（外加 content/raw_content
字段别名以兼容 web_extract_tool 的字段读取约定）。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from .provider import CDPExtractProvider, TUNNEL_SCRIPT, _ensure_cdp

logger = logging.getLogger(__name__)


def _run_tunnel(action: str) -> str:
    """运行隧道脚本并返回输出。"""
    try:
        proc = subprocess.run(
            [TUNNEL_SCRIPT, action],
            capture_output=True, text=True, timeout=30,
        )
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        result = out or ""
        if err:
            result += f"\n{err}" if result else err
        if proc.returncode != 0:
            result = f"退出码 {proc.returncode}:\n{result}"
        return result or "(无输出)"
    except Exception as exc:
        return f"错误: {exc}"


def _handle_cdp_tunnel(raw_args: str) -> str:
    """处理 /cdp_tunnel 斜杠命令。

    用法: /cdp_tunnel [status|start|stop|restart]
    """
    action = raw_args.strip().lower() or "status"
    if action not in ("status", "start", "stop", "restart"):
        return (
            "用法: `/cdp_tunnel status|start|stop|restart`\n"
            f"不支持的参数: {action}\n"
            "当前状态:\n" + _run_tunnel("status")
        )
    result = _run_tunnel(action)
    return f"cdp_tunnel {action}:\n{result}"


def _install_white_list_override() -> None:
    """Monkey-patch tools.web_tools 白名单识别 cdp-extract。

    tools/web_tools.py 里有两处硬编码白名单（Bug 1 of cdp-extract routing），
    即使 `web.extract_backend: cdp-extract` 已配，也会因白名单不认而
    静默 fallback 到 searxng auto-detect（SEARXNG_URL 命中）→ 报"search-only"错。

    用 monkey-patch 在 plugin register() 时扩展白名单 —— 真正的 plugin override
    轮子，不动 hermes 源码。注册 provider 之前先装，保证 web_extract_tool
    在 web_tools 模块查找 _is_backend_available / _get_backend 时能命中本插件。
    """
    import tools.web_tools as wt

    # 1) _is_backend_available: 探测本地 CDP 9222
    orig_avail = wt._is_backend_available
    def _patched_avail(backend: str) -> bool:
        if backend == "cdp-extract":
            return bool(_ensure_cdp())
        return orig_avail(backend)
    wt._is_backend_available = _patched_avail

    # 2) _get_backend: 把 cdp-extract 加到 known-set 和 auto-detect 末尾
    orig_get = wt._get_backend
    def _patched_get() -> str:
        from tools.web_tools import (
            _load_web_config, _has_env, _is_tool_gateway_ready,
            _ddgs_package_importable, check_firecrawl_api_key,
        )
        configured = (_load_web_config().get("backend") or "").lower().strip()
        known = {
            "parallel", "firecrawl", "tavily", "exa", "searxng",
            "brave-free", "ddgs", "xai", "cdp-extract",
        }
        if configured in known:
            if configured == "cdp-extract":
                if _ensure_cdp():
                    return configured
                # CDP 不可用 → 落到 auto-detect
            else:
                return configured
        candidates = (
            ("tavily", _has_env("TAVILY_API_KEY")),
            ("exa", _has_env("EXA_API_KEY")),
            ("parallel", _has_env("PARALLEL_API_KEY")),
            ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
            ("firecrawl", _is_tool_gateway_ready()),
            ("searxng", _has_env("SEARXNG_URL")),
            ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")),
            ("ddgs", _ddgs_package_importable()),
            ("cdp-extract", _ensure_cdp()),  # 本插件 append
        )
        for backend, available in candidates:
            if available:
                return backend
        return "firecrawl"  # 兼容默认
    wt._get_backend = _patched_get

    logger.info("cdp-extract: web_tools 白名单已扩展，cdp-extract 命中")


def register(ctx) -> None:
    """注册 web_extract provider + /cdp_tunnel 斜杠命令。

    顺序：先 patch 白名单 → 再注册 provider → 再注册斜杠命令 → 后台启动隧道。
    """
    # 在注册 provider 之前先装白名单 override
    _install_white_list_override()

    ctx.register_web_search_provider(CDPExtractProvider())

    # 注册斜杠命令，用户可在对话中管理隧道
    ctx.register_command(
        name="cdp_tunnel",
        handler=_handle_cdp_tunnel,
        description="管理 CDP 隧道: status / start / stop / restart",
        args_hint="status|start|stop|restart",
    )

    # 后台尝试启动隧道（不阻塞启动流程）
    _start_tunnel_background()


def _start_tunnel_background() -> None:
    """后台线程启动隧道，不阻塞 CLI/gateway 启动。"""
    import threading
    def _run():
        try:
            if _ensure_cdp():
                logger.info("CDP 隧道已就绪（后台启动）")
            else:
                logger.info("CDP 隧道未就绪（稍后 extract 时会重试）")
        except Exception:
            logger.debug("后台隧道启动跳过", exc_info=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
