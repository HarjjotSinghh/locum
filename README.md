# locum

Lets Grok Bot delegate coding work to the Claude Code and Codex CLIs you are
already logged into on your own machine, instead of burning Grok Bot usage on
its own agent loop.

Grok Bot runs on a persistent computer in xAI's cloud, so it cannot see
`localhost`. It reaches this server over a tunnel, as a **custom MCP connector** --
a documented Grok feature, not a workaround.

```
Grok Bot (xAI cloud)
   └─ MCP tool call ──► tunnel ──► this server (your Mac)
                                      ├─ spawns `claude -p`   (your Claude sub)
                                      └─ spawns `codex exec`  (your ChatGPT sub)
```

## Why this is allowed, and where the line is

Anthropic's [Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance)
draws the boundary explicitly:

> Advertised usage limits for Pro and Max plans assume **ordinary, individual
> usage of Claude Code and the Agent SDK**.

> Anthropic does not permit third-party developers to offer Claude.ai login or to
> **route requests through Free, Pro, or Max plan credentials on behalf of their
> users**.

So:

| | |
|---|---|
| ✅ | You, your machine, your subscription, your own work |
| ❌ | Hosting this so other people's requests hit **your** subscription |
| ⚠️ | Sharing the code so others run it on **their own** subscription -- fine only while the invariants below hold |

**Invariants. Do not remove them; they are the reason this is legal.**

1. Never read credential files, keychains, or OAuth tokens.
2. Never call `api.anthropic.com` / `api.openai.com` directly.
3. Only ever spawn the official `claude` / `codex` binaries, authenticated by the
   operator through the vendors' own login flows.
4. Single-operator: one bearer token, one allowlist of workspace roots.

If a change would break one of these, it is the wrong change.

## Setup

Requires `claude` and `codex` already signed in, plus [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # then set a real token and your roots
set -a && source .env && set +a
uv run server.py
```

Tunnel it (Cloudflare quick tunnels do **not** carry SSE; this server uses
Streamable HTTP, so they work fine):

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8791
```

That quick tunnel is fine for a first run, but it hands out a new hostname on
every restart and Grok stores the URL -- so you would re-register and re-consent
every time. For anything ongoing, take a stable hostname instead (needs a domain
already on your Cloudflare account):

```bash
cloudflared tunnel login          # browser, once
./setup-tunnel.sh locum.example.com
cloudflared tunnel run locum
```

`setup-tunnel.sh` is idempotent: it creates the named tunnel if missing, points
DNS at it, and writes `~/.cloudflared/config.yml`. To keep it up across reboots,
`sudo cloudflared service install`.

Register at `grok.com/connectors` -> **New Connector** -> **Custom**, with the
tunnel URL plus `/mcp`.

Grok's custom connectors speak **OAuth 2.1 only** -- the dialog has no static
header field. The bridge therefore ships its own minimal authorization server,
so there is no third-party OAuth app to create. Grok discovers the endpoints via
`/.well-known/oauth-authorization-server` and self-registers over RFC 7591.

If Grok still shows the manual "OAuth Credentials Required" form, fill it as:

| Field | Value |
|---|---|
| Client ID | anything, e.g. `locum` |
| Client Secret | leave empty |
| Authorization Endpoint | `https://<tunnel>/authorize` |
| Token Endpoint | `https://<tunnel>/token` |
| Scopes | `mcp` |
| Token Auth Method | `none (PKCE only)` |

You'll then get a consent screen. It shows the redirect target -- check it says
`grok.com` before approving -- and asks for a passphrase: paste your
`LOCUM_TOKEN`.

That passphrase gate is load-bearing. `/authorize` sits on a public tunnel;
without it, anyone who learned the URL could mint a token and get shell access
to your machine.

Finally, paste `SKILL.md` into a Grok Bot Skill. Without it the Bot keeps
grinding through its own loop and you save nothing.

## Tests

```bash
python3 test_oauth.py
```

Boots a throwaway instance on port 8799 and exercises discovery, dynamic
registration, the consent gate, PKCE enforcement, single-use codes, token
exchange, refresh, and an authenticated MCP `initialize`. 13 assertions.

## Tools

| Tool | Purpose |
|---|---|
| `delegate_to_claude(prompt, cwd, model?)` | Start a Claude Code job. Returns `job_id` immediately. |
| `resume_claude(session_id, prompt, cwd?)` | Continue a session. Reuses the prompt cache -- always prefer for follow-ups. |
| `delegate_to_codex(prompt, cwd, model?)` | Same contract, via Codex CLI. |
| `check_job(job_id)` | Poll. Returns status, turn count, recent tool activity, result. |
| `cancel_job(job_id)` | Kill a runaway job. |

Everything is async. MCP tool calls time out long before a real coding task
finishes, so `delegate_*` returns a handle and the Bot polls. This is the single
thing that makes the integration work at all.

## Safety

`LOCUM_ROOTS` is the only barrier between a cloud agent and your home
directory. Keep it narrow. Never set `LOCUM_PERMISSION_MODE=bypassPermissions`
while a tunnel is open.

Every token comparison uses `hmac.compare_digest`. Authorization codes are
single-use and expire in 120s. PKCE `S256` is required -- `plain` is refused.
`/health` and the discovery documents are the only unauthenticated routes;
`LOCUM_TOKEN` itself also remains a valid bearer token, which is what makes
`curl` smoke tests work.

## Troubleshooting

**Everything 404s, including `/health`, but the tunnel says it is connected.**
Port collision. The Grok Bot desktop app listens on `[::1]:8787`, and macOS
resolves `localhost` to `::1` before `127.0.0.1` -- so an ingress pointed at
`http://localhost:8787` silently reaches Grok Bot instead of the bridge, and
Grok Bot answers `Not found.` This is why the default port is **8791** and why
the ingress rule uses `127.0.0.1`, never `localhost`. To confirm:

```bash
lsof -nP -iTCP:<port> -sTCP:LISTEN     # who actually owns the port
curl -s http://127.0.0.1:<port>/health # bridge answers {"ok": true}
curl -s http://localhost:<port>/health # if this differs, you have a collision
```

`cloudflared --loglevel debug tunnel run <name>` settles it: each request logs
`ingressRule=` and `originService=`, so you can see whether the 404 came from
cloudflared's catch-all or from whatever is actually on the port.

**Jobs fail with "OAuth session expired and could not be refreshed".** The
server was launched from inside a Claude Code session. Claude Code exports
`CLAUDECODE`, a `CLAUDE_CODE_*` family, and `ANTHROPIC_BASE_URL` into every
child process; a `claude` that inherits them believes it is a nested child
session and tries to delegate auth to a host socket that is not listening.
`_child_env()` strips those when nesting is detected, but some sandboxed hosts
broker Claude's credentials entirely in-process, and there a spawned `claude`
has nothing on disk to authenticate with no matter what the environment says.
Run the server from an ordinary terminal.

**Cloudflare returns 403 with `error code: 1010`.** Cloudflare bans the default
`Python-urllib` User-Agent signature. Only that signature -- curl, Go, Node,
okhttp, and an absent User-Agent all pass, so MCP clients are unaffected. Set a
User-Agent on any Python tooling you point at the tunnel:

```python
urllib.request.Request(url, headers={"User-Agent": "locum-check/1.0"})
```

## Honest limits

- Cuts Grok Bot usage, does not zero it -- orchestration turns still meter. The
  win is collapsing ~50 Bot steps into one tool call plus a few polls.
- Your machine must be awake with the tunnel up.
- Quick-tunnel URLs change on restart; `./setup-tunnel.sh` gives you a stable
  hostname so the connector survives.
- Cold delegation re-pays ~18k tokens of `CLAUDE.md` + system prompt setup.
  `resume_claude` avoids it.
