# CDP Chrome Launch Quirks (Wayland/Sway + Chrome 149)

Operational lessons from integrating `cdp-extract` plugin with a local Chrome on **Sway 1.10.1 + wlroots 0.18.2 + Chrome 149**. Pitfalls that don't show up in any doc — only from running it.

## 1. Chrome Single-Instance Lock

**Chrome refuses to start two instances on the same `--user-data-dir`.** The second instance silently joins the existing session and exits:

```
Opening in existing browser session.
```

### Myth busted: different `--profile-directory` does NOT help

```
chrome --user-data-dir=/shared --profile-directory=Default      # 你的 PWA
chrome --user-data-dir=/shared --profile-directory=HermesAgent  # cdp-extract
```

Second Chrome sees the SingletonLock in the user-data-dir root, joins the first, and exits within 1s. Lock is at **user-data-dir level**, not profile level.

### Fix: separate user-data-dir

```
chrome --user-data-dir=/shared/chrome-debug/Default      # PWA (你的)
chrome --user-data-dir=/shared/cdp-chrome/Default       # cdp-extract (新)
```

Or any two non-overlapping paths. The contents can still share a `chrome-debug/` parent dir, just not the same `--user-data-dir` leaf.

## 2. Sway 1.10.1 + Chrome 149 Headed Mode Segfaults

| Mode | Behavior |
|---|---|
| Headed (`--ozone-platform=wayland`) | 3-4s 后 SIGSEGV (exit 139) |
| Headless (`--headless=new`) | 跑稳, 13+ 进程, 9222 在听 |
| X11 fallback (无 `--ozone-platform`) | "Missing X server or $DISPLAY" |

### What it looks like

```bash
$ /usr/bin/google-chrome --ozone-platform=wayland --remote-debugging-port=9222 ...
DevTools listening on ws://127.0.0.1:9222/devtools/browser/...
[ERROR:dbus] org.freedesktop.UPower was not provided
[ERROR:dbus] org.freedesktop.portal.FileChooser not found
exit_code: 139
```

DBus errors are **warnings**, NOT the cause. The segfault is elsewhere (likely wlroots 0.18.2 + Chrome 149 Wayland protocol mismatch).

User's PWA Chrome runs fine **only because** it's been long-running (warm state, no fresh-init code path). Fresh starts on cdp-chrome reliably segfault.

### Fix: just use headless

```bash
google-chrome --headless=new --remote-debugging-port=9222 \
              --user-data-dir=/path --no-first-run --no-default-browser-check
```

**`--headless=new`** is the new headless mode (Chrome 109+) that has full functionality (not the old `--headless` which is broken for localhost). cdp-extract's HTTP extraction works perfectly in headless.

Downside: no visible window — no "blue new tab" visual distinction. Acceptable trade-off for cdp-extract (it's an automated tool, no human needs to see it).

## 3. Required Flags for cdp-extract Auto-Launch

**For Sway/Wayland:**
```bash
google-chrome --headless=new \
              --remote-debugging-port=9222 \
              --user-data-dir=/path \
              --no-first-run --no-default-browser-check
```

**For X11 desktop (有 DISPLAY):**
```bash
google-chrome --remote-debugging-port=9222 \
              --user-data-dir=/path \
              --no-first-run --no-default-browser-check
```

**NOT needed:** `--ozone-platform=wayland` (only needed in headed Wayland mode; headless is platform-agnostic). **`--no-sandbox`**: not strictly required if running as `dr` (sandbox works in user ns).

## 4. Hermes `try_launch_chrome_debug()` is a Black Box — Avoid It

`hermes_cli.browser_connect.try_launch_chrome_debug()` does this:

```python
subprocess.Popen(
    [candidate, "--remote-debugging-port=N",
     "--user-data-dir=~/.hermes/chrome-debug",   # ← 写死, 改不了
     "--no-first-run", "--no-default-browser-check"],
    stdout=DEVNULL, stderr=DEVNULL,
    start_new_session=True,
)
```

**Problems for cdp-extract:**
- 写死 `~/.hermes/chrome-debug` → 撞你 PWA Chrome
- 不加 `--headless=new` → Sway segfault
- 不加 `--ozone-platform=wayland` (Sway 必要) → X11 error
- Popen 不等 → 返回 True 但 CDP 还没起

### Fix: 细粒度 import + 自己 Popen

```python
import platform
from hermes_cli.browser_connect import (
    get_chrome_debug_candidates,   # 多浏览器探测 (Chrome/Chromium/Brave/Edge × 13 路径)
    DEFAULT_BROWSER_CDP_PORT,
)

def _try_hermes_local_chrome() -> bool:
    user_data_dir = os.path.expanduser("~/.hermes/cdp-chrome")
    os.makedirs(user_data_dir, exist_ok=True)

    candidates = get_chrome_debug_candidates(platform.system())
    if not candidates:
        return False

    for candidate in candidates:
        try:
            subprocess.Popen(
                [candidate,
                 "--headless=new",  # 关键: Sway 唯一稳定
                 f"--remote-debugging-port={port}",
                 f"--user-data-dir={user_data_dir}",
                 "--no-first-run", "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # 关键: 等 CDP 真的起 (Popen 不等)
            for i in range(20):  # 10s max
                if _check_local_cdp(CDP_URL):
                    return True
                time.sleep(0.5)
            return False
        except Exception:
            continue
    return False
```

## 5. Plugin Config Schema Pattern

For cdp-extract-style plugins (`plugins.<name>` block):

```yaml
plugins:
  cdp_extract:
    # 本地 (Chrome CDP)
    cdp_url: http://127.0.0.1:9222
    local_chrome_profile: /home/dr/.hermes/cdp-chrome  # user-data-dir
    # 远端 (SSH tunnel) — 留空禁用
    remote_host: ""
    remote_user: ""
    remote_port: 22
```

```python
# Provider 端
def _load_cdp_config() -> dict:
    from hermes_cli.config import load_config
    cfg = load_config()
    return cfg.get("plugins", {}).get("cdp_extract", {}) or {}

user_data_dir = _load_cdp_config().get("local_chrome_profile") \
                or os.path.expanduser("~/.hermes/cdp-chrome")
```

## 6. Common Pitfalls (Quick Reference)

| Pitfall | Symptom | Fix |
|---|---|---|
| Sub-profile under shared user-data-dir | "Opening in existing browser session" + exit 0 | Different `--user-data-dir`, NOT different `--profile-directory` |
| Headed mode on Sway 1.10.1 | 3-4s segfault (exit 139) | Use `--headless=new` |
| `Popen` returns but CDP not ready | Provider says "CDP 不可用" | Add 10s polling loop with `is_browser_debug_ready()` |
| `try_launch_chrome_debug()` is black box | Can't override profile / flags | Use fine-grained `get_chrome_debug_candidates` + custom `Popen` |
| Forgot `--no-first-run --no-default-browser-check` | Chrome opens "Choose default browser" wizard | Always include both |

## 7. Test Stack (verified working)

| Component | Version |
|---|---|
| Google Chrome | 149.0.7827.114 |
| Sway | 1.10.1 |
| wlroots | 0.18.2 |
| Kernel | 6.12.90+deb13.1 |
| Mode | `--headless=new` |
| Result | 13 Chrome processes, 9222 LISTEN, cdp-extract extract() works on example.com, github.com, wikipedia (159KB markdown) |

## Related References

- `references/cdp-protocol-detail.md` — WebSocket buffer pollution, lifecycleEvent
- `references/hermes-cdp-provider-plugin.md` — Provider plugin architecture
- `references/buffer-pollution-debug.md` — Runtime.evaluate + awaitPromise
