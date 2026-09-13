"""Exercise the locum OAuth flow exactly as an MCP client would."""
import base64, hashlib, json, os, secrets, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

BASE, TOK = "http://127.0.0.1:8799", "smoketest-token"
REDIRECT = "https://grok.com/connectors/oauth/callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def call(path, data=None, headers=None, method=None):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(BASE + path, data=body, headers=headers or {}, method=method)
    try:
        r = opener.open(req, timeout=20)
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()


def ok(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    return cond


proc = subprocess.Popen(
    ["uv", "run", "server.py"],
    cwd=str(__import__("pathlib").Path(__file__).parent),
    env={**os.environ, "LOCUM_TOKEN": TOK, "LOCUM_PORT": "8799",
         # The throwaway server restores the journal at import; keep it away
         # from the operator's real history.
         "LOCUM_JOBS_FILE": os.path.join(
             tempfile.gettempdir(), f"locum-oauth-test-{os.getpid()}.jsonl")},
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
)
for _ in range(60):
    try:
        if call("/health")[0] == 200:
            break
    except Exception:
        time.sleep(1)

results = []
try:
    print("\n1. discovery")
    s, _, b = call("/.well-known/oauth-protected-resource")
    results.append(ok("protected-resource metadata", s == 200 and "authorization_servers" in b))
    s, _, b = call("/.well-known/oauth-authorization-server")
    meta = json.loads(b)
    results.append(ok("authorization-server metadata", s == 200 and meta["code_challenge_methods_supported"] == ["S256"]))

    print("\n2. 401 advertises where to authenticate")
    s, h, _ = call("/mcp", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    wa = h.get("www-authenticate", "")
    results.append(ok("401 + WWW-Authenticate", s == 401 and "resource_metadata=" in wa))

    print("\n3. dynamic client registration")
    s, _, b = call("/register", data=json.dumps({"client_name": "Grok", "redirect_uris": [REDIRECT]}).encode(),
                   headers={"Content-Type": "application/json"}, method="POST")
    cid = json.loads(b).get("client_id", "")
    results.append(ok("client registered", s == 201 and cid.startswith("locum-"), cid))

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    q = urllib.parse.urlencode({"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                "code_challenge": challenge, "code_challenge_method": "S256",
                                "state": "xyz123", "scope": "mcp"})

    print("\n4. consent screen")
    s, _, b = call(f"/authorize?{q}")
    results.append(ok("renders consent form", s == 200 and "LOCUM_TOKEN" in b))
    results.append(ok("shows redirect target to operator", "grok.com" in b))

    print("\n5. wrong passphrase is rejected")
    s, h, b = call(f"/authorize?{q}", data={"passphrase": "wrong"}, method="POST")
    results.append(ok("no code issued", s == 200 and "Wrong passphrase" in b and "location" not in h))

    print("\n6. correct passphrase issues a code")
    s, h, _ = call(f"/authorize?{q}", data={"passphrase": TOK}, method="POST")
    loc = h.get("location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [""])[0]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("state", [""])[0]
    results.append(ok("302 with code + state", s == 302 and bool(code) and state == "xyz123"))

    print("\n7. PKCE is actually enforced")
    s, _, b = call("/token", data={"grant_type": "authorization_code", "code": code,
                                   "code_verifier": "not-the-verifier", "redirect_uri": REDIRECT})
    results.append(ok("bad verifier rejected", s == 400 and "PKCE" in b))

    print("\n8. token exchange (fresh code)")
    _, h, _ = call(f"/authorize?{q}", data={"passphrase": TOK}, method="POST")
    code2 = urllib.parse.parse_qs(urllib.parse.urlparse(h["location"]).query)["code"][0]
    s, _, b = call("/token", data={"grant_type": "authorization_code", "code": code2,
                                   "code_verifier": verifier, "redirect_uri": REDIRECT})
    tokens = json.loads(b)
    access = tokens.get("access_token", "")
    results.append(ok("access_token issued", s == 200 and bool(access)))

    print("\n9. code is single-use")
    s, _, _ = call("/token", data={"grant_type": "authorization_code", "code": code2,
                                   "code_verifier": verifier, "redirect_uri": REDIRECT})
    results.append(ok("replay rejected", s == 400))

    print("\n10. issued token opens MCP")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "oauth-test", "version": "1"}}}).encode()
    s, _, b = call("/mcp", data=payload, method="POST",
                   headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream"})
    results.append(ok("initialize succeeds", s == 200 and "locum" in b))

    print("\n11. refresh token works")
    s, _, b = call("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    results.append(ok("refreshed", s == 200 and "access_token" in b))
finally:
    proc.terminate()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
