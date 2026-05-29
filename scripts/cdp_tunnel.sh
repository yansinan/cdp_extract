#!/usr/bin/env bash
set -euo pipefail

# start_remote_browser_tunnel.sh
# 也可以作为 cdp_extract 插件的隧道管理脚本被调用。
# 所有配置支持通过同名环境变量覆盖（大写，下划线分隔）。
# Usage: $0 {start|stop|restart|status}

# --- 配置（环境变量覆盖，大写+下划线） ---
REMOTE_USER="${CDP_TUNNEL_REMOTE_USER:-sunny}"
REMOTE_HOST="${CDP_TUNNEL_REMOTE_HOST:-192.168.1.35}"
SSH_KEY="${CDP_TUNNEL_SSH_KEY:-}"  # optional: path to private key
REMOTE_SSH_PORT="${CDP_TUNNEL_REMOTE_PORT:-22}"
REMOTE_CHROME_BIN="${CDP_TUNNEL_REMOTE_CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
REMOTE_CHROME_PROFILE="${CDP_TUNNEL_REMOTE_CHROME_PROFILE:-/tmp/chrome-cdp-profile}"
REMOTE_CHROME_ARGS="${CDP_TUNNEL_REMOTE_CHROME_ARGS:---no-first-run}"
REMOTE_PIDFILE="${CDP_TUNNEL_REMOTE_PIDFILE:-/tmp/cdp_extract_chrome.pid}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null || echo ".")" && pwd)"
LOCAL_TUNNEL_PIDFILE="${CDP_TUNNEL_LOCAL_PIDFILE:-${SCRIPT_DIR}/.tunnel.pid}"
LOCAL_FORWARD_PORT="${CDP_TUNNEL_LOCAL_PORT:-9222}"
REMOTE_DEBUG_PORT="${CDP_TUNNEL_REMOTE_DEBUG_PORT:-9222}"
TUNNEL_TOOL="${CDP_TUNNEL_TOOL:-auto}"  # auto | autossh | ssh
AUTOSSH_CMD="${CDP_TUNNEL_AUTOSSH_CMD:-autossh}"
SSH_CMD="${CDP_TUNNEL_SSH_CMD:-ssh}"
CURL_CMD="${CDP_TUNNEL_CURL_CMD:-curl}"
CONNECT_TIMEOUT="${CDP_TUNNEL_CONNECT_TIMEOUT:-5}"
HERMES_PY="${CDP_TUNNEL_HERMES_PY:-}"

SSH_KEY_ARG=()
if [ -n "${SSH_KEY}" ] && [ -f "${SSH_KEY}" ]; then
  SSH_KEY_ARG=( -i "${SSH_KEY}" )
fi

check_local_cdp() {
  if [ -n "${HERMES_PY}" ] && [ -x "${HERMES_PY}" ]; then
    if "$HERMES_PY" - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:${LOCAL_FORWARD_PORT}/json/version", timeout=3).read(1)
PY
    then
      return 0
    fi
  fi
  if command -v "$CURL_CMD" >/dev/null 2>&1; then
    if $CURL_CMD -sS --max-time 3 "http://127.0.0.1:${LOCAL_FORWARD_PORT}/json/version" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

check_remote_cdp() {
  if ssh "${SSH_KEY_ARG[@]}" -o BatchMode=yes -o ConnectTimeout="${CONNECT_TIMEOUT}" "${REMOTE_USER}@${REMOTE_HOST}" "${CURL_CMD} -sS --max-time 3 http://127.0.0.1:${REMOTE_DEBUG_PORT}/json/version >/dev/null 2>&1 && echo OK || echo NO" 2>/dev/null | grep -q OK; then
    return 0
  fi
  return 1
}

# 远端 Chrome 启动脚本（通过 SSH 推送）
start_remote_chrome() {
  ssh "${SSH_KEY_ARG[@]}" -o BatchMode=yes -o ConnectTimeout="${CONNECT_TIMEOUT}" "${REMOTE_USER}@${REMOTE_HOST}" bash -s <<EOF
set -euo pipefail
CHROME_BIN="${REMOTE_CHROME_BIN}"
PIDFILE="${REMOTE_PIDFILE}"
PROFILE="${REMOTE_CHROME_PROFILE}"
LOG="/tmp/cdp_extract_chrome.log"
REMOTE_DEBUG_PORT=${REMOTE_DEBUG_PORT}
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" >/dev/null 2>&1; then
  echo "[start_remote_browser_tunnel]REMOTE_ALREADY_RUNNING"
  exit 0
fi
if [ -x "$CHROME_BIN" ]; then
  nohup "$CHROME_BIN" --remote-debugging-port=$REMOTE_DEBUG_PORT --user-data-dir="$PROFILE" $REMOTE_CHROME_ARGS >"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if curl -sS --max-time 3 "http://127.0.0.1:$REMOTE_DEBUG_PORT/json/version" >/dev/null 2>&1; then
    echo "[start_remote_browser_tunnel]REMOTE_STARTED"
    exit 0
  else
    echo "[start_remote_browser_tunnel]REMOTE_START_FAILED" >&2
    exit 2
  fi
else
  echo "[start_remote_browser_tunnel]REMOTE_CHROME_NOT_FOUND" >&2
  exit 3
fi
EOF
}

start_tunnel() {
  if [ -f "$LOCAL_TUNNEL_PIDFILE" ]; then
    pid=$(cat "$LOCAL_TUNNEL_PIDFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "[start_remote_browser_tunnel]TUNNEL_ALREADY_RUNNING pid=$pid"
      return 0
    else
      rm -f "$LOCAL_TUNNEL_PIDFILE" || true
    fi
  fi

  if command -v "$AUTOSSH_CMD" >/dev/null 2>&1; then
    echo "[start_remote_browser_tunnel]Using autossh to create persistent tunnel"
    AUTOSSH_POLL=60 AUTOSSH_GATETIME=0 $AUTOSSH_CMD -M 0 -f -N -o "ExitOnForwardFailure yes" -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3" -o "BatchMode yes" -o "StrictHostKeyChecking no" "${SSH_KEY_ARG[@]}" -L ${LOCAL_FORWARD_PORT}:localhost:${REMOTE_DEBUG_PORT} ${REMOTE_USER}@${REMOTE_HOST}
    sleep 1
    pid=$(pgrep -f "autossh .* -L ${LOCAL_FORWARD_PORT}:localhost:${REMOTE_DEBUG_PORT}" | head -n1 || true)
    if [ -n "$pid" ]; then
      echo $pid > "$LOCAL_TUNNEL_PIDFILE"
      echo "[start_remote_browser_tunnel]TUNNEL_STARTED pid=$pid"
      return 0
    fi
    echo "[start_remote_browser_tunnel]AUTOSSH_START_FAILED" >&2
    return 2
  else
    echo "[start_remote_browser_tunnel]autossh not found — using ssh (background)."
    ssh "${SSH_KEY_ARG[@]}" -o "StrictHostKeyChecking no" -f -N -o "ExitOnForwardFailure yes" -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3" -o "BatchMode yes" -L ${LOCAL_FORWARD_PORT}:localhost:${REMOTE_DEBUG_PORT} ${REMOTE_USER}@${REMOTE_HOST}
    sleep 1
    pid=$(pgrep -f "ssh .* -L ${LOCAL_FORWARD_PORT}:localhost:${REMOTE_DEBUG_PORT}" | head -n1 || true)
    if [ -n "$pid" ]; then
      echo $pid > "$LOCAL_TUNNEL_PIDFILE"
      echo "[start_remote_browser_tunnel]TUNNEL_STARTED pid=$pid"
      return 0
    fi
    echo "[start_remote_browser_tunnel]SSH_START_FAILED" >&2
    return 2
  fi
}

stop_tunnel() {
  if [ -f "$LOCAL_TUNNEL_PIDFILE" ]; then
    pid=$(cat "$LOCAL_TUNNEL_PIDFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" || true
      sleep 1
    fi
    rm -f "$LOCAL_TUNNEL_PIDFILE" || true
    echo "[start_remote_browser_tunnel]TUNNEL_STOPPED"
    return 0
  fi
  pid=$(pgrep -f "ssh.*-L ${LOCAL_FORWARD_PORT}:localhost:${REMOTE_DEBUG_PORT}" | head -n1 || true)
  if [ -n "$pid" ]; then
    kill "$pid" || true
    echo "[start_remote_browser_tunnel]TUNNEL_STOPPED pid=$pid"
    return 0
  fi
  echo "[start_remote_browser_tunnel]TUNNEL_NOT_FOUND"
  return 1
}

ensure_agent_browser_connect() {
  AGENT_BROWSER_BIN="${CDP_TUNNEL_AGENT_BROWSER_BIN:-}"
  CDP_PORT="${CDP_TUNNEL_LOCAL_PORT:-9222}"

  print_err(){ echo "[start_remote_browser_tunnel]$@" >&2; }

  if [ -z "$AGENT_BROWSER_BIN" ] || [ ! -x "$AGENT_BROWSER_BIN" ]; then
    print_err "agent-browser binary not found or not executable: $AGENT_BROWSER_BIN"
    return 2
  fi

  CONNECT_CMD=("$AGENT_BROWSER_BIN")
  CONNECT_CMD+=("--auto-connect")
  CONNECT_CMD+=("connect" "$CDP_PORT")

  echo "[start_remote_browser_tunnel]Running: ${CONNECT_CMD[*]}"
  if "${CONNECT_CMD[@]}"; then
    echo "[start_remote_browser_tunnel]agent-browser connect succeeded"
  else
    print_err "agent-browser connect failed:${CONNECT_CMD[@]}"
    return 3
  fi
  return 0
}


status() {
  out=""
  if check_local_cdp; then
    out+="LOCAL_CDP_OK\n"
  else
    out+="LOCAL_CDP_NO\n"
  fi
  if check_remote_cdp; then
    out+="REMOTE_CDP_OK\n"
  else
    out+="REMOTE_CDP_NO\n"
  fi
  if [ -f "$LOCAL_TUNNEL_PIDFILE" ]; then
    pid=$(cat "$LOCAL_TUNNEL_PIDFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      # Verify it's actually a tunnel process (ssh/autossh with the expected port forward)
      if ps -p "$pid" -o comm= 2>/dev/null | grep -qE "ssh|autossh"; then
        out+="LOCAL_TUNNEL_PIDFILE=$pid (alive, $(ps -p $pid -o comm= 2>/dev/null))\\n"
      else
        out+="LOCAL_TUNNEL_PIDFILE=$pid (ignored — not a tunnel process, comm=$(ps -p $pid -o comm= 2>/dev/null || echo '?'))\\n"
        rm -f "$LOCAL_TUNNEL_PIDFILE" 2>/dev/null || true
      fi
    else
      rm -f "$LOCAL_TUNNEL_PIDFILE" 2>/dev/null || true
    fi
  fi
  if ssh "${SSH_KEY_ARG[@]}" -o BatchMode=yes -o ConnectTimeout=5 "${REMOTE_USER}@${REMOTE_HOST}" "[ -f ${REMOTE_PIDFILE} ] && echo REMOTE_PID=$(cat ${REMOTE_PIDFILE}) || true" 2>/dev/null | grep -q REMOTE_PID; then
    out+="REMOTE_PIDFILE_PRESENT\n"
  fi
  echo -e "[start_remote_browser_tunnel]$out"
  if check_local_cdp; then
    ensure_agent_browser_connect || echo "[start_remote_browser_tunnel]Failed to connect agent-browser, but CDP is reachable" >&2
  fi
}

case "${1:-status}" in
  status)
    status
    ;;
  start)
    if check_local_cdp; then
      echo "[start_remote_browser_tunnel]LOCAL_ALREADY_AVAILABLE"
      exit 0
    fi
    if ! check_remote_cdp; then
      echo "[start_remote_browser_tunnel]REMOTE_NO_CDP: attempting to start remote chrome"
      start_remote_chrome || {
        echo "[start_remote_browser_tunnel]Failed to start remote chrome" >&2
        exit 2
      }
    else
      echo "[start_remote_browser_tunnel]REMOTE_CDP_OK"
    fi
    start_tunnel || exit $?
    if check_local_cdp; then
      ensure_agent_browser_connect || echo "[start_remote_browser_tunnel]Failed to connect agent-browser, but CDP is reachable" >&2
      echo "[start_remote_browser_tunnel]LOCAL_ALREADY_AVAILABLE"
    fi
    ;;
  stop)
    stop_tunnel || true
    ssh "${SSH_KEY_ARG[@]}" -o BatchMode=yes "${REMOTE_USER}@${REMOTE_HOST}" "if [ -f ${REMOTE_PIDFILE} ]; then pid=\$(cat ${REMOTE_PIDFILE}); kill \$pid >/dev/null 2>&1 || true; rm -f ${REMOTE_PIDFILE} || true; echo REMOTE_CHROME_STOPPED; fi" || true
    ;;
  restart)
    $0 stop
    sleep 1
    $0 start
    ;;
  *)
    echo "[start_remote_browser_tunnel]Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac

# End of script
exit 0
