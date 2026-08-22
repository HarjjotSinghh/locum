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
# macOS note. launchd agents do not inherit your terminal's TCC grants, and
# ~/Documents, ~/Desktop, and ~/Downloads are protected. TCC attributes the
# access to the executable launchd starts, so this plist execs `uv` directly
# rather than a shell wrapper: granting Full Disk Access to uv then works,
# whereas with a wrapper the responsible process is /bin/bash and the uv grant
# is never consulted. That also means .env is read here, at install time, by
# your terminal, and passed through EnvironmentVariables.

set -euo pipefail

LABEL=co.harjot.locum
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/locum"
ERRLOG="$LOGDIR/server.err.log"
GUI="gui/$(id -u)"

die() { printf '\n%s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" != "0" ] || die "Do not run this with sudo. The agent must run as
you, or the spawned CLIs will not find your logins."

[ -f "$HERE/.env" ] || die "No $HERE/.env -- copy .env.example and set a token first."
[ -f "$HERE/server.py" ] || die "No server.py next to this script."

UV=$(command -v uv) || die "uv is not on PATH. https://docs.astral.sh/uv/"
UV=$(readlink -f "$UV" 2>/dev/null || echo "$UV")

PORT=$(awk -F= '/^LOCUM_PORT=/ {print $2}' "$HERE/.env")
PORT=${PORT:-8791}

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

# Build EnvironmentVariables from .env. launchd hands an agent a near-empty
# environment, and PATH must be explicit because uv shells out to python and
# node-managed installs of `claude` sit outside launchd's default PATH.
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

ENVXML=$(
  printf '    <key>PATH</key><string>%s</string>\n' \
    "$(xml_escape "$(dirname "$UV"):$HOME/.local/bin:$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")"
  printf '    <key>HOME</key><string>%s</string>\n' "$(xml_escape "$HOME")"
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    [ -n "$line" ] || continue
    [ "${line:0:1}" = "#" ] && continue
    key=${line%%=*}
    val=${line#*=}
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    printf '    <key>%s</key><string>%s</string>\n' "$key" "$(xml_escape "$val")"
  done < "$HERE/.env"
)

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string>
    <string>run</string>
    <string>$HERE/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
$ENVXML  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGDIR/server.out.log</string>
  <key>StandardErrorPath</key><string>$ERRLOG</string>
</dict>
</plist>
PLISTEOF

# The plist now carries LOCUM_TOKEN, so it is as sensitive as .env.
chmod 600 "$PLIST"
plutil -lint "$PLIST" >/dev/null

echo "==> stopping anything already serving port $PORT"
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
# uv execs python with a relative argv, so an absolute-path pkill misses it.
# Killing by listening port is the only reliable teardown.
for pid in $(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do kill "$pid" 2>/dev/null || true; done
sleep 2

: > "$ERRLOG"
echo "==> loading $LABEL"
launchctl bootstrap "$GUI" "$PLIST"
launchctl enable "$GUI/$LABEL" 2>/dev/null || true

# Verifying the port answers is not verification: a leftover server on the same
# port makes a broken agent look healthy. Require the listening pid to descend
# from the agent's own pid.
echo "==> verifying the agent itself is serving"
for _ in $(seq 1 30); do
  AGENT_PID=$(launchctl print "$GUI/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}')
  if [ -n "${AGENT_PID:-}" ] && curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    p=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
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

Re-run this script after editing .env: the values are copied into the plist.

DONE
      exit 0
    fi
  fi
  if grep -q "Operation not permitted" "$ERRLOG" 2>/dev/null; then
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    die "macOS blocked the agent from reading $HERE.

TCC attributes the access to the executable launchd started, which is:
  $UV

Grant it Full Disk Access, then re-run this script:
  System Settings > Privacy & Security > Full Disk Access > +
  press Cmd+Shift+G and enter the path above

If uv already appears there and is switched on, remove it with the minus button
and add it again: the entry is keyed to the binary, and a uv upgrade invalidates
it. Alternatively, move this checkout and LOCUM_ROOTS somewhere unprotected.

Until then, run the server in a terminal:  uv run server.py
The tunnel daemon from install-service.sh is unaffected."
  fi
  sleep 2
done

launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
die "The agent did not come up. Last lines of $ERRLOG:

$(tail -5 "$ERRLOG" 2>/dev/null)"
