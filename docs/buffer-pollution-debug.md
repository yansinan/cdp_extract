# CDP websocket 缓冲污染调试记录

2026-05-29，在 cdp-extract 插件开发过程中。

## 现象

`Runtime.evaluate` + `awaitPromise` + JS async IIFE 中的 `setTimeout(2000)` 没有等待，
瞬间返回（0.00s），导致滚动未执行。

## 根因

导航（`Page.navigate`）后，WebSocket 中残留了多个事件：
`Page.frameStoppedLoading`、`Page.lifecycleEvent(name="init")`、
`Page.frameNavigated`、`Page.lifecycleEvent(name="DOMContentLoaded")` 等。

后续的 `Runtime.evaluate` 命令发送后，代码调用 `ws.recv()` 读取响应，
但读到的却是这些**残留事件**而不是 `Runtime.evaluate` 的响应。
当循环条件（nav + load 都收到）提前满足后，残留事件未被消费。

## 复现

```python
# 错误做法：导航后只等到 nav_ok + load_ok，不排干残留事件
while not (nav_ok and load_ok):
    msg = json.loads(await ws.recv())
    # 检查 id 或 method...
    # 循环退出时可能有事件未被消费
```

```python
# 正确做法：用 _cdp_send（按 msg_id 过滤）
def cdp_send(method, params, msg_id):
    payload = {"id": msg_id, "method": method, "params": params}
    ws.send(payload)
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == msg_id:  # 只认匹配 id 的响应
            return msg
        # 事件跳过
```

## 教训

1. 任何基于 CDP 的代码必须用 msg_id 匹配机制，否则迟早被残留事件坑。
2. 写 CDP 代码前先查 `chromedevtools.github.io/devtools-protocol` 文档，不凭猜测。
3. 如果 `awaitPromise` 看起来不工作，先检查 websocket 消息流是否正确，而不是假定 setTimeout/RAF 不能用。
