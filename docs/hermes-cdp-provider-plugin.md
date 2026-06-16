# Hermes CDP Provider Plugin

Implementation walkthrough for `cdp-extract`, a Hermes `web_extract` provider that:
1. Opens pages via local Chrome DevTools Protocol (port 9222)
2. Scrolls to bottom triggering lazy loads (single JS IIFE + awaitPromise)
3. Grabs full HTML → Readability + Turndown → structured Markdown
4. Falls back to SSH tunnel when local CDP is unavailable

Location: `~/.hermes/plugins/web/cdp_extract/`

## File Roles

| File | Role |
|------|------|
| `plugin.yaml` | Declares `kind: backend`, `provides_web_providers: [cdp-extract]` |
| `__init__.py` | `register(ctx)` calls `ctx.register_web_search_provider(CDPExtractProvider())` |
| `provider.py` | Full pipeline: CDP fetch + read_down bridge + tunnel management |
| `read_down/index.js` | Node.js CLI: stdin JSON({html,url,options}) → stdout JSON({markdown,title,...}) |
| `scripts/cdp_tunnel.sh` | SSH tunnel management, env-var-configurable |

## CDP Command Flow

```
msg_id=1  → Page.enable                        (await response id=1)
msg_id=2  → Page.setLifecycleEventsEnabled(true) (await response id=2)
msg_id=3  → Page.navigate(url)
            ↓ manual event loop:
            - skip events, wait for BOTH response(id=3) AND lifecycleEvent("load")

_scroll_to_bottom(ws, msg_id)
  → one Runtime.evaluate with async IIFE + awaitPromise: true

msg_id=N  → Runtime.evaluate("document.title")
msg_id=N+1→ Runtime.evaluate("document.documentElement.outerHTML")
```

## Page Load Detection

Use `Page.setLifecycleEventsEnabled(true)` + `Page.lifecycleEvent(name="load")`:

```python
# Correct approach:
await _cdp_send(ws, "Page.setLifecycleEventsEnabled", {"enabled": True})

# Then after navigate:
while not (navigate_responded and load_fired):
    msg = json.loads(await ws.recv())
    if msg.get("id") == msg_id:           # navigate response
        navigate_responded = True
    elif msg.get("method") == "Page.lifecycleEvent":
        if msg["params"]["name"] == "load":
            load_fired = True
```

**Do NOT use:**
- `Page.loadEventFired` — deprecated in Chrome 148+
- `Page.frameStoppedLoading` — fires on frame render, NOT full resource load

`lifecycleEvent("load")` fires after `window.onload` — all resources including images and scripts.

## Scroll-to-Bottom (Lazy Load Trigger)

Single `Runtime.evaluate` call with an async IIFE. Python truly awaits via `awaitPromise`:

```python
scroll_js = """
    (async () => {
        const el = document.scrollingElement;
        const step = 80;
        let pos = 0, bottomCount = 0;
        await new Promise((resolve) => {
            const iv = setInterval(() => {
                pos += step;
                window.scrollTo(0, pos);
                if (window.scrollY + window.innerHeight >= el.scrollHeight) {
                    bottomCount++;
                    if (bottomCount >= 2) {
                        clearInterval(iv);
                        setTimeout(() => { resolve(el.scrollHeight); }, 3000);
                    }
                }
            }, 50);
        });
        return el.scrollHeight;
    })()
"""
resp = await _cdp_send(ws, "Runtime.evaluate", {
    "expression": scroll_js, "awaitPromise": True, "returnByValue": True,
})
height = resp["result"]["result"]["value"]
```

Key: `bottomCount >= 2` confirms the page is truly at bottom (two consecutive checks) before triggering the 3-second lazy-load wait.

## Websocket Buffer Pollution

**Critical CDP implementation detail.** After the navigate -> load-wait loop, residual CDP events (like `Page.frameStoppedLoading`, `Page.lifecycleEvent("init")`, `Page.frameNavigated`) linger in the WebSocket receive buffer. If you then send `Runtime.evaluate` and do a naive `await ws.recv()`, you get a stale event instead of the Runtime.evaluate response.

**Fix:** Every command after navigation must use message-ID-based filtering:

```python
def _cdp_send(ws, method, params, msg_id):
    payload = {"id": msg_id, "method": method, "params": params}
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        msg = json.loads(raw)
        if msg.get("id") == msg_id:     # ← key: match on id
            return msg
        # else: it's an event, skip
```

This is why our early tests (which used raw `await ws.recv()`) showed `Runtime.evaluate` returning in 0.00s — they were reading stale navigation events, not the actual Runtime.evaluate response.

## Config Loading

```python
from hermes_cli.config import load_config

cfg = load_config()
plugin_cfg = cfg.get("plugins", {}).get("cdp_extract", {}) or {}

# Then use: plugin_cfg.get("remote_host"), plugin_cfg.get("tunnel_tool", "auto"), etc.
```

## read_down Bridge

Python calls Node.js via subprocess stdin/stdout JSON:

```python
proc = subprocess.run(
    ["node", read_down_index],
    input=json.dumps({"html": html, "url": url, "options": {}}),
    capture_output=True, text=True, timeout=30,
)
result = json.loads(proc.stdout)
# result: { markdown, text, html, title, byline, dir, length, lang, error }
```

## Tunnel Integration

`_ensure_cdp()` implements automatic fallback:

```python
def _ensure_cdp():
    if check_local_cdp(cdp_url):
        return True
    
    cfg = _load_cdp_config()
    if not cfg.get("remote_host"):
        return False
    
    env = os.environ.copy()
    env.update(_build_tunnel_env(cfg))  # maps config keys → CDP_TUNNEL_* env vars
    
    proc = subprocess.run([TUNNEL_SCRIPT, "start"], env=env, ...)
    return check_local_cdp(cdp_url)
```

`config.yaml` → plugin config → env vars → `cdp_tunnel.sh`:

| Config key | Env var |
|-----------|---------|
| `remote_host` | `CDP_TUNNEL_REMOTE_HOST` |
| `remote_user` | `CDP_TUNNEL_REMOTE_USER` |
| `ssh_key` | `CDP_TUNNEL_SSH_KEY` |
| `local_port` | `CDP_TUNNEL_LOCAL_PORT` |
| `tunnel_tool` | `CDP_TUNNEL_TOOL` |
| `remote_chrome_bin` | `CDP_TUNNEL_REMOTE_CHROME_BIN` |
| `agent_browser_bin` | `CDP_TUNNEL_AGENT_BROWSER_BIN` |
