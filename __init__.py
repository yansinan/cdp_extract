"""CDP web extract plugin — Hermes Agent 的 web_extract provider。

通过 Chrome DevTools Protocol (CDP) 打开网页、滚动到底触发懒加载，
获取完整 HTML，再经 Readability + Turndown 管道输出结构化 Markdown。

输出接口对齐 hermes-sidebar 的 PageExtractionResult。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from .provider import CDPExtractProvider, TUNNEL_SCRIPT

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


def register(ctx) -> None:
    """注册 web_extract provider + /cdp_tunnel 斜杠命令。

    插件加载时在后台线程尝试启动 CDP 隧道（不阻塞 CLI/gateway 启动），
    隧道也会在 extract() / is_available() 中按需懒加载兜底。
    """
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
            from .provider import _ensure_cdp
            if _ensure_cdp():
                logger.info("CDP 隧道已就绪（后台启动）")
            else:
                logger.info("CDP 隧道未就绪（稍后 extract 时会重试）")
        except Exception:
            logger.debug("后台隧道启动跳过", exc_info=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
