# Security Policy

Read this before running Locum. It is not boilerplate: Locum spawns coding
agents with write access to your filesystem, reachable from a public URL.

## Reporting a vulnerability

Do not open a public issue. Email **harjjotsinghh@gmail.com** with "locum
security" in the subject, or use GitHub's
[private vulnerability reporting](https://github.com/HarjjotSinghh/locum/security/advisories/new).

Include the version or commit, what an attacker can do, and the smallest
reproduction you have. Expect an acknowledgement within 72 hours and an
assessment within a week.

## Threat model

Locum is a single-operator tool. It assumes exactly one person, the operator,
is authorised, and that everything reaching it over the tunnel is untrusted
until it proves otherwise.

**What an attacker gets if they defeat authentication:** the ability to run
arbitrary shell commands as your user, with approval prompts disabled. The
workspace allowlist bounds where a job starts, not what a shell command can
reach. Treat a Locum token exactly like an SSH key, because it grants the same
thing.

Three controls stand between the public internet and that outcome:

| control | what it stops |
|---|---|
| `LOCUM_TOKEN` on the consent screen | anyone who merely learned the tunnel URL |
| `LOCUM_ROOTS` allowlist | reaching files outside the directories you named |
| `LOCUM_AUTONOMY` | nothing, at the default. See below. |

### Authentication

Every request to `/mcp` requires a bearer token: either an OAuth access token
issued through the PKCE flow, or `LOCUM_TOKEN` itself. Both are compared with
`hmac.compare_digest`.

`/health` and the two `.well-known` discovery documents are the only
unauthenticated routes. They expose no secrets and no workspace information.

`/authorize` is reachable by anyone who knows the hostname, which is why it is
gated on `LOCUM_TOKEN` rather than auto-approving. The consent screen also
renders the `redirect_uri` before you approve, so a stranger's authorization
attempt is visible as an unexpected destination.

Authorization codes are single-use and expire after 120 seconds. PKCE `S256` is
required; `plain` is refused.

### Filesystem

`_resolve_cwd()` rejects any path that is not the allowlist root itself or a
descendant of it, after `Path.resolve()` has collapsed `..` and symlinks. Keep
`LOCUM_ROOTS` as narrow as the work requires. Setting it to `$HOME` hands an
agent your SSH keys, browser profiles, and shell history.

### Credentials

Locum never reads credential files, keychains, or OAuth tokens belonging to
Claude or OpenAI, and never calls their APIs. It spawns the official `claude`
and `codex` binaries, which authenticate themselves. There is no code path in
this repository that can exfiltrate a vendor credential, and pull requests that
introduce one will be rejected.

## Operator responsibilities

- **Understand that `LOCUM_AUTONOMY` defaults to `bypass`.** Delegated jobs run
  with all approval prompts disabled, because a job nobody is watching cannot
  answer one: the prompt hangs the job instead of pausing it. This is a
  deliberate trade, and it means an authenticated caller can run any command as
  your user. The token and a narrow `LOCUM_ROOTS` are what stand in the way, not
  the agent's own permission model. Set `LOCUM_AUTONOMY=ask` if you are driving
  Locum from a client that can actually show prompts.
- Keep `LOCUM_TOKEN` out of version control. `.env` is gitignored; keep it that
  way, and keep the file at `chmod 600`.
- **If you set `CLAUDE_CODE_OAUTH_TOKEN`**, understand what changes. Locum still
  never reads a vendor credential store, and the invariant holds: the value is
  one the operator minted with `claude setup-token` and placed in their own
  environment, and Locum passes it through without inspecting it. But `.env` and
  the plist `install-agent.sh` generates now hold a long-lived vendor
  credential. Both are `chmod 600`. Revoke it if either is ever exposed, and
  leave it unset unless you actually need the launchd agent.
- Rotate `LOCUM_TOKEN` if you ever paste it somewhere shared. Rotating means
  re-registering the connector.
- Do not host Locum for other people. It is designed for one operator on one
  machine, and the controls above assume that.
- Prefer a named tunnel over a quick tunnel. Quick-tunnel hostnames are
  effectively public and rotate without warning.

## Out of scope

- Anything requiring the attacker to already have `LOCUM_TOKEN` or local
  access to the machine.
- Vulnerabilities in `claude`, `codex`, `cloudflared`, or Grok Bot. Report those
  to their maintainers.
- Prompt injection reaching the agent through repository contents. Locum passes
  prompts to the CLI unchanged and, at the default autonomy, the agent's own
  permission model is not a control either. A repository you would not run
  `make` in is a repository you should not point a delegated job at.
- Denial of service against your own tunnel.
