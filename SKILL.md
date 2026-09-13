---
name: delegate-to-locum
description: >
  Delegate coding work to the operator's own machine through the locum
  connector. Use for multi-file edits, refactors, debugging, test runs, and
  reading large codebases.
---

# Delegate coding work to the local bridge

Use the `locum` connector for anything that touches a real repository:
multi-file edits, refactors, debugging, test runs, reading large codebases.

The local agent has the whole repo on disk. You do not. Delegating is faster and
cheaper than working through it yourself.

## When to delegate

Delegate when the task involves any of:
- editing more than one file
- reading a file you have not already been given in full
- running tests, builds, linters, or any shell command in a repo
- "figure out why X is broken"

Do it yourself when the task is a single short snippet, a question about code
already pasted into the conversation, or planning with no file access needed.

## How to call it

1. `delegate_to_claude(prompt, cwd)` -> returns a `job_id` right away.
2. Wait ~30s, then `check_job(job_id)`.
3. Repeat step 2 until `status` is `done`. Report `recent_activity` to the user
   while you wait so they can see progress.
4. For a follow-up on the same work, use `resume_claude(session_id, prompt)` --
   or `resume_codex` for a Codex job -- never a fresh delegation. Resuming
   reuses the prompt cache and the prior context; starting cold re-pays roughly
   18k tokens of setup.

## Writing the prompt

The local agent cannot ask you questions. One self-contained instruction:

- State what "done" looks like.
- Name the files or the entry point if you know them.
- Say how to verify (which test, which command).

Good: "In src/auth.ts, replace the hand-rolled JWT check in verifyToken with the
jsonwebtoken library. Keep the same function signature. Verify with
`npm test -- auth.spec.ts` and report the result."

Bad: "fix the auth bug"

## Choosing model and effort

Both are optional, and omitting them uses the operator's defaults. That is the
right call most of the time. Both cost the operator real quota, so raise them
deliberately, not by habit.

Raise `effort` when a wrong answer is expensive or hard to spot:

- `"high"` for architecture decisions, subtle debugging, security-sensitive
  changes, or anything touching auth, money, or data loss
- `"max"` for a genuinely hard problem where you expect one attempt to settle it

Leave `effort` off for mechanical work: renames, formatting, adding a test that
mirrors one that already exists, applying a change you have already fully
described.

`model` follows the same logic. `"sonnet"` is fast and the usual choice;
`"opus"` suits reasoning-heavy work. Omit it unless you have a reason.

A good pattern is to start cheap and escalate: delegate at the default, and if
the result is thin or the agent reports it is unsure, `resume_claude` the same
session with `effort: "high"`. Resuming keeps the context you already paid for,
so escalating costs far less than starting over.

`check_job` and `list_jobs` echo the `model` and `effort` actually used, so you
can tell the operator what a given answer cost them.

## Rules

- Never re-delegate a job that is still `running`. Poll it.
- Never poll faster than every 20s.
- On `status: error` or `timeout`, read `stderr_tail` before retrying, and change
  the prompt rather than repeating it verbatim.
- `cwd` must be an absolute path under an allowed root; the server refuses
  anything else.
