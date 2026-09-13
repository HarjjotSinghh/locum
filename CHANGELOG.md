# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Dashboard controls: filter the session list by status, copy a session id
  for `resume_*`, and cancel a queued or running job from its detail pane
  (with a confirm). Cancelling goes through a new `POST /api/jobs/<id>/cancel`
  endpoint behind the same cookie auth, sharing one helper with the
  `cancel_job` tool so both always agree. Session sightings also rebroadcast
  the job so the detail header (and its copy button) appears even when the
  pane was opened before the CLI reported its session.

## [0.7.0] - 2026-09-14

### Added

- A `status` tool: allowed roots, which CLIs are on PATH, running/queued
  counts, and free slots. The orchestrator calls it before delegating instead
  of discovering a missing binary or a bad `cwd` by failing a delegation.
- `git_changes` on `check_job`: when a finished job's `cwd` is a repo, the
  snapshot carries `git status --short` and `git diff --stat` taken at
  finish, so the Bot reports what landed on disk instead of trusting the
  agent's writeup. A clean repo yields empty strings; a non-repo omits the
  field. Journalled with the job, so it survives restarts too.
- `LOCUM_COMPLETION_WEBHOOK`: one JSON POST to the operator's own routine
  when a job finishes (`done`, `error`, or `timeout`), so the orchestrator
  can sleep instead of polling a long job every 30s. The payload mirrors
  `check_job`, delivery runs off the event loop with a 10s timeout, and a
  dead endpoint logs a line without touching the job. This is the server's
  single permitted outbound call, and the CI secrets job now enforces that
  it stays the only one.
- Job metadata persistence. Every spawn, session sighting, and finish appends
  one JSON line to `~/.locum/jobs.jsonl` (`LOCUM_JOBS_FILE`), and a restart
  restores history with session ids intact, so `resume_*` outlives launchd
  recycling the process. Jobs that were mid-flight come back as explicit
  errors rather than vanishing. Results are clipped to 2000 chars and
  transcripts stay in memory only, the file is owner-only, and pruning
  compacts it back to one line per live job.
- A `queued` job status, distinct from `running`. `LOCUM_MAX_CONCURRENT` is a
  semaphore taken after the job already reported `"running"`, so a third job
  looked live in `check_job` and the dashboard while it was sitting in a lock.
  New jobs now report `queued` with their position in line until a slot frees,
  `cancel_job` reaches queued jobs, and pruning never drops them.
- `resume_codex(session_id, prompt, ...)`, the missing twin of `resume_claude`.
  Codex follow-ups previously started cold while Claude follow-ups reused the
  prompt cache; Locum already stored the `thread_id`, it just never sent it
  back. A resume whose stream reports a different thread id than the one asked
  for fails loudly instead of reporting a fresh thread as a follow-up.
- `--version` / `-V`, reading the topmost `## [x.y.z]` heading in CHANGELOG.md.
  Written by a delegated Claude Code job during the demo recording, which is a
  fair test of the tool: the change it described matched the change it made.
  Moved above the config block afterwards, because the module-level
  `LOCUM_TOKEN` check raises `SystemExit` during import and a version flag that
  demands a secret to answer is the wrong shape.

## [0.6.0] - 2026-08-22

### Changed

- **Delegated jobs now run autonomously by default.** `LOCUM_AUTONOMY=bypass`
  passes `--dangerously-skip-permissions` to Claude and
  `--dangerously-bypass-approvals-and-sandbox` to Codex. A delegated job has
  nobody at the keyboard, so an approval prompt does not pause it, it hangs it
  until `LOCUM_JOB_TIMEOUT`. Jobs involving `gh`, package installs, or anything
  else the agent wanted to confirm were stalling.
- `LOCUM_AUTONOMY=ask` restores prompting (`--permission-mode` for Claude,
  `--sandbox workspace-write` for Codex). Only useful from a client that can
  surface prompts; Grok Bot cannot.
- `LOCUM_PERMISSION_MODE` is now consulted only in `ask` mode.
- SECURITY.md and the README were rewritten around this rather than patched.
  They previously said never to run with permissions bypassed, which the new
  default contradicts. The honest version: the token and a narrow `LOCUM_ROOTS`
  are what protect you now, the agent's permission model is not, and the
  workspace allowlist bounds where a job starts rather than what a shell command
  it runs can reach.

### Fixed

- The dashboard's live feed hung at "connecting" behind Cloudflare. The stream
  sent response headers and then waited up to 20s for its first event, and a
  proxy holds headers until some body arrives, so the browser never saw a
  response and `EventSource.onopen` never fired. It now flushes a comment
  immediately and sets `X-Accel-Buffering: no`. First byte through the tunnel
  went from never to about 0.4s.
- `/favicon.ico` returns 204 instead of 401. Browsers request it unprompted, so
  a healthy dashboard showed a red console error.

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

[Unreleased]: https://github.com/HarjjotSinghh/locum/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/HarjjotSinghh/locum/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/HarjjotSinghh/locum/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/HarjjotSinghh/locum/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/HarjjotSinghh/locum/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/HarjjotSinghh/locum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/HarjjotSinghh/locum/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/HarjjotSinghh/locum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HarjjotSinghh/locum/releases/tag/v0.1.0
