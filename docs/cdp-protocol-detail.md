# CDP 协议交互参考

## Page 域 — 加载流程

```
navigate → frameStartedNavigating
         → frameStartedLoading
         → lifecycleEvent("init")
         → lifecycleEvent("DOMContentLoaded")
         → frameNavigated
         → [navigate 响应: {id, result}]
         → lifecycleEvent("firstPaint")
         → lifecycleEvent("load")     ← 页面完全加载
         → frameStoppedLoading
```

**关键结论：** `lifecycleEvent("load")` 可能在 navige 响应之前或之后到达。必须同时等待两者。
不要在收到 navigate 响应后就以为页面加载完毕——JS 可能还在异步执行。

## Runtime.evaluate + awaitPromise 行为

`Runtime.evaluate` 的 `awaitPromise` 参数会让 CDP 等待 Promise resolve 后再返回。
**它能正确处理 setTimeout（macrotask）、requestAnimationFrame 等定时器。**

```python
# 正确用法：一次性注入异步滚动脚本
await cdp_send(ws, "Runtime.evaluate", {
    "expression": "(async () => { /* setInterval + setTimeout */ return result; })()",
    "awaitPromise": True,
    "returnByValue": True,
})
```

**之前误判为「不能」的原因：** WebSocket 缓冲污染。
导航阶段产生的事件（`lifecycleEvent`、`frameStartedLoading` 等）在 navigate while 循环结束后仍有残留。
当后续 `Runtime.evaluate` 用 `_cdp_send`（按 msg_id 过滤）发送时，这些残留事件被正确跳过，命令正常工作。

## 遗留事件问题

在 navigation wait 循环后，WebSocket 缓冲区可能仍有 2-6 个未读事件：
- `Page.frameStoppedLoading`
- `Page.lifecycleEvent(firstContentfulPaint)` 等

**修复：** 所有后续命令都用 `_cdp_send`（按 msg_id 过滤），自动跳过所有事件消息。

## 滚动脚本演进

| 版本 | 方法 | 覆盖率 | 问题 |
|------|------|--------|------|
| 1 | awaitPromise + IIFE(setTimeout) | 31% | 被缓冲污染误诊为「awaitPromise 不处理 macrotask」 |
| 2 | Python 循环 `window.scrollTo` + `asyncio.sleep(0.2)` | 93% | 每步一次 CDP 命令，耗时 |
| 3 | JS setInterval 自控 + Python 轮询标记 | 93% | Python 仍有轮询 |
| 4 | **awaitPromise + IIFE(setInterval + setTimeout)** | **93%** | ✅ 一条命令，JS 全权控制 |

## cdp_tunnel.sh 自举配置

脚本通过嵌入式 Python 从 `config.yaml` 读取 `plugins.cdp_extract` 配置。
Python 路径搜索顺序：

1. `$HERMES_PY`（环境变量，可配置）
2. `$HOME/.hermes/hermes-agent/venv/bin/python3`
3. `/usr/bin/python3`
4. `python3`（PATH 中的任意版本）

必须使用 Hermes venv 的 Python 才能加载 `yaml` 包。
普通 `python3` 无 `yaml` 包会导致配置加载失败。
