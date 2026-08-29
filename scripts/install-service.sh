#!/usr/bin/env bash
set -euo pipefail
PROJECT="$(cd "${1:-.}" && pwd)"
STATE="$PROJECT/.agent-control-plane/state.sqlite3"
PORT="${ACP_PORT:-8765}"
PYTHON="${ACP_PYTHON:-$PROJECT/.venv/bin/python}"
if [ "$(uname -s)" = "Darwin" ]; then
  case "$PROJECT" in
    "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
      if [ "${ACP_ALLOW_PROTECTED_PATH:-0}" != "1" ]; then
        echo "Project is under a macOS protected folder: $PROJECT" >&2
        echo "Move it to ~/Code or grant Terminal/Python Full Disk Access, then retry." >&2
        echo "To explicitly override: ACP_ALLOW_PROTECTED_PATH=1 $0 $PROJECT" >&2
        exit 3
      fi
      ;;
  esac
  LABEL="com.agentcontrolplane.$(basename "$PROJECT")"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  RUNNER="$PROJECT/.agent-control-plane/run-service.sh"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
cd "$PROJECT"
export PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m agent_control_plane http --state "$STATE" --port "$PORT"
EOF
  chmod 700 "$RUNNER"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>$LABEL</string><key>ProgramArguments</key><array><string>/bin/bash</string><string>$RUNNER</string></array><key>WorkingDirectory</key><string>$PROJECT</string><key>StandardOutPath</key><string>$PROJECT/.agent-control-plane/service.out.log</string><key>StandardErrorPath</key><string>$PROJECT/.agent-control-plane/service.err.log</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  sleep 1
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "installed $LABEL; health: http://127.0.0.1:$PORT/health"
  else
    echo "service was registered but health check failed; inspect $PROJECT/.agent-control-plane/service.err.log" >&2
    exit 4
  fi
elif command -v systemctl >/dev/null 2>&1; then
  UNIT="$HOME/.config/systemd/user/agent-control-plane.service"; mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=Agent Control Plane
[Service]
Environment=PYTHONPATH=$PROJECT/src
ExecStart=$PYTHON -m agent_control_plane http --state $STATE --port $PORT
Restart=always
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload; systemctl --user enable --now agent-control-plane.service
  sleep 1
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "installed systemd user service; health: http://127.0.0.1:$PORT/health"
  else
    echo "service was registered but health check failed; run systemctl --user status agent-control-plane.service" >&2
    exit 4
  fi
else
  echo "No supported service manager. Keep the MCP host open or run: $PYTHON -m agent_control_plane http --state $STATE --port $PORT" >&2; exit 2
fi
