# Locum

[![CI](https://github.com/HarjjotSinghh/locum/actions/workflows/ci.yml/badge.svg)](https://github.com/HarjjotSinghh/locum/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

*A locum is a qualified professional who temporarily does someone else's job.*

Lets Grok Bot delegate coding work to the Claude Code and Codex CLIs you are
already logged into on your own machine, instead of burning Grok Bot usage on
its own agent loop.

Not affiliated with or endorsed by xAI, Anysphere, OpenAI, or Anthropic.

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
DNS at it, and writes `~/.cloudflared/config.yml`.

To keep the tunnel up across reboots, use the script, not `cloudflared service
install`:

```bash
sudo ./install-service.sh
```

`cloudflared service install` writes a launchd plist containing only the binary
path, with no `tunnel run` subcommand, so the daemon crash-loops while your
user-level tunnel quietly masks the failure. `install-service.sh` writes the
plist itself and verifies `/health` before claiming success.

That keeps the tunnel up. To keep the **server** up as well, so a reboot does not
leave a healthy hostname pointing at nothing:

```bash
./install-agent.sh        # no sudo: it must run as you
```

Two macOS requirements, both of which fail confusingly if missed:

- If this checkout is under `~/Documents`, `~/Desktop`, or `~/Downloads`, grant
  **Full Disk Access to `uv`**. launchd agents do not inherit your terminal's
  TCC grants, and TCC judges the executable launchd starts.
- Run `claude setup-token` and put the result in `.env` as
  `CLAUDE_CODE_OAUTH_TOKEN`. A launchd agent does not get your login session's
  credential access, so delegation fails with "OAuth session expired" even
  though the server itself starts fine.

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

### Making the Bot actually use it

A connector only makes the tools *available*. Without an instruction to prefer
them, the Bot keeps grinding through its own loop and you save nothing. Two
levers, and the weaker one is the one people reach for first.

**1. The Bot's description (strongest).** Create a dedicated Bot, then
**Bot actions → Edit Profile → Description**. That field is for rules that
should remain true, so it applies to every conversation without being invoked:

```
You have the `locum` connector, which delegates work to the operator's own
machine.

Any task touching a real repository (multi-file edits, refactors, debugging,
running tests, reading a codebase) must go to delegate_to_claude rather than
being done yourself.

delegate_to_claude returns a job_id immediately. Poll check_job about every 30s
and report recent_activity so progress is visible. Never re-delegate a job that
is still queued or running. For follow-ups on the same work use resume_claude with the
session_id, never a fresh delegation (resume_codex for Codex jobs).

cwd must be an absolute path inside an allowed root.
```

**2. A saved Skill (the detail).** `SKILL.md` in this repo covers how to write a
good delegation prompt and what to do when a job errors. Save it by asking a Bot
"save this as a skill called delegate-to-locum" with the file contents pasted,
then enable it under **Settings → Plugins → Yours**. Invoke explicitly with `/`
in the composer when you want it applied to a specific task.

Use both. The description guarantees the behaviour; the skill improves the
quality of the prompts the Bot writes.

## Tests

```bash
python3 test_oauth.py
```

Boots a throwaway instance on port 8799 and exercises discovery, dynamic
registration, the consent gate, PKCE enforcement, single-use codes, token
exchange, refresh, and an authenticated MCP `initialize`. 13 assertions.

```bash
uv run --with fastmcp --with uvicorn python3 test_jobs.py
```

Covers the job registry: ordering, status filtering, limits, truncation, and the
`LOCUM_MAX_JOBS` cap. 18 assertions. Neither suite needs `claude` installed.

## Tools

| Tool | Purpose |
|---|---|
| `delegate_to_claude(prompt, cwd, model?, effort?)` | Start a Claude Code job. Returns `job_id` immediately. |
| `resume_claude(session_id, prompt, cwd?, model?, effort?)` | Continue a session. Reuses the prompt cache -- always prefer for follow-ups. |
| `delegate_to_codex(prompt, cwd, model?, effort?)` | Same contract, via Codex CLI. |
| `resume_codex(session_id, prompt, cwd?, model?, effort?)` | Continue a Codex session. Fails loudly if Codex reports back a different thread. |
| `check_job(job_id)` | Poll. Returns status, queue position while queued, turn count, recent tool activity, result. |
| `list_jobs(limit?, status?)` | Recent jobs, newest first. Confirms work really ran, recovers a lost `job_id`, finds a `session_id` to resume. |
| `cancel_job(job_id)` | Kill a runaway job. |

Everything is async. MCP tool calls time out long before a real coding task
finishes, so `delegate_*` returns a handle and the Bot polls. This is the single
thing that makes the integration work at all.

### Model and reasoning effort

`effort` takes one vocabulary across both CLIs, so a caller never has to know
which vendor spells it which way:

| `effort` | Claude | Codex |
|---|---|---|
| `low` / `medium` / `high` | `--effort <level>` | `-c model_reasoning_effort="<level>"` |
| `max` | `--effort max` | `-c model_reasoning_effort="high"` (no distinct max) |

`model` passes through unvalidated, since vendors add models faster than any
allowlist survives. Claude takes aliases (`opus`, `sonnet`, `fable`) or full
names; Codex takes its own.

Both are optional and both cost real quota, so the skill tells the Bot to raise
them deliberately: high effort for architecture, subtle debugging, and anything
touching auth or data loss, and nothing for mechanical edits. `check_job` and
`list_jobs` echo what was actually used.

## The dashboard

`https://your-host/dashboard` shows every delegated session: what was asked,
which model and effort ran it, the full transcript of tool calls, reasoning and
output, token counts, cost, and duration. Running jobs stream in live over
server-sent events, so it doubles as a window onto work happening right now.

Sign in with the same `LOCUM_TOKEN`. It is exchanged for an HttpOnly cookie, so
there is no second secret to manage and revoking the token revokes dashboard
access at the same moment. Everything under `/api/` and `/dashboard` requires
that cookie.

This is also the answer to "how do I show that it is really running on my
machine": the MCP client shows a chat, the dashboard shows the actual tool calls
and token spend behind it.

## Watching it work

The server narrates delegated jobs on stdout, so a terminal beside your MCP
client shows what is actually running:

```
06:51:24  -> claude  1726b9229609  sonnet  ~/Documents/Projects/locum
06:51:24       "How many tools does this MCP server expose? Read server.py..."
06:51:30       Bash grep -c "@mcp.tool" ...
06:51:33       Bash grep -n "@mcp.tool" ...
06:51:35  ok claude  1726b9229609  done - 4 turns - 10.3s - $0.17
```

Under launchd that goes to `~/Library/Logs/locum/server.out.log`:

```bash
tail -f ~/Library/Logs/locum/server.out.log | grep -v 'INFO:'
```

Set `LOCUM_NARRATE=0` for access logs only. The log has no rotation, so on a
long-running install either turn narration off or truncate it periodically.

## Safety

Locum runs agents autonomously by default, because a delegated job has nobody
at the keyboard: an approval prompt does not pause the work, it hangs the job
until it times out. `LOCUM_AUTONOMY=bypass` passes
`--dangerously-skip-permissions` to Claude and
`--dangerously-bypass-approvals-and-sandbox` to Codex.

Be clear-eyed about what that buys and costs. The agent can run any command as
your user. `LOCUM_ROOTS` bounds the directory a job *starts* in, and it is still
the check that stops a caller pointing a job at `~/.ssh`, but a shell command
the agent runs is not confined by it.

What actually protects you, in order:

1. `LOCUM_TOKEN`, which gates both the consent screen and every MCP call
2. `LOCUM_ROOTS`, kept narrow
3. Running this only for yourself, on your own machine

`LOCUM_AUTONOMY=ask` restores prompting, but only use it from a client that can
surface the prompts. Grok Bot cannot, so jobs will hang.

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
lsof -nPw -iTCP:<port> -sTCP:LISTEN    # who actually owns the port
curl -s http://127.0.0.1:<port>/health # locum answers {"ok": true}
curl -s http://localhost:<port>/health # if this differs, you have a collision
```

`./demo-port-collision.sh` reproduces the whole thing in isolation on a port of
your choosing, if you want to see the mechanism without waiting to be bitten by
it.

`cloudflared --loglevel debug tunnel run <name>` settles it: each request logs
`ingressRule=` and `originService=`, so you can see whether the 404 came from
cloudflared's catch-all or from whatever is actually on the port.

**Jobs fail with "OAuth session expired and could not be refreshed".** Check the
boring cause first: run `claude -p "reply with OK"` yourself. If that fails too,
your Claude Code login has genuinely expired and `claude /login` fixes it. Locum
surfaces the CLI's error verbatim, so this looks identical to a Locum bug.

If `claude -p` works standalone but fails through Locum, then the
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

## Project

- [How it works](https://www.harjotrana.com/blog/locum-grok-bot-provider-adapter),
  the architecture writeup, with diagrams and the three bugs that cost the most time
- [CONTRIBUTING.md](CONTRIBUTING.md), development setup and the four invariants
- [SECURITY.md](SECURITY.md), threat model and how to report a vulnerability.
  Read this before exposing Locum to a tunnel
- [CHANGELOG.md](CHANGELOG.md)

Licensed under [Apache 2.0](LICENSE).
