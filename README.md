# grok-bridge

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
cloudflared tunnel --url http://localhost:8787
```

Register at `grok.com/connectors` -> **New Connector** -> **Custom**:

- URL: `https://<your-tunnel>.trycloudflare.com/mcp`
- Auth header: `Authorization: Bearer <GROK_BRIDGE_TOKEN>`

Then paste `GROK_SKILL.md` into a Grok Bot Skill. Without it the Bot keeps
grinding through its own loop and you save nothing.

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

`GROK_BRIDGE_ROOTS` is the only barrier between a cloud agent and your home
directory. Keep it narrow. Never set `GROK_BRIDGE_PERMISSION_MODE=bypassPermissions`
while a tunnel is open. The bearer token is checked with a constant-time compare
on every request; `/health` is the only unauthenticated route.

## Honest limits

- Cuts Grok Bot usage, does not zero it -- orchestration turns still meter. The
  win is collapsing ~50 Bot steps into one tool call plus a few polls.
- Your machine must be awake with the tunnel up.
- Quick-tunnel URLs change on restart; use a named Cloudflare tunnel for a
  stable one.
- Cold delegation re-pays ~18k tokens of `CLAUDE.md` + system prompt setup.
  `resume_claude` avoids it.
