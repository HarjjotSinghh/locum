#!/usr/bin/env bash
# Keep the Locum server running across logins and reboots.
#
# A LaunchAgent, not a LaunchDaemon, and the distinction is the point: it runs
# as you, in your login session, so the spawned `claude` and `codex` find the
# same credentials they find when you run them by hand. A root daemon runs as
# root, whose ~/.claude does not exist, and every delegated job fails to
# authenticate.
#
# Pairs with install-service.sh, which keeps the tunnel up. Both are needed: a
# tunnel that survives a reboot without the server is a healthy hostname
# pointing at nothing, which returns 502 and reads like a Cloudflare fault.
#
#   ./install-agent.sh          (no sudo -- sudo would defeat the purpose)
#
# macOS note. launchd agents do not inherit your terminal's TCC grants. If this
# checkout, or LOCUM_ROOTS, lives under ~/Documents, ~/Desktop, or ~/Downloads,
# the agent cannot read them and every start fails with "Operation not
# permitted". The script detects that and tells you the fix rather than
# reporting a false success.

set -euo pipefail

LABEL=co.harjot.locum
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SUPPORT="$HOME/Library/Application Support/locum"
WRAPPER="$SUPPORT/run.sh"
LOGDIR="$HOME/Library/Logs/locum"
ERRLOG="$LOGDIR/server.err.log"
GUI="gui/$(id -u)"

die() { printf '\n%s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" != "0" ] || die "Do not run this with sudo. The agent must run as
you, or the spawned CLIs will not find your logins."

[ -f "$HERE/.env" ] || die "No $HERE/.env -- copy .env.example and set a token first."
[ -f "$HERE/server.py" ] || die "No server.py next to this script."

UV=$(command -v uv) || die "uv is not on PATH. https://docs.astral.sh/uv/"

PORT=$(awk -F= '/^LOCUM_PORT=/ {print $2}' "$HERE/.env")
PORT=${PORT:-8791}

# Warn early rather than after a confusing failure.
case "$HERE" in
  "$HOME"/Documents/*|"$HOME"/Desktop/*|"$HOME"/Downloads/*)
    echo "!! $HERE is inside a TCC-protected folder."
    echo "!! The agent will not start until you grant Full Disk Access (see below)."
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$SUPPORT" "$LOGDIR"

# The wrapper lives outside the repo because launchd cannot execute a file in a
# TCC-protected folder at all, and a clear "cannot read .env" beats an opaque
# "cannot execute run.sh". launchd also gives an agent a near-empty environment
# and no shell, so sourcing .env has to happen here rather than in the plist.
# PATH is rebuilt explicitly: uv shells out to python, and node-managed installs
# of `claude` sit outside launchd's default PATH.
cat > "$WRAPPER" <<WRAPPEREOF
#!/usr/bin/env bash
set -euo pipefail
cd "$HERE"
export PATH="$(dirname "$UV"):$HOME/.local/bin:$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
set -a
. "$HERE/.env"
set +a
exec "$UV" run server.py
WRAPPEREOF
chmod +x "$WRAPPER"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$WRAPPER</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGDIR/server.out.log</string>
  <key>StandardErrorPath</key><string>$ERRLOG</string>
</dict>
</plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null

echo "==> stopping anything already serving port $PORT"
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
# Match both `uv run server.py` and the python it execs, which carries only the
# relative path in its argv and so escapes an absolute-path pattern.
pkill -f "$HERE/server.py" 2>/dev/null || true
for pid in $(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do kill "$pid" 2>/dev/null || true; done
sleep 2

: > "$ERRLOG"
echo "==> loading $LABEL"
launchctl bootstrap "$GUI" "$PLIST"
launchctl enable "$GUI/$LABEL" 2>/dev/null || true

# Verifying the port answers is not enough: a leftover process on the same port
# makes a broken agent look healthy. Require the listening pid to be the agent's.
echo "==> verifying the agent itself is serving"
for _ in $(seq 1 30); do
  AGENT_PID=$(launchctl print "$GUI/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}')
  if [ -n "${AGENT_PID:-}" ] && curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    LISTEN_PID=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
    # The agent runs the wrapper, which execs uv, which spawns python. The
    # listener is a descendant, so walk parents up to the agent.
    p=$LISTEN_PID
    for _ in 1 2 3 4 5; do
      [ "$p" = "$AGENT_PID" ] && break
      p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ') || break
      [ -z "$p" ] && break
    done
    if [ "$p" = "$AGENT_PID" ]; then
      cat <<DONE

OK -- the agent is serving on port $PORT and will come back after a reboot.

  logs     tail -f $ERRLOG
  restart  launchctl kickstart -k $GUI/$LABEL
  stop     launchctl bootout $GUI/$LABEL
  remove   launchctl bootout $GUI/$LABEL && rm "$PLIST"

DONE
      exit 0
    fi
  fi
  if grep -q "Operation not permitted" "$ERRLOG" 2>/dev/null; then
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    die "macOS blocked the agent from reading $HERE.

launchd agents do not inherit the Full Disk Access your terminal has, and
~/Documents, ~/Desktop, and ~/Downloads are protected. The agent cannot read
.env, and a spawned agent could not read your workspace roots either.

Two ways forward:

  1. Grant Full Disk Access to uv:
       System Settings > Privacy & Security > Full Disk Access > +
       press Cmd+Shift+G and enter: $UV
     then re-run this script.

  2. Move this checkout and LOCUM_ROOTS somewhere unprotected, e.g. ~/src.

Until then, run the server in a terminal:  uv run server.py
The tunnel daemon from install-service.sh is unaffected."
  fi
  sleep 2
done

launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
die "The agent did not come up. Last lines of $ERRLOG:

$(tail -5 "$ERRLOG" 2>/dev/null)"
