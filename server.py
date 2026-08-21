#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2.10", "uvicorn>=0.30"]
# ///
"""
grok-bridge - expose the Claude Code and Codex CLIs you are ALREADY logged into
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
import secrets
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

from fastmcp import FastMCP

# ---------------------------------------------------------------- config ----

def _roots() -> list[Path]:
    raw = os.environ.get("GROK_BRIDGE_ROOTS", str(Path.home() / "Documents" / "Projects"))
    out = []
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if chunk:
            out.append(Path(chunk).expanduser().resolve())
    if not out:
        raise SystemExit("GROK_BRIDGE_ROOTS resolved to nothing")
    return out

ROOTS = _roots()
TOKEN = os.environ.get("GROK_BRIDGE_TOKEN", "")
HOST = os.environ.get("GROK_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("GROK_BRIDGE_PORT", "8791"))
PERMISSION_MODE = os.environ.get("GROK_BRIDGE_PERMISSION_MODE", "acceptEdits")
JOB_TIMEOUT = int(os.environ.get("GROK_BRIDGE_JOB_TIMEOUT", "1800"))
MAX_CONCURRENT = int(os.environ.get("GROK_BRIDGE_MAX_CONCURRENT", "2"))

if not TOKEN:
    raise SystemExit(
        "GROK_BRIDGE_TOKEN is not set. Pick one and keep it stable -- the Grok\n"
        "connector stores it, so regenerating means re-registering.\n\n"
        f'  export GROK_BRIDGE_TOKEN="{secrets.token_urlsafe(32)}"\n'
    )

# ------------------------------------------------------------------ jobs ----

@dataclass
class Job:
    id: str
    kind: str                       # "claude" | "codex"
    prompt: str
    cwd: str
    status: str = "running"         # running | done | error | timeout | cancelled
    session_id: str | None = None
    result: str | None = None
    error: str | None = None
    turns: int = 0
    cost_usd: float | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    activity: deque[str] = field(default_factory=lambda: deque(maxlen=14))
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
    if job.session_id:
        out["session_id"] = job.session_id
    if job.status == "done":
        out["result"] = job.result
        if job.cost_usd is not None:
            out["reported_cost_usd"] = job.cost_usd
    if job.status in {"error", "timeout"}:
        out["error"] = job.error
        out["stderr_tail"] = list(job.stderr_tail)
    if job.status == "running":
        out["hint"] = "Still working. Poll check_job again in 20-30s."
    return out

# --------------------------------------------------------------- runners ----

async def _pump_stderr(stream: asyncio.StreamReader, job: Job) -> None:
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            job.stderr_tail.append(line)


def _note_claude_event(job: Job, evt: dict[str, Any]) -> None:
    kind = evt.get("type")
    if kind == "system" and evt.get("subtype") == "init":
        job.session_id = evt.get("session_id") or job.session_id
    elif kind == "assistant":
        job.turns += 1
        for block in (evt.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                inp = block.get("input") or {}
                detail = inp.get("file_path") or inp.get("path") or inp.get("command") or ""
                job.activity.append(f"{block.get('name')} {str(detail)[:90]}".strip())
    elif kind == "result":
        job.session_id = evt.get("session_id") or job.session_id
        job.cost_usd = evt.get("total_cost_usd")
        if evt.get("is_error"):
            job.status, job.error = "error", str(evt.get("result") or "claude reported is_error")
        else:
            job.status, job.result = "done", evt.get("result") or ""


def _note_codex_event(job: Job, evt: dict[str, Any]) -> None:
    # Codex's --json event shape shifts between releases, so stay tolerant:
    # record whatever looks like a type, and take the real answer from the
    # --output-last-message file once the process exits.
    msg = evt.get("msg") if isinstance(evt.get("msg"), dict) else evt
    label = msg.get("type") or evt.get("type")
    if label and label not in {"token_count", "agent_message_delta"}:
        job.activity.append(str(label)[:90])
    if label in {"agent_message", "task_complete"}:
        job.turns += 1
    job.session_id = evt.get("session_id") or evt.get("conversation_id") or job.session_id


async def _run(job: Job, argv: list[str], on_event, finalize=None) -> None:
    async with SEM:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                limit=32 * 1024 * 1024,   # stream-json lines carry whole file reads
                env={**os.environ, "CI": "1", "NO_COLOR": "1"},
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
                        job.activity.append(line[:90])

            await asyncio.wait_for(
                asyncio.gather(read_stdout(), _pump_stderr(proc.stderr, job)),
                timeout=JOB_TIMEOUT,
            )
            rc = await proc.wait()

            if finalize:
                finalize(job)
            if job.status == "running":
                if rc == 0:
                    job.status = "done"
                    job.result = job.result or "(process exited 0 with no captured result)"
                else:
                    job.status = "error"
                    job.error = f"{job.kind} exited {rc}"
        except asyncio.TimeoutError:
            job.status = "timeout"
            job.error = f"exceeded GROK_BRIDGE_JOB_TIMEOUT ({JOB_TIMEOUT}s)"
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


def _spawn(job: Job, argv: list[str], on_event, finalize=None) -> dict[str, Any]:
    JOBS[job.id] = job
    task = asyncio.create_task(_run(job, argv, on_event, finalize))
    job.proc = job.proc or None
    setattr(job, "_task", task)
    return {
        "job_id": job.id,
        "status": "running",
        "cwd": job.cwd,
        "next_step": f"Call check_job('{job.id}') in about 30s. Do NOT re-delegate; "
                     "the work is already running.",
    }

# ------------------------------------------------------------------ tools ----

mcp = FastMCP(
    name="grok-bridge",
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
async def delegate_to_claude(prompt: str, cwd: str | None = None, model: str | None = None) -> dict:
    """Hand a coding task to the operator's local Claude Code. Returns a job_id
    immediately; poll check_job for the result.

    Args:
        prompt: One self-contained instruction. Say what "done" means and how to verify.
        cwd: Absolute path to the repo. Must sit under an allowed root.
        model: Optional alias, e.g. "opus" or "sonnet". Omit for the operator's default.
    """
    argv = [_require("claude"), "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", PERMISSION_MODE]
    if model:
        argv += ["--model", model]
    job = Job(id=uuid.uuid4().hex[:12], kind="claude", prompt=prompt, cwd=_resolve_cwd(cwd))
    return _spawn(job, argv, _note_claude_event)


@mcp.tool
async def resume_claude(session_id: str, prompt: str, cwd: str | None = None) -> dict:
    """Continue an earlier Claude Code session instead of starting cold. Much
    cheaper and much faster - it reuses the prompt cache and keeps prior context.
    Always prefer this over delegate_to_claude for follow-ups.

    Args:
        session_id: The session_id returned by a previous check_job.
        prompt: The follow-up instruction.
        cwd: Same repo as the original job.
    """
    argv = [_require("claude"), "-p", prompt, "--resume", session_id,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", PERMISSION_MODE]
    job = Job(id=uuid.uuid4().hex[:12], kind="claude", prompt=prompt, cwd=_resolve_cwd(cwd))
    job.session_id = session_id
    return _spawn(job, argv, _note_claude_event)


@mcp.tool
async def delegate_to_codex(prompt: str, cwd: str | None = None, model: str | None = None) -> dict:
    """Hand a coding task to the operator's local Codex CLI. Same async contract
    as delegate_to_claude. Useful as a second opinion or when Claude's weekly
    limit is exhausted.

    Args:
        prompt: One self-contained instruction.
        cwd: Absolute path to the repo. Must sit under an allowed root.
        model: Optional model override.
    """
    resolved = _resolve_cwd(cwd)
    outfile = Path(resolved) / f".codex-last-{uuid.uuid4().hex[:8]}.txt"
    argv = [_require("codex"), "exec", prompt, "--json",
            "--cd", resolved, "--skip-git-repo-check",
            "--output-last-message", str(outfile)]
    if model:
        argv += ["--model", model]

    def finalize(j: Job) -> None:
        try:
            if outfile.exists():
                j.result = outfile.read_text("utf-8", "replace").strip()
                if j.status == "running":
                    j.status = "done"
                outfile.unlink(missing_ok=True)
        except OSError as exc:
            j.error = f"could not read codex output: {exc}"

    job = Job(id=uuid.uuid4().hex[:12], kind="codex", prompt=prompt, cwd=resolved)
    return _spawn(job, argv, _note_codex_event, finalize)


@mcp.tool
async def check_job(job_id: str) -> dict:
    """Poll a delegated job. status is one of: running, done, error, timeout,
    cancelled. While running you get turn count and recent tool activity so you
    can report progress. Poll every 20-30s; do not busy-loop.

    Args:
        job_id: The job_id returned by a delegate_* tool.
    """
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id!r}")
    return _snapshot(job)


@mcp.tool
async def cancel_job(job_id: str) -> dict:
    """Kill a running job that has gone off the rails or is no longer needed.

    Args:
        job_id: The job_id to cancel.
    """
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id!r}")
    task = getattr(job, "_task", None)
    if job.status == "running" and task:
        task.cancel()
        return {"job_id": job_id, "status": "cancelling"}
    return {"job_id": job_id, "status": job.status, "note": "job was not running"}

# ------------------------------------------------------------------- auth ----
# Grok Bot's custom-connector dialog only speaks OAuth 2.1 -- it offers no static
# header field. So the bridge ships a minimal, single-operator authorization
# server: discovery metadata, dynamic client registration, a PKCE authorization
# code flow, and a consent screen gated on GROK_BRIDGE_TOKEN.
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
<title>grok-bridge - authorize</title>
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
 <h1>Authorize grok-bridge</h1>
 <p>This grants shell-level access to your allowed workspace roots. Check the
    redirect target below before approving.</p>
 <dl><dt>client</dt><dd>{client}</dd>
     <dt>redirect</dt><dd>{redirect}</dd>
     <dt>roots</dt><dd>{roots}</dd></dl>
 {error}
 <form method=post>
  {hidden}
  <input type=password name=passphrase placeholder="GROK_BRIDGE_TOKEN" autofocus required>
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
        if forced := os.environ.get("GROK_BRIDGE_PUBLIC_URL"):
            return forced.rstrip("/")
        headers = dict(scope.get("headers") or {})
        host = headers.get(b"host", b"localhost").decode()
        proto = headers.get(b"x-forwarded-proto", b"").decode() or (
            "http" if host.startswith(("localhost", "127.")) else "https")
        return f"{proto}://{host}"

    # -- routing ----------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path, method = scope.get("path", ""), scope.get("method", "GET")
        base = self._base(scope)

        if path == "/health":
            return await self._json(send, 200, {"ok": True})

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
            cid = f"grok-bridge-{secrets.token_hex(8)}"
            CLIENTS[cid] = req
            return await self._json(send, 201, {
                "client_id": cid,
                "client_id_issued_at": int(time.time()),
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "redirect_uris": req.get("redirect_uris", []),
                "client_name": req.get("client_name", "grok-bridge client"),
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
                        f'Bearer realm="grok-bridge", '
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
            error = '<p class="e">Wrong passphrase. It is GROK_BRIDGE_TOKEN from your .env.</p>'

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

    print(f"grok-bridge  http://{HOST}:{PORT}/mcp")
    print(f"  roots           : {', '.join(str(r) for r in ROOTS)}")
    print(f"  permission mode : {PERMISSION_MODE}")
    print(f"  job timeout     : {JOB_TIMEOUT}s, max concurrent {MAX_CONCURRENT}")
    print("  oauth           : /authorize /token /register + discovery")
    uvicorn.run(AuthGateway(mcp.http_app(path="/mcp"), TOKEN),
                host=HOST, port=PORT, log_level="info")
