# CDP Mode Dispatch Pattern (cdp-extract v1.1.0+)

> **Note:** Earlier drafts of this reference described a *single-script* dispatcher (generalizing `cdp_tunnel.sh` to handle both local and remote via `detect_mode()`). The user reviewed and **rejected** that approach in session 2026-06-13 in favor of **separate scripts + a generic slash command**. This reference documents the *chosen* pattern. If you came here looking for the single-script approach, that path is closed.

Applied to `~/.hermes/plugins/web/cdp_extract/` in session 2026-06-13.
Adds **local Chrome** mode to a plugin that was previously remote-tunnel-only, while preserving the remote path bit-for-bit for other Hermes installations using the same git repo.

## Layer split (the key design decision)

```
              USER
               │
               ▼
       /cdp_tunnel  ← slash command (the SKILL)  →  generic dispatcher
               │                                       │
               │              config-driven dispatch  │
               │                                       │
       ┌───────┴────────┐                              │
       ▼                ▼                              │
scripts/cdp_local.sh  scripts/cdp_tunnel.sh            │
(local Chrome)        (SSH tunnel to remote)           │
       │                │                              │
       ▼                ▼                              │
~/.local/share/...   192.168.1.35:9222                 │
chrome-profile       (sunny@)                          │
       │                │                              │
       └────── both write to local 127.0.0.1:9222 ────┘
                              │
                              ▼
                    Python provider (provider.py)
                              │
                              ▼
                  Readability + Turndown (read_down/)
```

**Scripts** (bash files in `scripts/`) stay per-mode. **Slash command** (registered in `__init__.py`) is the generic dispatcher. Two responsibilities, two surfaces — don't conflate.

## Design constraints (all enforced)

1. **Strict config-driven dispatch, no auto-fallback** — user picks mode by config (`local_chrome_bin` set → local; `remote_host` set → remote). Neither set → error exit 4. No silent fallback chains.
2. **Two separate scripts** — `scripts/cdp_local.sh` (new) and `scripts/cdp_tunnel.sh` (unchanged). Single responsibility per file.
3. **Remote path bytes are sacred** — `cdp_tunnel.sh` is not touched at all in this plan. Other Hermes installations using the same git repo see no behavior change.
4. **Generic skill** — `/cdp_tunnel` slash command in `__init__.py` reads config and decides which script to invoke. One user-facing command, two underlying scripts.

## Script 1: cdp_local.sh (new)

Full implementation lives at `scripts/cdp_local.sh`. 190 lines, all env-var driven.

**Interface:** `start | stop | restart | status` — symmetric with cdp_tunnel.sh.

**Env vars (all optional, with defaults):**

| Variable | Default | Purpose |
|---|---|---|
| `CDP_LOCAL_CHROME_BIN` | `/usr/bin/google-chrome` | Chrome binary |
| `CDP_LOCAL_CHROME_PROFILE` | `$HOME/.local/share/cdp-extract/chrome-profile` | Isolated `--user-data-dir` |
| `CDP_LOCAL_CHROME_ARGS` | `--no-sandbox --disable-dev-shm-usage` | Launch args |
| `CDP_LOCAL_PORT` | `9222` | CDP port (matches cdp_tunnel.sh default) |
| `CDP_LOCAL_PIDFILE` | `${SCRIPT_DIR}/.chrome.pid` | Separate from `.tunnel.pid` to avoid cross-mode pollution |
| `CDP_LOCAL_LOG` | `/tmp/cdp-extract-chrome.log` | Chrome log path |
| `CDP_LOCAL_CURL_CMD` | `curl` | Health-check command |

**Self-bootstrap from config.yaml (optional, additive — same pattern as cdp_tunnel.sh):**
Python heredoc reads `plugins.cdp_extract.local_*` keys and exports as `CDP_LOCAL_*` env vars.

**Lifecycle commands:**

```bash
start_local_chrome() {
  # 1. If CDP already up → CDP_ALREADY_UP, return 0
  # 2. If pidfile stale → kill stale, clean, continue
  # 3. If chrome bin not executable → CHROME_NOT_FOUND exit 2
  # 4. mkdir profile
  # 5. nohup chrome --remote-debugging-port=... --user-data-dir=... &
  # 6. echo $! > $PIDFILE
  # 7. sleep 2; check CDP up → STARTED, or START_FAILED with log tail exit 3
}

stop_local_chrome() {
  # 4 branches: no-pidfile → NOT_RUNNING; empty → PIDFILE_EMPTY;
  #             stale → PIDFILE_STALE; alive → SIGTERM → SIGKILL if needed
}

local_status() {
  # echo CDP_OK/CDP_NO + PID + full config (CHROME_BIN, PROFILE, ARGS, LOG)
}
```

**Verification matrix (all PASS in session 2026-06-13):**

| Test | Result |
|---|---|
| `bash -n` syntax | exit 0 |
| idle `status` | `CDP_NO / PID=none / BIN=/usr/bin/google-chrome / PROFILE=~/.local/share/...` |
| `start` (env var) | `STARTED pid=N port=9222 profile=...` |
| `curl /json/version` | `Browser: Chrome/149.0.7827.114` + WS URL |
| Repeat `start` | `CDP_ALREADY_UP port=9222` (idempotent) |
| `stop` | `STOPPED pid=N`, port released, pidfile removed |
| Stale pidfile | `PIDFILE_STALE pid=99999 — removed` |
| Custom env var | PROFILE/ARGS override respected |
| Crash recovery (`kill -9` then `start`) | Auto-cleans stale, starts fresh PID |
| Missing bin (`CDP_LOCAL_CHROME_BIN=/nonexistent`) | `CHROME_NOT_FOUND` stderr, exit 2, no pidfile residue |

## Script 2: cdp_tunnel.sh (UNCHANGED)

Existing remote-tunnel script. Not touched in v1.1.0. Other Hermes installations using the same git repo see byte-identical behavior.

## Skill: /cdp_tunnel (generic dispatcher)

`__init__.py:_handle_cdp_tunnel()` reads config:

```python
def _handle_cdp_tunnel(raw_args: str) -> str:
    cfg = _load_cdp_config()
    if cfg.get('local_chrome_bin'):
        script = LOCAL_CHROME_SCRIPT  # cdp_local.sh
    elif cfg.get('remote_host'):
        script = TUNNEL_SCRIPT        # cdp_tunnel.sh
    else:
        return "Error: neither local_chrome_bin nor remote_host configured"
    return _run_script(script, raw_args)  # forwards status|start|stop|restart
```

User types `/cdp_tunnel start` → reads config → dispatches to the right script. One mental model for the user, two scripts under the hood.

## Why this works for shared repos

- Other Hermes installations: `local_chrome_bin` empty + `remote_host` set → `/cdp_tunnel` dispatches to `cdp_tunnel.sh` (unchanged). They see no behavior change.
- New local-mode users on a desktop with Chrome installed: `local_chrome_bin` set + `remote_host` empty → `/cdp_tunnel` dispatches to `cdp_local.sh`. They get auto-launch, no SSH, no tunnel.
- cdp_tunnel.sh zero edits → zero regression risk.
- Each script is independently testable (env-var driven, no inter-script coupling).

## Why "single generic script" was rejected

The single-script approach (generalize `cdp_tunnel.sh` to handle both modes via `detect_mode()`) was tried in the second plan revision. User rejected because:
- Couples unrelated code paths (Chrome launch + SSH tunnel) in one file
- Forces one PID file for both modes → cross-mode pollution risk
- `detect_mode()` adds an extra decision layer that must be debugged when something fails
- Status output must branch by mode → harder to grep / parse
- "Just one more flag" is the start of every unmaintainable script
- The slash command is already a layer — let IT be the dispatcher, not the script
