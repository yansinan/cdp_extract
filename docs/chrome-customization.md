# Chrome "Pick a Theme Colour" — Customizing a Chrome Instance's Accent Color

The user-facing "Pick a theme colour" setting in `chrome://settings` (Appearance section) is stored in the per-profile `Preferences` file as a base64-encoded protobuf. There is **no Chrome command-line flag** to override it (verified 2026-06-13: `google-chrome --help | grep -iE 'theme|color'` returns nothing). To programmatically set a custom accent color on a specific Chrome instance, patch the Preferences file.

This is useful for distinguishing multiple Chrome windows on a desktop — e.g. visually marking a Hermes-managed local CDP browser (`~/.hermes/chrome-debug/`) so the user can tell it apart from their personal Chrome.

## Where the value lives

Per-profile JSON file (NOT the top-level `Preferences`, but `Default/Preferences` inside the user-data-dir):

```
$HERMES_HOME/chrome-debug/Default/Preferences
~/.config/google-chrome/Default/Preferences         # user normal Chrome
<any-user-data-dir>/Default/Preferences             # any Chrome instance
```

Field path in the JSON:

```json
{
  "browser": {
    "theme": {
      "color_scheme": 0,                    // 0=system default, 1=light, 2=dark
      "color_scheme2": 0,
      "follows_system_colors": false,
      "saved_local_theme": "CAASCGZmMWE3M2U4SAE="
    }
  }
}
```

`color_scheme` and `follows_system_colors` are simpler flags. The actual color is in `saved_local_theme` (a base64-encoded protobuf).

## Protobuf format (`BrowserTheme`)

The `saved_local_theme` value is a base64-encoded protobuf with this structure (from `chromium/browser_theme.proto`):

| Field # | Wire type | Name | Notes |
|---------|-----------|------|-------|
| 1 | varint | `color_scheme` | 0=default, 1=light, 2=dark |
| 2 | LEN | `theme_color` | hex string `"ffaabbcc"` (8 chars = RRGGBBAA) |
| 5 | LEN | `background_color` | optional |
| 9 | varint | `alternate_ntp` | bool |

The base64 string `CAAQAEgB` decodes to `08 00 10 00 48 01`:
- `08 00` = field 1, varint 0
- `10 00` = field 2, length 0 (empty theme_color)
- `48 01` = field 9, varint 1 (true)

## Python helper to encode a custom color

```python
import base64
import json

def encode_theme_color(hex_color: str) -> str:
    """Encode a theme color as base64 protobuf. Input: 8-char hex like 'ff1a73e8' (RRGGBBAA)."""
    color_bytes = hex_color.encode("ascii")
    payload = bytes([0x08, 0x00])                          # field 1 = color_scheme (0)
    payload += bytes([0x12, len(color_bytes)]) + color_bytes  # field 2 = theme_color
    payload += bytes([0x48, 0x01])                          # field 9 = alternate_ntp = true
    return base64.b64encode(payload).decode()


def patch_chrome_theme(prefs_path: str, hex_color: str = "ff1a73e8") -> None:
    """Set browser.theme.saved_local_theme in a Chrome Preferences file."""
    prefs = json.load(open(prefs_path))
    prefs.setdefault("browser", {}).setdefault("theme", {})
    prefs["browser"]["theme"]["saved_local_theme"] = encode_theme_color(hex_color)
    prefs["browser"]["theme"]["follows_system_colors"] = False
    prefs["browser"]["theme"]["color_scheme"] = 0
    json.dump(prefs, open(prefs_path, "w"), indent=2)


# Example: set Hermes Chrome to Google blue
patch_chrome_theme("/home/dr/.hermes/chrome-debug/Default/Preferences", "ff1a73e8")
```

Common Google accent colors (use 8-char hex, AA=ff for full opacity):
- `ff1a73e8` — Google blue (default)
- `ff34a853` — Google green
- `ffea4335` — Google red
- `fffbbc05` — Google yellow
- `ff8ab4f8` — Light blue

## Apply workflow

1. **Patch the Preferences file** (Chrome does NOT need to be running, but the value is cached in memory if it IS running).
2. **Restart Chrome** to pick up the new value. Either:
   - Kill the existing Chrome on that profile (`pkill -f "user-data-dir=<path>"` — careful: see pitfall below)
   - Or wait for next auto-launch (e.g. via `try_launch_chrome_debug`)
3. **Verify** by opening a new tab in that Chrome — the address bar / accent should show the new color.

## Pitfalls

### 1. `pkill -f` matches the calling shell's own command line

This is a recurring footgun. The pattern `pkill -f "google-chrome.*remote-debugging-port"` will match the bash command that contains the search pattern as a string, killing the parent shell. Two safe alternatives:

```bash
# Use pkill -x (exact process name match) — only matches process COMM, not args
pkill -x chrome
# Note: this also kills the user's normal Chrome, since comm is just "chrome" for all instances

# Safer: kill by PID read from `pgrep` (e.g. the Chrome main process)
MAIN_PID=$(pgrep -x chrome | head -1)
kill -TERM "$MAIN_PID"
```

Or use the dedicated `pkill -P <ppid>` to kill only the children of a specific parent PID. Always verify with `pgrep` first.

### 2. Chrome caches theme in memory

Patching `Preferences` while Chrome is running does NOT take effect until Chrome restarts. The cached in-memory value is what's used to render the accent color. Closing and reopening the window is not enough — must be a full Chrome process exit + relaunch.

### 3. The same `--user-data-dir` can be locked

If two Chrome processes try to use the same user-data-dir, the second one refuses to start (or takes over and the first dies). The `~/.hermes/chrome-debug` profile is the standard path for Hermes-managed CDP Chrome. If the user opens their personal Chrome with `--user-data-dir=$HOME/.hermes/chrome-debug` (without `--remote-debugging-port`), it will lock the profile and the next `try_launch_chrome_debug` will fail. Either:
- Use a different profile for manual Chrome (different `--user-data-dir`)
- Stop the conflicting Chrome first
- Or: tolerate the conflict and re-launch the CDP Chrome after the user closes their personal one

### 4. Per-profile, not per-window

The theme is per-profile, not per-window. If the user wants different colors for different windows of the same Chrome instance, this technique won't work — they'd need different `--user-data-dir` values per window.

## When NOT to use this technique

- **A handful of use cases where you actually want a custom new tab page** — if the goal is to brand the new tab content (e.g. show "this is the Hermes CDP browser" as text on the page), use a custom `new-tab.html` + `session.startup_urls` in Preferences instead. But for accent color specifically, the theme approach is the right call.
- **Cloud browser providers (Browserbase, Browser Use, Firecrawl)** — those run on remote infrastructure, not local Chrome.
- **Headless Chrome** — `--headless` mode does not render the theme color (no chrome UI). The setting is still stored but invisible.

## Related

- `browser-content-extraction` SKILL.md — "Local Browser Launch — Use Hermes's built-in `try_launch_chrome_debug`" section (fire-and-forget process model, why no PID tracking)
- `hermes-plugin-authoring` SKILL.md — "Step 0: Capability Audit" (search existing Hermes modules before building)
- `~/.hermes/hermes-agent/hermes_cli/browser_connect.py` — `try_launch_chrome_debug`, `chrome_debug_data_dir()`
- `~/.hermes/hermes-agent/website/docs/user-guide/features/browser.md` — `/browser connect` documentation
