# Hermes Local Browser Launch — `hermes_cli.browser_connect`

Hermes ships a cross-platform local Chromium-family browser launcher that any plugin, provider, or tool can use to ensure a CDP-capable browser is running. The module is at `hermes_cli/browser_connect.py` (~217 lines). This file is the canonical reference for plugin authors who need a local browser without writing their own launch logic.

## When to use

- Your provider or tool needs a local Chrome/Chromium/Brave/Edge to drive CDP
- You want a single source of truth for browser detection, launch args, and profile location
- You don't want to maintain cross-platform shell scripts

## When NOT to use

- A user has explicitly attached a browser via `/browser connect` — respect the existing `BROWSER_CDP_URL` env var instead of launching a second instance
- A cloud browser provider is configured and `browser.cloud_provider` is set — use the `BrowserProvider` ABC (`agent/browser_provider.py`) instead
- You're implementing cloud-mode browser tools (Browserbase / Browser Use / Firecrawl)

## Wayland / Sway desktops — Chrome needs `--ozone-platform=wayland`

On Wayland-only compositors (Sway, Hyprland, etc.) with **no X server installed**, `try_launch_chrome_debug` will fail immediately with:

```
[PID:PID:TIMESTAMP:ERROR:ui/ozone/platform/x11/ozone_platform_x11.cc:257] Missing X server or $DISPLAY
[PID:PID:TIMESTAMP:ERROR:ui/aura/env.cc:246] The platform failed to initialize.  Exiting.
```

Chrome defaults to the X11 ozone backend; without `DISPLAY` set it bails. The fix is to add `--ozone-platform=wayland` to the launch args. Since `try_launch_chrome_debug` doesn't accept extra args, wrap the call:

```python
import os, time
from hermes_cli.browser_connect import try_launch_chrome_debug, is_browser_debug_ready

def _ensure_local_chrome(port=9222):
    url = f"http://127.0.0.1:{port}"
    if is_browser_debug_ready(url):
        return True
    if not os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        # Wayland-only desktop — patch Chrome args before launch
        import hermes_cli.browser_connect as bc
        orig = bc._chrome_debug_args
        bc._chrome_debug_args = lambda p: orig(p) + ["--ozone-platform=wayland"]
        try:
            launched = try_launch_chrome_debug(port=port)
        finally:
            bc._chrome_debug_args = orig
    else:
        launched = try_launch_chrome_debug(port=port)
    if not launched:
        return False
    for _ in range(20):
        if is_browser_debug_ready(url):
            return True
        time.sleep(0.5)
    return False
```

**Detection heuristic** for Wayland-only: `WAYLAND_DISPLAY` is set AND `DISPLAY` is unset (Sway's default session has no X fallback). When Xwayland is running alongside, `DISPLAY=:0` will be set and the default X11 path works.

**Proper fix (Hermes-side)**: `try_launch_chrome_debug` should auto-add `--ozone-platform=wayland` when it detects a Wayland-only environment. Until that lands, use the wrapper above. Verified working on Sway 1.10.1 + Debian 13 + Chrome 149.

## API

### `try_launch_chrome_debug(port=9222, system=None) -> bool`

Launches the first available Chromium-family browser with the standard debug args. Returns `True` if a candidate was found and `Popen` succeeded; the caller is responsible for verifying CDP came up via `is_browser_debug_ready()`.

**Key behavior:** uses `subprocess.Popen(..., start_new_session=True)` (Linux/macOS) or `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (Windows). The child process is fully detached — no PID to track, no pidfile, no zombie process to clean up. If the child dies for any reason, the next caller detects it via `is_browser_debug_ready` and re-launches.

```python
from hermes_cli.browser_connect import try_launch_chrome_debug, is_browser_debug_ready
import time

port = 9222
url = f"http://127.0.0.1:{port}"

if not is_browser_debug_ready(url):
    if try_launch_chrome_debug(port):
        for _ in range(20):  # up to 10s
            if is_browser_debug_ready(url):
                break
            time.sleep(0.5)
```

### `is_browser_debug_ready(url, timeout=1.0) -> bool`

Cheap reachability check. Probes both `/json/version` and `/json`; supports `http://`, `https://`, `ws://`, `wss://` schemes. Returns `False` on any connection error, timeout, or non-2xx status.

```python
is_browser_debug_ready("http://127.0.0.1:9222")  # True / False
is_browser_debug_ready("ws://localhost:9222")    # accepts ws:// too
```

### `chrome_debug_data_dir() -> str`

Returns the standard profile directory: `$HERMES_HOME/chrome-debug`. Resolves via `get_hermes_home()` so it respects the active profile (`.hermes/profiles/<name>/chrome-debug` for non-default profiles).

### `get_chrome_debug_candidates(system=None) -> list[str]`

Returns all detected browser binary paths, in priority order. Detection includes:

- **macOS**: `/Applications/{Google Chrome,Chromium,Brave Browser,Microsoft Edge}.app/Contents/MacOS/...`
- **Linux**: 13+ paths covering Chrome, Chromium, Brave, Edge — including `/snap/bin/brave`, `/opt/brave.com/brave/...`, `/usr/bin/google-chrome`, `/opt/google/chrome/chrome`, WSL2 mounts under `/mnt/c/...`
- **Windows**: `ProgramFiles` / `ProgramFiles(x86)` / `LOCALAPPDATA` registry-typical install paths

Pass `system` explicitly (e.g. `"Linux"`) to override `platform.system()` — useful for tests.

### `manual_chrome_debug_command(port=9222, system=None) -> str | None`

Returns a shell-ready string the user can paste to launch manually. Returns `None` if no browser binary was found. Used by `/browser connect` to give the user a fallback command when auto-launch fails.

```python
>>> manual_chrome_debug_command()
'/usr/bin/google-chrome --remote-debugging-port=9222 --user-data-dir=/home/dr/.hermes/chrome-debug --no-first-run --no-default-browser-check'
```

## Standard launch args

These are baked into `_chrome_debug_args(port)` and used by `try_launch_chrome_debug`:

```
--remote-debugging-port=<port>
--user-data-dir=$HERMES_HOME/chrome-debug
--no-first-run
--no-default-browser-check
```

**Why `--user-data-dir`?** Without it, launching a new browser process while a regular instance is already running typically opens a new window on the existing process — which was NOT started with `--remote-debugging-port`, so port 9222 never opens. A dedicated user-data-dir forces a fresh browser process where the debug port actually listens.

**Why `--no-first-run --no-default-browser-check`?** Skips the first-launch wizard for the fresh profile.

## Integration example: cdp_extract provider

The `cdp_extract` provider (`~/.hermes/plugins/web/cdp_extract/provider.py`) wraps CDP-based web extraction. Its `_ensure_cdp()` should use this module rather than maintain its own Chrome launch logic.

**Recommended `_ensure_cdp()` shape:**

```python
from hermes_cli.browser_connect import try_launch_chrome_debug, is_browser_debug_ready
import time

def _ensure_cdp() -> bool:
    global CDP_URL
    CDP_URL = _cdp_url_from_config()

    # ① Already up? (catches /browser connect, manual launch, prior call)
    if is_browser_debug_ready(CDP_URL):
        return True

    # ② Try Hermes's auto-launch (cross-platform, no PID tracking)
    if try_launch_chrome_debug(port=9222):
        for _ in range(20):
            if is_browser_debug_ready(CDP_URL):
                return True
            time.sleep(0.5)

    # ③ Strict config-driven fallback: only if remote_host is set
    cfg = _load_cdp_config()
    if not (cfg.get("remote_host") or "").strip():
        return False  # no auto-fallback chain
    # ... call cdp_tunnel.sh here ...
    return False
```

**Do NOT:**

- Write your own bash script that does `nohup chrome ... > log 2>&1 &` and writes a pidfile
- Hardcode `/usr/bin/google-chrome` (misses Chromium, Brave, Edge, snap installs, WSL2)
- Hardcode `~/.local/share/...` as profile dir (misses XDG, profile-aware home)
- Track the Chrome PID yourself (becomes stale on crash, OOM, user kill)

## `start_new_session=True` — why no PID tracking

The standard bash-script approach (write pidfile, `kill -0` to check liveness, `kill` to terminate) is **inherently fragile**:

- If the child dies outside the script's control (OOM, crash, user kill), the pidfile becomes stale
- Subsequent `start` calls need stale-pid detection + cleanup logic
- The script accumulates complexity to handle edge cases (and pidfile-based commands like `stop` and `status` become footguns — see session 2026-06-13: `pkill -f "google-chrome.*remote-debugging-port"` matched the calling shell's own command line and killed the parent shell)

Hermes's approach (`subprocess.Popen(..., start_new_session=True)`) sidesteps this:

- The child is in a new process group / session, fully detached
- The parent doesn't track the PID
- If the child dies, no cleanup needed — `is_browser_debug_ready` returns `False` and the next caller re-launches
- No `stop` / `restart` / `status` commands are needed at the script level — the parent just checks "is it up?" and acts

This is the right pattern for any long-lived child process started by a Hermes plugin/provider. Reserve pidfile tracking for processes whose lifetime is explicitly tied to the parent's lifetime (e.g. a child that must be killed before the parent exits cleanly via `atexit`).

## `/browser connect` integration

The `/browser connect` slash command (handler in `hermes_cli/cli_commands_mixin.py:_handle_browser_command`) is the CLI surface for this module. It does the same dance — checks `is_browser_debug_ready`, falls back to `try_launch_chrome_debug`, polls the port — and additionally sets `os.environ["BROWSER_CDP_URL"]` so the `browser_*` tools know where to connect.

A provider that uses this module does NOT need the user to run `/browser connect` first — the provider's own `_ensure_cdp()` does the equivalent work for its CDP connection. But it CAN respect an already-attached browser by calling `is_browser_debug_ready(CDP_URL)` first (which returns True if `/browser connect` has already done the work and set `BROWSER_CDP_URL`).

## Verification

```bash
# 1. Confirm Hermes can detect browsers on this system
python3 -c "
from hermes_cli.browser_connect import get_chrome_debug_candidates
import platform
for c in get_chrome_debug_candidates(platform.system())[:5]:
    print(c)
"

# 2. End-to-end: launch + verify CDP comes up
python3 -c "
from hermes_cli.browser_connect import try_launch_chrome_debug, is_browser_debug_ready
import time
url = 'http://127.0.0.1:9222'
print('ready before:', is_browser_debug_ready(url))
print('launched:', try_launch_chrome_debug(9222))
for _ in range(20):
    if is_browser_debug_ready(url):
        print('ready after: True')
        break
    time.sleep(0.5)
"

# 3. CLI surface
hermes
> /browser connect
> /browser status
> /browser disconnect
```

## Related

- `~/.hermes/hermes-agent/website/docs/user-guide/features/browser.md` — user-facing `/browser` documentation
- `~/.hermes/hermes-agent/hermes_cli/browser_connect.py` — source (~217 lines)
- `~/.hermes/hermes-agent/tests/cli/test_cli_browser_connect.py` — test suite
- `~/.hermes/hermes-agent/hermes_cli/cli_commands_mixin.py` — `/browser` command handler (search for `_handle_browser_command`)
- `~/.hermes/hermes-agent/agent/browser_provider.py` — cloud browser provider ABC (different scope; uses Browserbase / Browser Use / Firecrawl)
