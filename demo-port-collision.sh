#!/usr/bin/env bash
# Reproduce the IPv4/IPv6 port collision in isolation, for a screenshot.
#
# Two processes can hold the same port on macOS if one binds 127.0.0.1 and the
# other binds ::1. No "address already in use" is raised, because they are
# different address families. `localhost` resolves to ::1 first, so whichever
# process took IPv6 wins every connection made by name, and the other is
# invisible.
#
# This is the mechanism behind the bug where every route returned "Not found."
# while the tunnel, the ingress rules and the routing all checked out.
#
#   ./demo-port-collision.sh [port]

set -euo pipefail
PORT="${1:-8787}"
PY=$(command -v python3)

srv() {  # $1 = bind address, $2 = what it answers
  "$PY" - "$1" "$PORT" "$2" <<'PYEOF' &
import http.server, socket, socketserver, sys
addr, port, body = sys.argv[1], int(sys.argv[2]), sys.argv[3]

class V6(socketserver.TCPServer):
    address_family = socket.AF_INET6

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, *a): pass

cls = V6 if ":" in addr else socketserver.TCPServer
cls.allow_reuse_address = True
cls((addr, port), H).serve_forever()
PYEOF
}

cleanup() { kill $(jobs -p) 2>/dev/null || true; }
trap cleanup EXIT

srv "127.0.0.1" "the process you meant"
srv "::1"       "Not found."
sleep 1

printf '$ lsof -nPw -iTCP:%s -sTCP:LISTEN\n' "$PORT"
lsof -nPw -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR==1 || /LISTEN/'

printf '\n$ python3 -c "import socket;print(socket.getaddrinfo(\x27localhost\x27,%s)[0][4][0])"\n' "$PORT"
"$PY" -c "import socket;print(socket.getaddrinfo('localhost',$PORT)[0][4][0])"

printf '\n$ curl -s http://localhost:%s/\n' "$PORT"; curl -s "http://localhost:$PORT/"; echo
printf '\n$ curl -s http://127.0.0.1:%s/\n' "$PORT"; curl -s "http://127.0.0.1:$PORT/"; echo
printf '\n$ curl -s "http://[::1]:%s/"\n' "$PORT"; curl -s "http://[::1]:$PORT/"; echo

printf '\nSame port. No error. localhost picks the IPv6 one.\n'
