"""CDP web extract plugin — Hermes Agent 的 web_extract provider。

通过 Chrome DevTools Protocol (CDP) 打开网页、滚动到底触发懒加载，
获取完整 HTML，再经 Readability + Turndown 管道输出结构化 Markdown。

输出接口对齐 hermes-sidebar 的 PageExtractionResult。
"""

from __future__ import annotations

from .provider import CDPExtractProvider


def register(ctx) -> None:
    """Register the CDP extract provider with the plugin context."""
    ctx.register_web_search_provider(CDPExtractProvider())
