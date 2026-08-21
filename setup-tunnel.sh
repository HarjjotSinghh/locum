#!/usr/bin/env bash
# Point a stable hostname at the local bridge, so the Grok connector survives
# restarts. Quick tunnels hand out a new *.trycloudflare.com name every time,
# and Grok stores the URL -- which means re-registering and re-consenting daily.
#
#   ./setup-tunnel.sh bridge.example.com
#
# Prerequisite: `cloudflared tunnel login` (browser, once).

set -euo pipefail

HOSTNAME="${1:-}"
TUNNEL="${TUNNEL_NAME:-grok-bridge}"
PORT="${GROK_BRIDGE_PORT:-8791}"
CFDIR="$HOME/.cloudflared"

die() { printf '\n%s\n\n' "$*" >&2; exit 1; }

[ -n "$HOSTNAME" ] || die "usage: ./setup-tunnel.sh <hostname>
  e.g. ./setup-tunnel.sh bridge.example.com
  The domain must already be on your Cloudflare account."

[ -f "$CFDIR/cert.pem" ] || die "Not logged in. Run:  cloudflared tunnel login"

# Idempotent: reuse the tunnel if this script already ran.
if cloudflared tunnel list --output json 2>/dev/null | grep -q "\"name\":\"$TUNNEL\""; then
  echo "==> reusing existing tunnel '$TUNNEL'"
else
  echo "==> creating tunnel '$TUNNEL'"
  cloudflared tunnel create "$TUNNEL"
fi

ID=$(cloudflared tunnel list --output json | python3 -c "
import json,sys
print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNNEL'))")
echo "    id: $ID"

echo "==> routing DNS: $HOSTNAME -> $TUNNEL"
cloudflared tunnel route dns --overwrite-dns "$TUNNEL" "$HOSTNAME"

echo "==> writing $CFDIR/config.yml"
cat > "$CFDIR/config.yml" <<YAML
tunnel: $ID
credentials-file: $CFDIR/$ID.json

ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:$PORT
  - service: http_status:404
YAML

cat <<DONE

Done. Start it with:

    cloudflared tunnel run $TUNNEL

Or run it as a background service that survives reboots:

    sudo cloudflared service install
    sudo launchctl start com.cloudflare.cloudflared

Then re-register the Grok connector at grok.com/connectors with:

    https://$HOSTNAME/mcp

This is the last time you have to re-register -- the hostname is stable now.
Consent again with your GROK_BRIDGE_TOKEN when prompted.

Optional, pins the OAuth issuer to the stable host:

    echo 'GROK_BRIDGE_PUBLIC_URL=https://$HOSTNAME' >> .env

DONE
