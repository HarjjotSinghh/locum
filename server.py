#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2.10", "uvicorn>=0.30"]
# ///
"""
locum - expose the Claude Code and Codex CLIs you are ALREADY logged into
on this machine to Grok Bot, as a remote MCP server.

ToS-safety invariants. Do not remove these; they are the reason this is legal:
  1. Never reads credential files, keychains, or OAuth tokens.
  2. Never calls api.anthropic.com / api.openai.com directly.
  3. Only ever spawns the official `claude` / `codex` binaries, which the
     operator authenticated themselves via the vendors' own login flows.
  4. Single-operator. One bearer token, one allowlist of workspace roots.
     Never host this so that other people's requests hit your subscription.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

from fastmcp import FastMCP

import dashboard

# Version lives in CHANGELOG.md (and the matching git tag); read the topmost
# released heading rather than keeping a second copy of the number here.
def _read_version() -> str:
    changelog = Path(__file__).resolve().parent / "CHANGELOG.md"
    try:
        for line in changelog.read_text(encoding="utf-8").splitlines():
            m = re.match(r"##\s*\[(\d[^\]]*)\]", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return "unknown"


__version__ = _read_version()

# Before the config below: that raises SystemExit when LOCUM_TOKEN is unset, so
# a version check placed after it would demand a secret to answer.
if __name__ == "__main__" and {"--version", "-V"} & set(sys.argv[1:]):
    print(f"locum {__version__}")
    sys.exit(0)

# ---------------------------------------------------------------- config ----

def _roots() -> list[Path]:
    raw = os.environ.get("LOCUM_ROOTS", str(Path.home() / "Documents" / "Projects"))
    out = []
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if chunk:
            out.append(Path(chunk).expanduser().resolve())
    if not out:
        raise SystemExit("LOCUM_ROOTS resolved to nothing")
    return out

ROOTS = _roots()
TOKEN = os.environ.get("LOCUM_TOKEN", "")
HOST = os.environ.get("LOCUM_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCUM_PORT", "8791"))
PERMISSION_MODE = os.environ.get("LOCUM_PERMISSION_MODE", "acceptEdits")
# A delegated job has nobody at the keyboard. Anything that stops to ask for
# approval does not pause, it hangs until the job times out, so autonomy is the
# default. "ask" restores prompting for anyone driving Locum from a client that
# can actually surface them.
AUTONOMY = os.environ.get("LOCUM_AUTONOMY", "bypass").strip().lower()
if AUTONOMY not in {"bypass", "ask"}:
    raise SystemExit(f'LOCUM_AUTONOMY must be "bypass" or "ask", got {AUTONOMY!r}')
JOB_TIMEOUT = int(os.environ.get("LOCUM_JOB_TIMEOUT", "1800"))
MAX_CONCURRENT = int(os.environ.get("LOCUM_MAX_CONCURRENT", "2"))
MAX_JOBS = int(os.environ.get("LOCUM_MAX_JOBS", "200"))
# Narrate delegated work on stdout. Uvicorn's access lines prove a request
# arrived; they say nothing about what ran. Set to 0 for access logs only.
NARRATE = os.environ.get("LOCUM_NARRATE", "1") not in {"0", "false", "no"}
# Transcript depth per job, for the dashboard. Bounded because the process runs
# for weeks and a chatty agent emits thousands of events.
MAX_EVENTS = int(os.environ.get("LOCUM_MAX_EVENTS", "400"))
# Job metadata journal: one JSON object per line, last line per id wins. Full
# transcripts stay in memory only; this carries just enough (session ids,
# status, cost) that history and resume_* survive a restart.
JOBS_FILE = Path(os.environ.get("LOCUM_JOBS_FILE",
                                str(Path.home() / ".locum" / "jobs.jsonl"))).expanduser()
# POST one JSON payload here when a job finishes, so the orchestrator can
# sleep instead of polling. Empty disables it. This is the operator's own
# routine, not a vendor API, and the payload carries no credentials.
WEBHOOK_URL = os.environ.get("LOCUM_COMPLETION_WEBHOOK", "").strip()

# One vocabulary across both CLIs, so a caller never has to know which vendor
# spells it which way. Claude takes --effort low|medium|high|xhigh|max; Codex
# takes -c model_reasoning_effort with none|minimal|low|medium|high. "max" has
# no Codex equivalent, so it lands on its ceiling rather than erroring.
EFFORT_LEVELS = ("low", "medium", "high", "max")
CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def _claude_autonomy() -> list[str]:
    if AUTONOMY == "bypass":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", PERMISSION_MODE]


def _codex_autonomy(resume: bool = False) -> list[str]:
    if AUTONOMY == "bypass":
        return ["--dangerously-bypass-approvals-and-sandbox"]
    # `codex exec resume` accepts no --sandbox flag, so in ask mode a resume
    # just omits the bypass flag and prompting comes back by default.
    return [] if resume else ["--sandbox", "workspace-write"]


def _effort(level: str | None) -> str | None:
    if level is None:
        return None
    level = level.strip().lower()
    if level not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {list(EFFORT_LEVELS)}, got {level!r}")
    return level

if not TOKEN:
    raise SystemExit(
        "LOCUM_TOKEN is not set. Pick one and keep it stable -- the Grok\n"
        "connector stores it, so regenerating means re-registering.\n\n"
        f'  export LOCUM_TOKEN="{secrets.token_urlsafe(32)}"\n'
    )

# ------------------------------------------------------------------ jobs ----

@dataclass
class Event:
    """One thing the agent did, normalised across Claude and Codex so the
    dashboard renders a single transcript format for both."""
    t: float
    kind: str      # tool | thinking | text | system | error | raw
    label: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.t, "kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass
class Job:
    id: str
    kind: str                       # "claude" | "codex"
    prompt: str
    cwd: str
    status: str = "queued"          # queued | running | done | error | timeout | cancelled
    session_id: str | None = None
    result: str | None = None
    error: str | None = None
    turns: int = 0
    model: str | None = None
    effort: str | None = None
    cost_usd: float | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    activity: deque[str] = field(default_factory=lambda: deque(maxlen=14))
    seen_labels: set[str] = field(default_factory=set)
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    tokens: dict[str, int] = field(default_factory=dict)
    resumed_from: str | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    proc: Any = None

JOBS: dict[str, Job] = {}
SEM = asyncio.Semaphore(MAX_CONCURRENT)


def _resolve_cwd(cwd: str | None) -> str:
    """Reject anything outside the allowlist. Grok Bot lives in xAI's cloud;
    this is the only thing standing between it and your whole home directory."""
    p = (Path(cwd).expanduser() if cwd else ROOTS[0]).resolve()
    if not any(p == r or r in p.parents for r in ROOTS):
        raise ValueError(
            f"cwd {p} is outside the allowed roots: {', '.join(map(str, ROOTS))}"
        )
    if not p.is_dir():
        raise ValueError(f"cwd {p} does not exist or is not a directory")
    return str(p)


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise ValueError(f"`{binary}` is not on PATH for this server process")
    return path


# Live subscribers for the dashboard. Each is a bounded queue: a browser that
# stops reading must never block the agent that is producing events.
SUBSCRIBERS: set[asyncio.Queue] = set()


def _broadcast(payload: dict[str, Any]) -> None:
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _log(line: str) -> None:
    if NARRATE:
        print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)


def _activity(job: Job, text: str, kind: str = "tool", detail: str = "") -> None:
    """Record an agent action once, for all three consumers: check_job's
    recent_activity, the dashboard transcript, and the server log. Routing
    everything through here is what keeps them from drifting apart."""
    text = " ".join(text.split())
    job.activity.append(text)
    ev = Event(t=time.time(), kind=kind, label=text[:120], detail=detail)
    job.events.append(ev)
    _broadcast({"type": "event", "job_id": job.id, "event": ev.as_dict()})
    _log(f"     {text[:100]}")


def _clip(text: str, width: int) -> str:
    """Slice rather than textwrap.shorten, which discards the whole string when
    the first whitespace-delimited token is longer than width."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."


def _brief(job: Job) -> dict[str, Any]:
    """One row's worth of a job. Shared by list_jobs, the dashboard table, and
    the live feed, so all three always agree."""
    out: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "elapsed_seconds": round((job.finished or time.time()) - job.started, 1),
        "started": job.started,
        "turns": job.turns,
        "cwd": job.cwd,
        "prompt": _clip(job.prompt, 120),
    }
    for key, val in (("model", job.model), ("effort", job.effort),
                     ("session_id", job.session_id), ("resumed_from", job.resumed_from)):
        if val:
            out[key] = val
    if job.tokens:
        out["tokens"] = dict(job.tokens)
    if job.cost_usd is not None:
        out["cost_usd"] = round(job.cost_usd, 4)
    if job.status == "done" and job.result:
        out["result"] = _clip(job.result, 200)
    if job.error:
        out["error"] = _clip(job.error, 200)
    return out


def _snapshot(job: Job) -> dict[str, Any]:
    elapsed = round((job.finished or time.time()) - job.started, 1)
    out: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "elapsed_seconds": elapsed,
        "turns": job.turns,
        "recent_activity": list(job.activity),
    }
    if job.model:
        out["model"] = job.model
    if job.effort:
        out["effort"] = job.effort
    if job.session_id:
        out["session_id"] = job.session_id
    if job.status == "done":
        out["result"] = job.result
        if job.cost_usd is not None:
            out["reported_cost_usd"] = job.cost_usd
    if job.status in {"error", "timeout"}:
        out["error"] = job.error
        out["stderr_tail"] = list(job.stderr_tail)
    if job.status == "queued":
        # JOBS preserves spawn order and the semaphore wakes waiters FIFO, so
        # the index among queued jobs is the job's real position in line.
        queued = [jid for jid, j in JOBS.items() if j.status == "queued"]
        out["queue_position"] = queued.index(job.id) + 1 if job.id in queued else 1
        out["hint"] = ("Waiting for a free slot behind other jobs. "
                       "Poll check_job again in 20-30s.")
    if job.status == "running":
        out["hint"] = "Still working. Poll check_job again in 20-30s."
    return out

# ---------------------------------------------------------- persistence ----

RESULT_KEEP = 2000   # bounds the journal; full results live in memory only
PROMPT_KEEP = 4000


def _job_record(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "kind": job.kind,
        "prompt": _clip(job.prompt, PROMPT_KEEP),
        "cwd": job.cwd,
        "status": job.status,
        "session_id": job.session_id,
        "resumed_from": job.resumed_from,
        "model": job.model,
        "effort": job.effort,
        "cost_usd": job.cost_usd,
        "tokens": dict(job.tokens),
        "turns": job.turns,
        "started": job.started,
        "finished": job.finished,
        "result": _clip(job.result, RESULT_KEEP) if job.result else None,
        "error": job.error,
    }


def _persist_job(job: Job) -> None:
    """Append one metadata line. Best-effort: a failure here must never break
    the job itself, so it logs and moves on."""
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not JOBS_FILE.exists():
            JOBS_FILE.touch()
            os.chmod(JOBS_FILE, 0o600)
        with open(JOBS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(_job_record(job)) + "\n")
    except OSError as exc:
        _log(f"!! persist {job.id} failed: {exc}")


def _rewrite_jobs_file() -> None:
    """Compact the journal back to one line per live job. Runs after pruning
    and after loading, so the append-only file cannot grow without bound."""
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            for job in JOBS.values():
                f.write(json.dumps(_job_record(job)) + "\n")
        os.chmod(JOBS_FILE, 0o600)
    except OSError as exc:
        _log(f"!! compact jobs file failed: {exc}")


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _job_from_record(rec: dict[str, Any]) -> Job:
    """Rebuild a job from one journal line. Defensive about types: the file
    outlives the process, so it can predate whatever wrote it."""
    kind = rec.get("kind")
    prompt = rec.get("prompt")
    cwd = rec.get("cwd")
    job = Job(id=rec["job_id"],
              kind=kind if isinstance(kind, str) else "claude",
              prompt=prompt if isinstance(prompt, str) else "",
              cwd=cwd if isinstance(cwd, str) else "")
    job.status = rec.get("status") if isinstance(rec.get("status"), str) else "done"
    job.session_id = _str_or_none(rec.get("session_id"))
    job.resumed_from = _str_or_none(rec.get("resumed_from"))
    job.model = _str_or_none(rec.get("model"))
    job.effort = _str_or_none(rec.get("effort"))
    job.error = _str_or_none(rec.get("error"))
    job.result = _str_or_none(rec.get("result"))
    cost = rec.get("cost_usd")
    job.cost_usd = cost if isinstance(cost, (int, float)) else None
    job.tokens = dict(rec.get("tokens")) if isinstance(rec.get("tokens"), dict) else {}
    turns = rec.get("turns")
    job.turns = turns if isinstance(turns, int) else 0
    started = rec.get("started")
    job.started = started if isinstance(started, (int, float)) else time.time()
    finished = rec.get("finished")
    job.finished = finished if isinstance(finished, (int, float)) else None
    return job


def _set_session_id(job: Job, sid: Any) -> None:
    """Record the CLI's session handle, journaling the first sighting so a
    restart between init and finish does not lose the resume handle."""
    if isinstance(sid, str) and sid and sid != job.session_id:
        job.session_id = sid
        _persist_job(job)


def _load_jobs() -> None:
    """Restore job metadata from the previous process, if any. Jobs that were
    still queued or running died with it; they come back as errors with their
    session ids intact, so resume_* can still follow up on the CLI session."""
    try:
        lines = JOBS_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log(f"!! load jobs file failed: {exc}")
        return
    latest: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            rec = json.loads(line)
            if isinstance(rec, dict) and rec.get("job_id"):
                latest[rec["job_id"]] = rec
        except ValueError:
            continue
    restored = []
    for rec in latest.values():
        try:
            restored.append(_job_from_record(rec))
        except (KeyError, TypeError):
            continue
    for job in restored:
        if job.status in {"queued", "running"}:
            was = job.status
            job.status, job.finished = "error", time.time()
            job.error = (f"locum restarted while this job was {was}; "
                         "it did not survive the restart")
    restored.sort(key=lambda j: j.started, reverse=True)
    # Insert oldest-first: the registry reads newest-first off insertion order.
    for job in reversed(restored[:MAX_JOBS]):
        JOBS[job.id] = job
    if restored:
        _log(f".. restored {len(JOBS)} jobs from {JOBS_FILE}")
        _rewrite_jobs_file()


_load_jobs()

# ------------------------------------------------------------ completion ----

def _deliver_webhook(payload: dict[str, Any]) -> None:
    """POST one completion payload to the operator's own routine. This is the
    single permitted outbound call in the server (see the CI allowlist): the
    target is LOCUM_COMPLETION_WEBHOOK, never a vendor API, and the payload
    carries no credentials. Best-effort; failures log and move on."""
    req = urllib.request.Request(
        WEBHOOK_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 # Cloudflare 1010-bans the default Python-urllib signature.
                 "User-Agent": f"locum/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # webhook-allowlist
            resp.read(1024)
    except Exception as exc:                      # noqa: BLE001
        _log(f"!! completion webhook failed: {type(exc).__name__}: {exc}")


async def _fire_webhook(job: Job) -> None:
    """Deliver one completion payload off the event loop, so a slow or dead
    endpoint never stalls job completion. The payload mirrors check_job, so
    the orchestrator can wake up and call check_job exactly once."""
    await asyncio.to_thread(_deliver_webhook, _snapshot(job))

# --------------------------------------------------------------- runners ----

async def _pump_stderr(stream: asyncio.StreamReader, job: Job) -> None:
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            job.stderr_tail.append(line)


def _note_claude_event(job: Job, evt: dict[str, Any]) -> None:
    kind = evt.get("type")
    if kind == "system" and evt.get("subtype") == "init":
        _set_session_id(job, evt.get("session_id"))
    elif kind == "assistant":
        job.turns += 1
        for block in (evt.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "tool_use":
                inp = block.get("input") or {}
                detail = inp.get("file_path") or inp.get("path") or inp.get("command") or ""
                _activity(job, f"{block.get('name')} {str(detail)[:90]}",
                          kind="tool", detail=json.dumps(inp)[:1500])
            elif btype == "thinking":
                text = block.get("thinking") or ""
                if text:
                    _activity(job, "thinking", kind="thinking", detail=text[:4000])
            elif btype == "text":
                text = block.get("text") or ""
                if text.strip():
                    _activity(job, "message", kind="text", detail=text[:4000])
    elif kind == "user":
        for block in (evt.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_result":
                out = block.get("content")
                if isinstance(out, list):
                    out = " ".join(b.get("text", "") for b in out if isinstance(b, dict))
                if isinstance(out, str) and out.strip():
                    _activity(job, "result", kind="result", detail=out[:2000])
    elif kind == "result":
        _set_session_id(job, evt.get("session_id"))
        job.cost_usd = evt.get("total_cost_usd")
        usage = evt.get("usage") if isinstance(evt.get("usage"), dict) else {}
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            if isinstance(usage.get(k), int):
                job.tokens[k] = usage[k]
        if evt.get("is_error"):
            job.status, job.error = "error", str(evt.get("result") or "claude reported is_error")
        else:
            job.status, job.result = "done", evt.get("result") or ""


def _note_codex_event(job: Job, evt: Any) -> None:
    """Parse Codex's --json stream.

    Current events are thread.started / turn.started / item.completed /
    turn.completed with detail nested under `item`. Older releases used a flat
    `msg` envelope with different names; both are handled.

    Every field is type-checked before use. A stream carrying a bare string, a
    number, or a list is valid JSON, and an unguarded .get() on one takes down
    the stdout reader, which used to mean the child was never reaped and the
    Codex output file was never cleaned up.
    """
    if not isinstance(evt, dict):
        _activity(job, str(evt)[:90])
        return

    msg = evt.get("msg") if isinstance(evt.get("msg"), dict) else evt
    label = msg.get("type") or evt.get("type") or ""
    if not isinstance(label, str):
        label = str(label)

    # Read identifiers from whichever envelope carries them.
    for src in (evt, msg):
        found = src.get("thread_id") or src.get("session_id") or src.get("conversation_id")
        _set_session_id(job, found)

    # Counting both vocabularies double-counts an old-protocol turn, since
    # agent_message and task_complete both fire for the same turn.
    job.seen_labels.add(label)

    if label == "turn.completed":
        job.turns += 1
        usage = evt.get("usage") if isinstance(evt.get("usage"), dict) else {}
        for k, v in usage.items():
            if isinstance(v, int):
                job.tokens[k] = job.tokens.get(k, 0) + v
        return
    # In the old vocabulary the per-turn marker is agent_message; task_complete
    # fires once for the whole exec, so counting it too inflated every turn
    # count by one.
    if label == "agent_message" and "turn.completed" not in job.seen_labels:
        job.turns += 1
        return
    if label == "task_complete":
        return

    # Record the failure but leave the status alone. Codex can emit a transient
    # error and carry on; flipping the job out of "running" here would break
    # cancel_job, let _prune_jobs evict a live job, and mask a later success.
    # _run decides the final status once the process actually exits.
    if label in {"error", "turn.failed"}:
        err = evt.get("error") if isinstance(evt.get("error"), dict) else msg.get("error")
        detail = (evt.get("message") or msg.get("message")
                  or (err.get("message") if isinstance(err, dict) else None)
                  or (err if isinstance(err, str) else None)
                  or label)
        job.error = str(detail)[:600]
        _activity(job, f"error {str(detail)[:80]}")
        return

    if label == "item.completed":
        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        itype = item.get("type") or "item"
        detail = (item.get("command") or item.get("path")
                  or item.get("text") or item.get("title") or "")
        ekind = {"reasoning": "thinking", "agent_message": "text"}.get(itype, "tool")
        _activity(job, f"{itype} {str(detail)[:90]}", kind=ekind, detail=str(detail)[:4000])
        return

    # item.started / item.updated fire constantly and say nothing item.completed
    # does not; without this they crowd everything useful out of the deque.
    if label and label not in {"token_count", "agent_message_delta", "turn.started",
                               "thread.started", "item.started", "item.updated"}:
        _activity(job, label[:90])


def _child_env() -> dict[str, str]:
    """Environment for the spawned CLI.

    Claude Code exports session-scoped plumbing into every child process:
    CLAUDECODE, a CLAUDE_CODE_* family, and ANTHROPIC_BASE_URL. A `claude`
    that inherits those believes it is a nested child session and tries to
    delegate auth to a host socket that is not listening, dying with
    "Failed to authenticate: OAuth session expired and could not be refreshed".

    Strip them, but only when nesting is actually detected, so that a
    deliberately set ANTHROPIC_BASE_URL still works outside Claude Code.
    """
    env = {**os.environ, "CI": "1", "NO_COLOR": "1"}
    if not env.get("CLAUDECODE"):
        return env

    exact = {"CLAUDECODE", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
             "CLAUDE_EFFORT", "CLAUDE_PID"}
    prefixes = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")

    # CLAUDE_CODE_OAUTH_TOKEN matches the prefix above but is the opposite of
    # session plumbing: it is a long-lived token the operator minted with
    # `claude setup-token` so the CLI can authenticate without a login session.
    # Stripping it would break exactly the headless case it exists for, and the
    # failure looks identical to the nesting bug this function fixes.
    keep = {"CLAUDE_CODE_OAUTH_TOKEN"}

    return {
        k: v for k, v in env.items()
        if k in keep or (k not in exact and not k.startswith(prefixes))
    }


async def _run(job: Job, argv: list[str], on_event, finalize=None) -> None:
    # The semaphore sits inside the try so that cancelling a queued job lands
    # in the CancelledError branch instead of escaping with the job stuck at
    # "queued" forever. The timeout still only covers the gather below, so
    # queue waiting never eats into LOCUM_JOB_TIMEOUT.
    try:
        async with SEM:
            job.status = "running"
            _broadcast({"type": "job", "job": _brief(job)})
            _persist_job(job)
            waited = time.time() - job.started
            if waited > 1:
                _log(f"~~ {job.kind}  {job.id}  started after {waited:.0f}s queued")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                limit=32 * 1024 * 1024,   # stream-json lines carry whole file reads
                env=_child_env(),
            )
            job.proc = proc

            async def read_stdout() -> None:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        on_event(job, json.loads(line))
                    except json.JSONDecodeError:
                        _activity(job, line[:90])
                    except Exception as exc:            # noqa: BLE001
                        # Never let a parser bug escape into the gather: that
                        # kills the reader, and _run then skips proc.wait() and
                        # finalize, leaving the child unreaped and the Codex
                        # output file on disk. Losing one event beats leaking.
                        _activity(job, f"unparsed event ({type(exc).__name__})")

            await asyncio.wait_for(
                asyncio.gather(read_stdout(), _pump_stderr(proc.stderr, job)),
                timeout=JOB_TIMEOUT,
            )
            rc = await proc.wait()

            if finalize:
                finalize(job)
            if job.status == "running":
                if rc != 0:
                    job.status = "error"
                    job.error = job.error or f"{job.kind} exited {rc}"
                elif job.error:
                    # Exit code 0 with a reported failure: Codex does this when
                    # a turn fails, and trusting rc alone reports it as done.
                    job.status = "error"
                else:
                    job.status = "done"
                    job.result = job.result or "(process exited 0 with no captured result)"
    except asyncio.TimeoutError:
        job.status = "timeout"
        job.error = f"exceeded LOCUM_JOB_TIMEOUT ({JOB_TIMEOUT}s)"
        if job.proc and job.proc.returncode is None:
            job.proc.kill()
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = "cancelled by cancel_job"
        if job.proc and job.proc.returncode is None:
            job.proc.kill()
    except Exception as exc:                      # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished = time.time()
        took = f"{job.finished - job.started:.1f}s"
        cost = f" - ${job.cost_usd:.2f}" if job.cost_usd else ""
        mark = "ok" if job.status == "done" else "!!"
        _broadcast({"type": "job", "job": _brief(job)})
        _persist_job(job)
        # Fire-and-forget: the job is already terminal, and a slow endpoint
        # must not stall completion. Cancelled jobs stay silent; the caller
        # that cancelled them is already awake.
        if WEBHOOK_URL and job.status in {"done", "error", "timeout"}:
            asyncio.create_task(_fire_webhook(job))
        _log(f"{mark} {job.kind}  {job.id}  {job.status} - "
             f"{job.turns} turns - {took}{cost}")


def _prune_jobs() -> None:
    """Jobs live in memory for the process lifetime. Under launchd the process
    can run for weeks, so drop the oldest finished ones once the history grows
    past MAX_JOBS. Queued and running jobs are never dropped: their handle is
    the only way the client can reach them again."""
    if len(JOBS) <= MAX_JOBS:
        return
    finished = [jid for jid, j in JOBS.items() if j.status not in {"queued", "running"}]
    for jid in finished[: len(JOBS) - MAX_JOBS]:
        JOBS.pop(jid, None)
    _rewrite_jobs_file()


def _spawn(job: Job, argv: list[str], on_event, finalize=None) -> dict[str, Any]:
    JOBS[job.id] = job
    _persist_job(job)
    _prune_jobs()
    tuning = "/".join(x for x in (job.model, job.effort) if x) or "defaults"
    _broadcast({"type": "job", "job": _brief(job)})
    _log(f"-> {job.kind}  {job.id}  {tuning}  {job.cwd}")
    _log(f'     "{" ".join(job.prompt.split())[:110]}"')
    task = asyncio.create_task(_run(job, argv, on_event, finalize))
    job.proc = job.proc or None
    setattr(job, "_task", task)
    return {
        "job_id": job.id,
        "status": "queued",
        "cwd": job.cwd,
        "next_step": f"Call check_job('{job.id}') in about 30s. Do NOT re-delegate; "
                     "the work is already queued.",
    }

# ------------------------------------------------------------------ tools ----

mcp = FastMCP(
    name="locum",
    instructions=(
        "Delegates coding work to the operator's own local Claude Code and Codex "
        "CLIs. Prefer these tools over doing multi-file code work yourself: they "
        "run on the operator's machine with full repo context.\n\n"
        "All work is ASYNCHRONOUS. delegate_* returns a job_id immediately; poll "
        "check_job until status is 'done'. Never re-delegate a job that is still "
        "running.\n\n"
        "Give ONE self-contained, goal-shaped prompt (what done looks like, which "
        "files, how to verify). The local agent cannot ask you follow-up questions.\n\n"
        f"Allowed cwd roots: {', '.join(str(r) for r in ROOTS)}"
    ),
)


@mcp.tool
async def delegate_to_claude(prompt: str, cwd: str | None = None,
                             model: str | None = None,
                             effort: str | None = None) -> dict:
    """Hand a coding task to the operator's local Claude Code. Returns a job_id
    immediately; poll check_job for the result.

    Args:
        prompt: One self-contained instruction. Say what "done" means and how to verify.
        cwd: Absolute path to the repo. Must sit under an allowed root.
        model: Optional. Claude aliases: "opus" (deepest), "sonnet" (fast, the
            usual choice), "fable". Omit for the operator's default.
        effort: Optional reasoning depth: "low", "medium", "high", or "max".
            Raise it for architecture, tricky debugging, or anything needing
            care; leave it off for mechanical edits. Costs more time and tokens.
    """
    effort = _effort(effort)
    argv = [_require("claude"), "-p", prompt,
            "--output-format", "stream-json", "--verbose"] + _claude_autonomy()
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    job = Job(id=uuid.uuid4().hex[:12], kind="claude", prompt=prompt,
              cwd=_resolve_cwd(cwd), model=model, effort=effort)
    return _spawn(job, argv, _note_claude_event)


@mcp.tool
async def resume_claude(session_id: str, prompt: str, cwd: str | None = None,
                        model: str | None = None,
                        effort: str | None = None) -> dict:
    """Continue an earlier Claude Code session instead of starting cold. Much
    cheaper and much faster - it reuses the prompt cache and keeps prior context.
    Always prefer this over delegate_to_claude for follow-ups.

    Args:
        session_id: The session_id returned by a previous check_job.
        prompt: The follow-up instruction.
        cwd: Same repo as the original job.
        model: Optional override for this turn only.
        effort: Optional reasoning depth for this turn only. Useful to start
            cheap and escalate when the follow-up is the hard part.
    """
    effort = _effort(effort)
    argv = [_require("claude"), "-p", prompt, "--resume", session_id,
            "--output-format", "stream-json", "--verbose"] + _claude_autonomy()
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    job = Job(id=uuid.uuid4().hex[:12], kind="claude", prompt=prompt,
              cwd=_resolve_cwd(cwd), model=model, effort=effort)
    job.session_id = session_id
    return _spawn(job, argv, _note_claude_event)


@mcp.tool
async def delegate_to_codex(prompt: str, cwd: str | None = None,
                            model: str | None = None,
                            effort: str | None = None) -> dict:
    """Hand a coding task to the operator's local Codex CLI. Same async contract
    as delegate_to_claude. Useful as a second opinion or when Claude's weekly
    limit is exhausted.

    Args:
        prompt: One self-contained instruction.
        cwd: Absolute path to the repo. Must sit under an allowed root.
        model: Optional model override.
        effort: Optional reasoning depth: "low", "medium", "high", or "max".
            Codex has no distinct "max", so it maps onto "high".
    """
    effort = _effort(effort)
    resolved = _resolve_cwd(cwd)
    outfile = Path(resolved) / f".codex-last-{uuid.uuid4().hex[:8]}.txt"
    argv = [_require("codex"), "exec", prompt, "--json",
            "--cd", resolved, "--skip-git-repo-check",
            "--output-last-message", str(outfile)] + _codex_autonomy()
    if model:
        argv += ["--model", model]
    if effort:
        # Codex exposes reasoning depth as a config override, not a flag, and
        # -c has to precede the subcommand.
        argv[1:1] = ["-c", f'model_reasoning_effort="{CODEX_EFFORT[effort]}"']

    def finalize(j: Job) -> None:
        _finalize_codex_output(j, outfile)

    job = Job(id=uuid.uuid4().hex[:12], kind="codex", prompt=prompt, cwd=resolved,
              model=model, effort=effort)
    return _spawn(job, argv, _note_codex_event, finalize)


def _finalize_codex_output(job: Job, outfile: Path,
                           expect_thread: str | None = None) -> None:
    """Collect a finished Codex run: the last message from its output file.

    Shared by delegate_to_codex and resume_codex. When expect_thread is set,
    the stream must have reported that same thread id back; a bad resume id
    can silently start a fresh thread, so a mismatch fails loudly rather than
    reporting a cold run as a follow-up.
    """
    try:
        if outfile.exists():
            job.result = outfile.read_text("utf-8", "replace").strip()
            if job.status == "running":
                job.status = "done"
            outfile.unlink(missing_ok=True)
    except OSError as exc:
        job.error = f"could not read codex output: {exc}"
    if expect_thread is not None and job.session_id != expect_thread:
        job.status = "error"
        job.error = (
            f"resume target mismatch: asked for thread {expect_thread!r}, "
            f"codex reported {job.session_id!r}. Refusing to report a "
            f"fresh thread as a follow-up."
        )


@mcp.tool
async def resume_codex(session_id: str, prompt: str, cwd: str | None = None,
                       model: str | None = None,
                       effort: str | None = None) -> dict:
    """Continue an earlier Codex session instead of starting cold. The twin of
    resume_claude: it keeps prior context rather than re-paying setup. Always
    prefer this over delegate_to_codex for follow-ups.

    Fails loudly if Codex reports back a different thread id than the one
    asked for, which is how a bad id silently starting a fresh thread shows up.

    Args:
        session_id: The session_id returned by a previous check_job.
        prompt: The follow-up instruction.
        cwd: Same repo as the original job.
        model: Optional override for this turn only.
        effort: Optional reasoning depth for this turn only. Codex has no
            distinct "max", so it maps onto "high".
    """
    effort = _effort(effort)
    resolved = _resolve_cwd(cwd)
    outfile = Path(resolved) / f".codex-last-{uuid.uuid4().hex[:8]}.txt"
    # `codex exec resume` takes [SESSION_ID] [PROMPT] positionally and accepts
    # no --cd, so the working directory comes from the subprocess cwd alone.
    argv = [_require("codex"), "exec", "resume", session_id, prompt, "--json",
            "--skip-git-repo-check",
            "--output-last-message", str(outfile)] + _codex_autonomy(resume=True)
    if model:
        argv += ["--model", model]
    if effort:
        argv[1:1] = ["-c", f'model_reasoning_effort="{CODEX_EFFORT[effort]}"']

    def finalize(j: Job) -> None:
        _finalize_codex_output(j, outfile, expect_thread=session_id)

    job = Job(id=uuid.uuid4().hex[:12], kind="codex", prompt=prompt, cwd=resolved,
              model=model, effort=effort)
    job.session_id = session_id
    return _spawn(job, argv, _note_codex_event, finalize)


@mcp.tool
async def check_job(job_id: str) -> dict:
    """Poll a delegated job. status is one of: queued, running, done, error,
    timeout, cancelled. A queued job is waiting for a free slot; while running
    you get turn count and recent tool activity so you can report progress.
    Poll every 20-30s; do not busy-loop.

    Args:
        job_id: The job_id returned by a delegate_* tool.
    """
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id!r}")
    return _snapshot(job)


@mcp.tool
async def list_jobs(limit: int = 10, status: str | None = None) -> dict:
    """Recent delegated jobs, newest first.

    Use it to confirm work actually ran on the operator's machine, to recover a
    job_id you lost track of, or to find a session_id worth resuming. Prompts
    and results are truncated here; call check_job for a job's full result.

    Args:
        limit: How many to return, newest first. Default 10, capped at 50.
        status: Optional filter. One of queued, running, done, error, timeout, cancelled.
    """
    valid = {"queued", "running", "done", "error", "timeout", "cancelled"}
    if status is not None and status not in valid:
        raise ValueError(f"status must be one of {sorted(valid)}, got {status!r}")

    limit = max(1, min(int(limit), 50))
    # dicts preserve insertion order, so reversing gives newest first.
    jobs = [j for j in reversed(JOBS.values()) if status is None or j.status == status]

    return {
        "returned": len(jobs[:limit]),
        "matching": len(jobs),
        "total_in_memory": len(JOBS),
        "jobs": [_brief(j) for j in jobs[:limit]],
    }


@mcp.tool
async def cancel_job(job_id: str) -> dict:
    """Kill a queued or running job that has gone off the rails or is no longer
    needed.

    Args:
        job_id: The job_id to cancel.
    """
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id!r}")
    task = getattr(job, "_task", None)
    if job.status in {"queued", "running"} and task:
        task.cancel()
        return {"job_id": job_id, "status": "cancelling"}
    return {"job_id": job_id, "status": job.status, "note": "job was not queued or running"}

# ------------------------------------------------------------------- auth ----
# Grok Bot's custom-connector dialog only speaks OAuth 2.1 -- it offers no static
# header field. So the bridge ships a minimal, single-operator authorization
# server: discovery metadata, dynamic client registration, a PKCE authorization
# code flow, and a consent screen gated on LOCUM_TOKEN.
#
# The passphrase gate is load-bearing. /authorize sits on a public tunnel;
# without it, anyone who learned the URL could mint a token for themselves and
# get shell access to this machine.

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass
class AuthCode:
    challenge: str
    redirect_uri: str
    expires: float


CODES: dict[str, AuthCode] = {}
ACCESS: dict[str, float] = {}          # token -> expiry
REFRESH: set[str] = set()
CLIENTS: dict[str, dict[str, Any]] = {}
TOKEN_TTL = 30 * 24 * 3600

CONSENT_PAGE = """<!doctype html><meta charset=utf-8>
<title>locum - authorize</title>
<style>
 body{{background:#0b0b0d;color:#e7e7ea;font:15px/1.55 -apple-system,system-ui,sans-serif;
      display:grid;place-items:center;min-height:100vh;margin:0}}
 .c{{width:min(440px,90vw);background:#141417;border:1px solid #2a2a30;border-radius:14px;padding:28px}}
 h1{{font-size:17px;margin:0 0 4px}} p{{color:#9a9aa4;margin:0 0 18px;font-size:13px}}
 dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:12.5px;margin:0 0 20px}}
 dt{{color:#7a7a85}} dd{{margin:0;word-break:break-all;font-family:ui-monospace,monospace}}
 input{{width:100%;box-sizing:border-box;background:#0b0b0d;border:1px solid #33333b;color:#e7e7ea;
        border-radius:9px;padding:11px 13px;font:inherit;margin:0 0 12px}}
 button{{width:100%;background:#e7e7ea;color:#0b0b0d;border:0;border-radius:9px;
         padding:11px;font:600 15px/1 inherit;cursor:pointer}}
 .e{{color:#ff8f8f;font-size:13px;margin:0 0 12px}}
</style>
<div class=c>
 <h1>Authorize locum</h1>
 <p>This grants shell-level access to your allowed workspace roots. Check the
    redirect target below before approving.</p>
 <dl><dt>client</dt><dd>{client}</dd>
     <dt>redirect</dt><dd>{redirect}</dd>
     <dt>roots</dt><dd>{roots}</dd></dl>
 {error}
 <form method=post>
  {hidden}
  <input type=password name=passphrase placeholder="LOCUM_TOKEN" autofocus required>
  <button type=submit>Approve</button>
 </form>
</div>"""


class AuthGateway:
    """Wraps the MCP app: OAuth endpoints in front, bearer enforcement behind."""

    def __init__(self, app, token: str) -> None:
        self.app, self.token = app, token

    # -- plumbing ---------------------------------------------------------
    @staticmethod
    async def _send(send, status: int, body: bytes, ctype: str, extra=()) -> None:
        headers = [(b"content-type", ctype.encode()),
                   (b"content-length", str(len(body)).encode()),
                   (b"cache-control", b"no-store")]
        headers.extend(extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send, status: int, payload: dict, extra=()) -> None:
        await self._send(send, status, json.dumps(payload).encode(), "application/json", extra)

    @staticmethod
    async def _body(receive) -> bytes:
        buf, more = b"", True
        while more:
            msg = await receive()
            buf += msg.get("body", b"")
            more = msg.get("more_body", False)
        return buf

    @staticmethod
    def _base(scope) -> str:
        if forced := os.environ.get("LOCUM_PUBLIC_URL"):
            return forced.rstrip("/")
        headers = dict(scope.get("headers") or {})
        host = headers.get(b"host", b"localhost").decode()
        proto = headers.get(b"x-forwarded-proto", b"").decode() or (
            "http" if host.startswith(("localhost", "127.")) else "https")
        return f"{proto}://{host}"

    # -- routing ----------------------------------------------------------
    # --- dashboard ---------------------------------------------------------
    def _cookie(self) -> str:
        """A cookie value derived from the token, so no second secret exists and
        revoking LOCUM_TOKEN revokes dashboard access at the same moment."""
        return hashlib.sha256(f"locum-dash:{self.token}".encode()).hexdigest()

    def _has_cookie(self, scope) -> bool:
        raw = dict(scope.get("headers") or {}).get(b"cookie", b"").decode()
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "locum_dash" and hmac.compare_digest(v, self._cookie()):
                return True
        return False

    async def _dashboard(self, scope, receive, send, method: str):
        if method == "POST":
            form = dict(parse_qsl((await self._body(receive)).decode()))
            if hmac.compare_digest(form.get("passphrase", ""), self.token):
                return await self._send(
                    send, 303, b"", "text/plain",
                    extra=[(b"location", b"/dashboard"),
                           (b"set-cookie",
                            f"locum_dash={self._cookie()}; Path=/; HttpOnly; "
                            f"SameSite=Lax; Max-Age=604800".encode())])
            page = dashboard.LOGIN.format(error='<p class="err">Wrong token.</p>')
            return await self._send(send, 401, page.encode(), "text/html; charset=utf-8")

        if not self._has_cookie(scope):
            page = dashboard.LOGIN.format(error="")
            return await self._send(send, 200, page.encode(), "text/html; charset=utf-8")
        return await self._send(send, 200, dashboard.PAGE.encode(), "text/html; charset=utf-8")

    async def _api(self, scope, send, path: str):
        if not self._has_cookie(scope):
            return await self._json(send, 401, {"error": "unauthorized"})

        if path == "/api/jobs":
            jobs = [_brief(j) for j in reversed(JOBS.values())]
            return await self._json(send, 200, {"jobs": jobs})

        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return await self._json(send, 404, {"error": "unknown job"})
            out = _brief(job)
            out["prompt_full"] = job.prompt
            out["events"] = [e.as_dict() for e in job.events]
            out["result"] = job.result
            out["stderr_tail"] = list(job.stderr_tail)
            return await self._json(send, 200, out)

        return await self._json(send, 404, {"error": "not found"})

    async def _stream(self, scope, send):
        """Server-sent events. A bounded queue means a browser that stops
        reading gets dropped events rather than stalling the agent."""
        if not self._has_cookie(scope):
            return await self._json(send, 401, {"error": "unauthorized"})
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        SUBSCRIBERS.add(q)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache, no-store"),
                                # Cloudflare and nginx both buffer a response
                                # until it looks complete unless told not to,
                                # which stalls a stream indefinitely.
                                (b"x-accel-buffering", b"no"),
                                (b"connection", b"keep-alive")]})
        try:
            # Flush a byte immediately. A proxy may hold the headers until some
            # body arrives, and until they arrive the browser never fires
            # EventSource.onopen, so the page sits on "connecting" forever.
            await send({"type": "http.response.body",
                        "body": b": open\n\n", "more_body": True})
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    body = f"data: {json.dumps(payload)}\n\n".encode()
                except asyncio.TimeoutError:
                    body = b": keepalive\n\n"   # keeps proxies from closing it
                await send({"type": "http.response.body", "body": body, "more_body": True})
        except Exception:                          # noqa: BLE001  client vanished
            pass
        finally:
            SUBSCRIBERS.discard(q)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path, method = scope.get("path", ""), scope.get("method", "GET")
        base = self._base(scope)

        if path == "/health":
            return await self._json(send, 200, {"ok": True})

        # Browsers request this unprompted on every page. Answering 401 puts a
        # red error in the console of an otherwise healthy dashboard.
        if path == "/favicon.ico":
            return await self._send(send, 204, b"", "image/x-icon")

        if path == "/dashboard":
            return await self._dashboard(scope, receive, send, method)
        if path == "/api/stream":
            return await self._stream(scope, send)
        if path.startswith("/api/"):
            return await self._api(scope, send, path)

        # Clients probe both the bare and resource-suffixed discovery paths.
        if path.startswith("/.well-known/oauth-protected-resource"):
            return await self._json(send, 200, {
                "resource": f"{base}/mcp",
                "authorization_servers": [base],
                "scopes_supported": ["mcp"],
                "bearer_methods_supported": ["header"],
            })

        if path.startswith(("/.well-known/oauth-authorization-server",
                            "/.well-known/openid-configuration")):
            return await self._json(send, 200, {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
                "scopes_supported": ["mcp"],
            })

        # RFC 7591 dynamic registration, so Grok can self-register rather than
        # making the operator hand-copy a client id.
        if path == "/register" and method == "POST":
            try:
                req = json.loads(await self._body(receive) or b"{}")
            except json.JSONDecodeError:
                req = {}
            cid = f"locum-{secrets.token_hex(8)}"
            CLIENTS[cid] = req
            return await self._json(send, 201, {
                "client_id": cid,
                "client_id_issued_at": int(time.time()),
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "redirect_uris": req.get("redirect_uris", []),
                "client_name": req.get("client_name", "locum client"),
            })

        if path == "/authorize":
            return await self._authorize(scope, receive, send, method)

        if path == "/token" and method == "POST":
            return await self._token(receive, send)

        header = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
        if not self._authorized(header):
            return await self._json(
                send, 401, {"error": "unauthorized"},
                extra=[(b"www-authenticate",
                        f'Bearer realm="locum", '
                        f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
                        .encode())])
        return await self.app(scope, receive, send)

    def _authorized(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        presented = header[7:]
        # The static token stays valid: it is the same secret that gates consent,
        # and it keeps curl smoke tests working.
        if hmac.compare_digest(presented, self.token):
            return True
        expiry = ACCESS.get(presented)
        return bool(expiry and expiry > time.time())

    async def _authorize(self, scope, receive, send, method: str):
        params = dict(parse_qsl(scope.get("query_string", b"").decode()))
        error = ""

        if method == "POST":
            form = dict(parse_qsl((await self._body(receive)).decode()))
            params = {**params, **form}
            if hmac.compare_digest(form.get("passphrase", ""), self.token):
                redirect_uri = params.get("redirect_uri", "")
                code = secrets.token_urlsafe(32)
                CODES[code] = AuthCode(challenge=params.get("code_challenge", ""),
                                       redirect_uri=redirect_uri,
                                       expires=time.time() + 120)
                sep = "&" if "?" in redirect_uri else "?"
                target = f"{redirect_uri}{sep}code={quote(code)}"
                if state := params.get("state"):
                    target += f"&state={quote(state)}"
                return await self._send(send, 302, b"", "text/plain",
                                        extra=[(b"location", target.encode())])
            error = '<p class="e">Wrong passphrase. It is LOCUM_TOKEN from your .env.</p>'

        if params.get("code_challenge_method", "S256") != "S256":
            return await self._json(send, 400, {"error": "invalid_request",
                                                "error_description": "S256 PKCE required"})

        hidden = "".join(
            f'<input type=hidden name="{escape(k, True)}" value="{escape(v, True)}">'
            for k, v in params.items() if k != "passphrase")
        page = CONSENT_PAGE.format(
            client=escape(params.get("client_id", "(none)")),
            redirect=escape(params.get("redirect_uri", "(none)")),
            roots=escape(", ".join(str(r) for r in ROOTS)),
            error=error, hidden=hidden)
        return await self._send(send, 200, page.encode(), "text/html; charset=utf-8")

    async def _token(self, receive, send):
        form = dict(parse_qsl((await self._body(receive)).decode()))
        grant = form.get("grant_type")

        if grant == "refresh_token":
            if form.get("refresh_token") not in REFRESH:
                return await self._json(send, 400, {"error": "invalid_grant"})
        elif grant == "authorization_code":
            entry = CODES.pop(form.get("code", ""), None)
            if not entry or entry.expires < time.time():
                return await self._json(send, 400, {"error": "invalid_grant",
                                                    "error_description": "code expired or unknown"})
            digest = _b64u(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if not entry.challenge or not hmac.compare_digest(digest, entry.challenge):
                return await self._json(send, 400, {"error": "invalid_grant",
                                                    "error_description": "PKCE verification failed"})
        else:
            return await self._json(send, 400, {"error": "unsupported_grant_type"})

        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        ACCESS[access] = time.time() + TOKEN_TTL
        REFRESH.add(refresh)
        for tok, exp in list(ACCESS.items()):          # opportunistic sweep
            if exp < time.time():
                ACCESS.pop(tok, None)
        return await self._json(send, 200, {
            "access_token": access, "token_type": "Bearer",
            "expires_in": TOKEN_TTL, "refresh_token": refresh, "scope": "mcp",
        })


if __name__ == "__main__":
    import uvicorn

    print(f"locum  http://{HOST}:{PORT}/mcp")
    print(f"  roots           : {', '.join(str(r) for r in ROOTS)}")
    print(f"  permission mode : {PERMISSION_MODE}")
    print(f"  job timeout     : {JOB_TIMEOUT}s, max concurrent {MAX_CONCURRENT}")
    print("  oauth           : /authorize /token /register + discovery")
    uvicorn.run(AuthGateway(mcp.http_app(path="/mcp"), TOKEN),
                host=HOST, port=PORT, log_level="info")
