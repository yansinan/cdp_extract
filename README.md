# cdp-extract — CDP 网页内容提取插件

## 前置要求（必读）

### 关键依赖：`agent-browser`

**cdp-extract 通过 Chrome DevTools Protocol (CDP) 抓取网页。** 本地必须有一个 Chrome/Chromium 实例在 port 9222 上开启远程调试。

Hermes 通过以下方式管理这个实例：  

→ **`/browser connect`** 启动 agent 专用 Chrome（user-data-dir: `~/.hermes/chrome-debug/`）  
→ 没有它，cdp-extract 就无法工作，Hermes 会无声降级回 `curl` 提取

### 检查是否已安装

**推荐的一键检查：**

```bash
AGENT_BROWSER=~/.hermes/node/bin/agent-browser; \
if [ ! -x "$AGENT_BROWSER" ]; then echo "❌ 未安装"; exit 1; fi; \
echo "✅ $("$AGENT_BROWSER" --version)"; \
CDP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9222/json/version 2>/dev/null); \
if [ "$CDP" = "200" ]; then echo "✅ CDP 9222 可达"; else echo "❌ CDP 9222 不可达"; fi; \
if pgrep -f "chrome.*remote-debugging.*9222" >/dev/null 2>&1; then echo "✅ Chrome CDP 进程运行中"; else echo "❌ 无 Chrome CDP 进程"; fi
```

正确输出示例（三段全绿才算可用）：
```
✅ agent-browser 0.27.0
✅ CDP 9222 可达
✅ Chrome CDP 进程运行中
```

**单项检查：**

```bash
# agent-browser 二进制是否存在
~/.hermes/node/bin/agent-browser --version

# CDP 端口是否在响应
curl -s http://127.0.0.1:9222/json/version | python3 -m json.tool 2>/dev/null | head -3

# Chrome 进程是否存活
ps aux | grep -E 'chrome.*remote-debugging.*9222' | grep -v grep
```

没装的话：

```bash
npm install -g agent-browser
```

### 如果没有 agent-browser，会发生什么

```
Hermes 需要提取网页内容
  → cdp-extract 插件 is_available() 返回 False（CDP 端口不可达）
  → Hermes 无声降级到 curl（web_extract 兜底）
  → curl 拿不到 JS 渲染后的内容，只返回静态 HTML
  → 用户看到"提取不完整"、"页面内容为空"
  → 排查半天，发现是 agent-browser 没装
```

### 其他依赖

- **Node.js ≥ 18**（read_down 管道运行环境）
- `linkedom` + `@mozilla/readability` + `turndown`（已纳入 `read_down/package.json`，`npm install` 即可）

Hermes Agent 的 `web_extract` provider。通过本地 Chrome DevTools Protocol 打开网页，
滚动到底触发懒加载，获取完整 HTML，再经 Readability + Turndown 管道输出结构化 Markdown。

## 架构

```
┌─ URL ──────────────────────────────────────────────────────────────────┐
│                                                                         │
▼                                                                         │
──── Chrome DevTools Protocol (CDP, port 9222) ────────────────────       │
│  1. Target.createTarget → 新建标签页                                     │
│  2. Page.setLifecycleEventsEnabled(true)                                │
│  3. Page.navigate(url) → 等待 lifecycleEvent("load")                    │
│  4. Runtime.evaluate(滚动脚本) → setInterval(50ms) → 到底 → wait 3s     │
│  5. Runtime.evaluate → document.documentElement.outerHTML              │
│  6. Target.close → 关闭标签页                                           │
└────────────────────────────────────────────────────────────────────────┘
         │
         ▼ raw HTML
──── read_down (Node.js) ────────────────────────────────────────────   │
│  linkedom → @mozilla/readability → turndown → turndown-plugin-gfm     │
└────────────────────────────────────────────────────────────────────────┘
         │
         ▼ PageExtractionResult
         { markdown, text, html, title, byline, length, ... }
```

## 文件结构

```
~/.hermes/plugins/web/cdp_extract/
├── __init__.py          # Plugin 入口: register(ctx)
├── plugin.yaml          # 插件元数据: name, version, kind
├── provider.py          # CDP 抓取 + read_down 调用（全中文注释 + 文档来源）
├── read_down/
│   ├── index.js         # Readability + Turndown 管道（CLI + Library）
│   ├── package.json     # linkedom + @mozilla/readability + turndown + gfm
│   └── package-lock.json
├── README.md
└── .gitignore
```

## 接口

### 输出（对齐 hermes-sidebar `PageExtractionResult`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | `string` | Readability 纯文本 |
| `markdown` | `string?` | Turndown 输出的 Markdown |
| `html` | `string?` | Readability 提取的正文 HTML |
| `title` | `string?` | 页面标题 |
| `byline` | `string?` | 作者 |
| `dir` | `string?` | 文字方向 |
| `length` | `number?` | 字符数 |
| `lang` | `string?` | 语言 |
| `error` | `string?` | 错误信息 |

## 配置

在 `config.yaml` 的 `plugins.cdp_extract` 中配置：

```yaml
plugins:
  enabled:
    - web/cdp_extract
  cdp_extract:
    # CDP 端点（默认 http://127.0.0.1:9222）
    cdp_url: "http://127.0.0.1:9222"

    # --- 远端隧道（可选）---
    # 设了 remote_host 后, 本地 CDP 不可用时自动建隧道
    remote_host: "192.168.1.35"
    remote_user: "sunny"
    remote_port: 22
    ssh_key: ""

    # 端口转发
    local_port: 9222
    remote_debug_port: 9222
    tunnel_tool: auto         # auto | autossh | ssh

    # 远端 Chrome 启动
    remote_chrome_bin: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    remote_chrome_profile: "/tmp/chrome-cdp-profile"
    remote_chrome_args: "--no-first-run"

  disabled: []
```

## 使用

### 作为 Hermes Plugin

```bash
# 确保本地 Chrome/Chromium 启用了远程调试 (port 9222)
google-chrome --remote-debugging-port=9222

# 已在 config.yaml 中启用:
# plugins.enabled:
#   - web/cdp_extract
# web.extract_backend: cdp-extract

# 在 Hermes 会话中调用 web_extract
web_extract("https://example.com/article")

# 管理 CDP 隧道（对话中直接用斜杠命令）
/cdp_tunnel status
/cdp_tunnel start
/cdp_tunnel stop
```

### 直接测试

```bash
cd ~/.hermes && source hermes-agent/venv/bin/activate

# Python 端
python3 -c "
import asyncio
from hermes_cli.plugins import PluginManager
from agent.web_search_registry import get_provider

pm = PluginManager()
pm.discover_and_load()

async def test():
    cdp = get_provider('cdp-extract')
    results = await cdp.extract(['https://example.com'])
    print(results[0].get('markdown', '')[:500])

asyncio.run(test())
"
```

```bash
# Node.js 端（独立测试 read_down）
echo '{"html":"<html><body><h1>Test</h1></body></html>"}' \
  | node read_down/index.js | jq
```

## 文档参考

- [Chrome DevTools Protocol - Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/)
- [Chrome DevTools Protocol - Runtime](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/)
- [Chrome DevTools Protocol - Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/)
- [MDN Document.scrollingElement](https://developer.mozilla.org/en-US/docs/Web/API/Document/scrollingElement)
- [MDN window.scrollTo](https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollTo)
- [@mozilla/readability](https://github.com/mozilla/readability)
- [turndown](https://github.com/mixmark-io/turndown)
