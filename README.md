# cdp-extract — CDP 网页内容提取插件

**依赖：本地 Chrome/Chromium 需开启远程调试（`--remote-debugging-port=9222`）**

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
