# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-22

First public release.

### Added

- Remote MCP server exposing five tools: `delegate_to_claude`, `resume_claude`,
  `delegate_to_codex`, `check_job`, and `cancel_job`.
- Asynchronous job model. `delegate_*` returns a job handle immediately and the
  client polls `check_job`, because MCP tool calls time out long before a real
  coding task finishes.
- Live progress from `claude --output-format stream-json`: turn count and recent
  tool activity, so a polling client can report what the agent is doing rather
  than only that it is running.
- `resume_claude`, which reuses a session id and therefore the prompt cache. A
  cold delegation re-pays roughly 18k tokens of `CLAUDE.md` and system prompt.
- OAuth 2.1 authorization server: discovery metadata, RFC 7591 dynamic client
  registration, PKCE `S256` with single-use 120-second codes, and refresh
  tokens. Required because MCP clients reject bearer-only servers.
- Consent screen gated on `LOCUM_TOKEN`, displaying the `redirect_uri` before
  approval so an unexpected destination is visible.
- Workspace root allowlist, resolved through `Path.resolve()` so `..` and
  symlinks cannot escape it.
- `setup-tunnel.sh` for a stable Cloudflare hostname, and `install-service.sh`
  for a launchd daemon that survives reboots.
- `test_oauth.py`, thirteen assertions over the whole authorization flow.

### Fixed

- Default port moved to 8791 and the tunnel ingress targets `127.0.0.1`
  explicitly. The Grok Bot desktop app listens on `[::1]:8787`, and macOS
  resolves `localhost` to `::1` first, so an ingress pointed at
  `localhost:8787` silently reached that app instead. No "address already in
  use" error is raised, because the two listeners are on different address
  families.
- `install-service.sh` writes the launchd plist directly. `cloudflared service
  install` emits `ProgramArguments` containing only the binary path, with no
  subcommand, so the daemon prints "use `cloudflared tunnel run`" and exits 1 on
  a five-second loop while a user-level tunnel masks the failure.
- Nested Claude Code session variables are stripped before spawning. Claude Code
  exports `CLAUDECODE`, a `CLAUDE_CODE_*` family, and `ANTHROPIC_BASE_URL` into
  every child; an inheriting `claude` believes it is a nested child session and
  fails with "OAuth session expired and could not be refreshed". Stripping only
  happens when nesting is detected, so a deliberate `ANTHROPIC_BASE_URL` still
  works in an ordinary terminal.

[Unreleased]: https://github.com/HarjjotSinghh/locum/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/HarjjotSinghh/locum/releases/tag/v0.1.0
