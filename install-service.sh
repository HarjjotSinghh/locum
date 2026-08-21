#!/usr/bin/env bash
# Install cloudflared as a launch daemon that actually works.
#
# Plain `sudo cloudflared service install` installs a plist whose
# ProgramArguments is just ["/opt/homebrew/bin/cloudflared"] -- no `tunnel run`,
# no `--config`. The daemon runs as root, whose HOME is /var/root, so
# ~/.cloudflared/config.yml is invisible to it. Result: it crash-loops every 5s
# (KeepAlive + ThrottleInterval) while the tunnel appears to work, because your
# user-level `cloudflared tunnel run` is the one actually serving traffic.
#
# /etc/cloudflared is the path root reads, so the config and credentials have to
# be copied there.
#
#   sudo ./install-service.sh

set -euo pipefail

TUNNEL="${TUNNEL_NAME:-locum}"
SRC="${SUDO_USER:+/Users/$SUDO_USER}/.cloudflared"
DST=/etc/cloudflared

die() { printf '\n%s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Run with sudo:  sudo ./install-service.sh"
[ -n "${SUDO_USER:-}" ] || die "Run via sudo from your normal user, not as root directly."
[ -f "$SRC/config.yml" ] || die "No $SRC/config.yml -- run ./setup-tunnel.sh <hostname> first."

ID=$(awk '/^tunnel:/ {print $2}' "$SRC/config.yml")
[ -n "$ID" ] || die "Could not read the tunnel id from $SRC/config.yml"
[ -f "$SRC/$ID.json" ] || die "Missing credentials file $SRC/$ID.json"

echo "==> stopping any user-level tunnel (it would double-register as a second connector)"
pkill -u "$SUDO_USER" -f 'cloudflared.*tunnel run' 2>/dev/null || true

echo "==> removing the existing daemon, if any"
cloudflared service uninstall 2>/dev/null || true

echo "==> copying config and credentials to $DST"
mkdir -p "$DST"
install -m 0644 "$SRC/config.yml" "$DST/config.yml"
install -m 0600 "$SRC/$ID.json" "$DST/$ID.json"

# The copied config still points credentials-file at the user's home directory,
# which root cannot read.
sed -i '' "s|^credentials-file:.*|credentials-file: $DST/$ID.json|" "$DST/config.yml"

echo "==> validating ingress from $DST/config.yml"
cloudflared --config "$DST/config.yml" tunnel ingress validate

BIN=$(command -v cloudflared)
PLIST=/Library/LaunchDaemons/com.cloudflare.cloudflared.plist

# `cloudflared service install` writes ProgramArguments containing only the
# binary path, with no subcommand, on every version tested. Bare `cloudflared`
# just prints "use `cloudflared tunnel run` to start tunnel <id>" and exits 1,
# so launchd crash-loops it forever on the 5s ThrottleInterval. Passing
# --config to the installer does not change what it writes. Writing the plist
# directly is the only thing that actually produces a running daemon.
echo "==> writing $PLIST"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.cloudflare.cloudflared</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>--config</string><string>$DST/config.yml</string>
    <string>--no-autoupdate</string>
    <string>tunnel</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Library/Logs/com.cloudflare.cloudflared.out.log</string>
  <key>StandardErrorPath</key><string>/Library/Logs/com.cloudflare.cloudflared.err.log</string>
  <key>ThrottleInterval</key><integer>5</integer>
</dict>
</plist>
PLISTEOF
chmod 0644 "$PLIST"
plutil -lint "$PLIST" >/dev/null

echo "==> loading the daemon"
launchctl bootout system/com.cloudflare.cloudflared 2>/dev/null || true
launchctl bootstrap system "$PLIST"
sleep 4

echo
echo "ProgramArguments (must include 'tunnel' and 'run', not just the binary):"
plutil -p /Library/LaunchDaemons/com.cloudflare.cloudflared.plist | sed -n '/ProgramArguments/,/]/p'

HOST=$(awk '/hostname:/ {print $3; exit}' "$DST/config.yml")
echo
echo "Checking https://$HOST/health ..."
for _ in $(seq 1 15); do
  if curl -sf --max-time 4 "https://$HOST/health" >/dev/null 2>&1; then
    echo "OK -- daemon is serving, and it will survive reboots."
    exit 0
  fi
  sleep 2
done

die "Still not reachable. Check /Library/Logs/com.cloudflare.cloudflared.err.log"
