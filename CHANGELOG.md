# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-08-22

### Added

- A dashboard at `/dashboard`. One page listing every delegated session with
  status, model, effort, turns, duration, tokens and cost, plus a detail pane
  showing the full transcript: each tool call with its arguments, the agent's
  reasoning, tool results, and the final output. Running jobs stream live over
  server-sent events.
- `/api/jobs`, `/api/jobs/<id>` and `/api/stream` behind the same auth, for
  anything that wants the data without the page.
- Jobs now record a bounded transcript (`LOCUM_MAX_EVENTS`, default 400) and
  token usage from both CLIs, so cost and depth are visible per session rather
  than only as a total.
- Dashboard auth reuses `LOCUM_TOKEN`, exchanged once for an HttpOnly cookie
  derived from it. No second secret exists, and revoking the token revokes the
  dashboard.

### Changed

- `list_jobs`, the dashboard table and the live feed now share one `_brief()`
  shape, so they cannot report a job differently.

## [0.4.0] - 2026-08-22

### Added

- The server narrates delegated work on stdout: a start line with kind, job id,
  model/effort and cwd, the prompt, every tool call the agent makes as it makes
  it, and a finish line with status, turns, duration and reported cost.
  Previously the only server output was uvicorn access lines, which prove a
  request arrived and say nothing about what ran. Set `LOCUM_NARRATE=0` to
  restore the old behaviour.

## [0.3.1] - 2026-08-22

Found by delegating a review of `_note_codex_event` to Locum itself, at
`model: opus, effort: high`, an hour after that function was written.

### Fixed

- `_note_codex_event` crashed on any JSON line that was not an object. A bare
  string, number, or list is valid JSON and raised `AttributeError`, which
  killed the stdout reader; `_run` then skipped both `proc.wait()` and
  `finalize`, leaving the Codex child unreaped and its `.codex-last-*.txt`
  behind. Nested `error` and `item` values are now type-checked too.
- Any parser exception is now contained at the call site, so no future bug in
  event handling can skip process cleanup. Losing one event beats leaking a
  process.
- A mid-stream `error` or `turn.failed` no longer flips the job out of
  `running`. Codex can emit a transient error and carry on, and marking the job
  failed immediately broke `cancel_job`, let `_prune_jobs` evict a live job, and
  masked a later success. `_run` now decides the final status when the process
  exits, including the exit-0-with-a-reported-failure case.
- Turn counting no longer double-counts on the old protocol. `task_complete`
  fires once per exec rather than once per turn, so counting it alongside
  `agent_message` inflated every count.
- `item.started` and `item.updated` no longer crowd `recent_activity`. They fire
  constantly and add nothing `item.completed` does not.
- Session identifiers are read from the old flat `msg` envelope as well as the
  current one, instead of only being used for the event label.

## [0.3.0] - 2026-08-22

### Added

- `effort` on `delegate_to_claude`, `resume_claude`, and `delegate_to_codex`:
  `low`, `medium`, `high`, or `max`. One vocabulary across both CLIs, so a
  caller never has to know that Claude takes `--effort` while Codex takes
  `-c model_reasoning_effort`. Codex has no distinct `max`, so it maps onto its
  ceiling rather than erroring.
- `model` on `resume_claude`, which previously could not override it, so a
  follow-up can escalate on the turn that actually needs it while keeping the
  context already paid for.
- `check_job` and `list_jobs` echo the `model` and `effort` a job ran with, so
  what an answer cost is verifiable rather than assumed.
- Guidance in `SKILL.md` for choosing them: raise effort where a wrong answer is
  expensive or hard to spot, leave it off for mechanical edits, and prefer
  escalating a resumed session over restarting at a higher setting.

### Fixed

- Codex progress reporting was reading a stream Codex no longer emits. It now
  parses `thread.started` / `turn.started` / `item.completed` / `turn.completed`,
  so `turns` counts instead of staying at 0, `recent_activity` shows the item
  type and its command or path instead of the bare string `item.completed`, and
  `thread_id` is captured as the session handle. The older flat `msg` envelope
  still parses.
- A Codex `turn.failed` or `error` event now marks the job failed. Codex can
  fail a turn and still exit 0, so those jobs were reported `done` with whatever
  happened to be in the output file.

## [0.2.0] - 2026-08-22

### Added

- `list_jobs(limit, status)`: recent jobs, newest first. Answers the first
  question every new operator has, "did it actually run on my machine", without
  grepping the access log. Also recovers a `job_id` you lost and surfaces a
  `session_id` worth resuming.
- `LOCUM_MAX_JOBS` (default 200). Job history lived in memory for the process
  lifetime, which was harmless when the server died with your terminal and is a
  slow leak now that it runs under launchd for weeks. Finished jobs are pruned
  oldest-first past the cap; running jobs are never dropped, since their handle
  is the only way back to them.
- `test_jobs.py`, 18 assertions over ordering, filtering, limits, truncation,
  and the cap. Injects jobs directly into the registry, so it needs no `claude`.

### Fixed

- `CLAUDE_CODE_OAUTH_TOKEN` is no longer stripped by `_child_env()`. It matches
  the `CLAUDE_CODE_` prefix used to undo nested-session plumbing, so it was
  being removed in exactly the headless case it exists to serve, and the
  resulting failure was indistinguishable from the nesting bug.

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

[Unreleased]: https://github.com/HarjjotSinghh/locum/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/HarjjotSinghh/locum/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/HarjjotSinghh/locum/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/HarjjotSinghh/locum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/HarjjotSinghh/locum/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/HarjjotSinghh/locum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HarjjotSinghh/locum/releases/tag/v0.1.0
