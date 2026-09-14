"""Exercise list_jobs against a throwaway server: shape, ordering, filtering,
limits, truncation, and the prune cap. No `claude` needed -- jobs are injected
directly into the registry."""
import asyncio, importlib.util, os, pathlib, sys, tempfile, time

os.environ.setdefault("LOCUM_TOKEN", "t")
os.environ.setdefault("LOCUM_ROOTS", "/tmp")
os.environ.setdefault("LOCUM_MAX_JOBS", "5")
# Importing the server restores the journal, so point it at a fresh temp path
# first: the operator's real history must neither leak into the suite nor be
# rewritten by it.
os.environ.setdefault("LOCUM_JOBS_FILE",
                      str(pathlib.Path(tempfile.mkdtemp(prefix="locum-test-")) / "jobs.jsonl"))

spec = importlib.util.spec_from_file_location(
    "srv", str(pathlib.Path(__file__).parent / "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv          # @dataclass resolves the module by name
spec.loader.exec_module(srv)

# LOCUM_ROOTS differs between a laptop and CI, so derive a legal cwd from the
# server's own config rather than assuming /tmp is allowed.
ROOT = str(srv.ROOTS[0])

results = []
def ok(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    results.append(cond)

def mk(jid, status, prompt="do a thing", result=None, kind="claude"):
    j = srv.Job(id=jid, kind=kind, prompt=prompt, cwd=ROOT)
    j.status = status
    j.result = result
    j.session_id = f"sess-{jid}"
    j.turns = 3
    if status != "running":
        j.finished = time.time()
    srv.JOBS[jid] = j
    return j

# @mcp.tool returns the plain function in some FastMCP versions and a
# FunctionTool wrapper in others.
_impl = getattr(srv.list_jobs, "fn", srv.list_jobs)
call = lambda **kw: asyncio.run(_impl(**kw))

print("\nempty registry")
r = call()
ok("returns empty cleanly", r["returned"] == 0 and r["jobs"] == [])

print("\nordering and shape")
for i, st in enumerate(["done", "error", "running"]):
    mk(f"job{i}", st, result="the answer" if st == "done" else None)
r = call()
ok("newest first", [j["job_id"] for j in r["jobs"]] == ["job2", "job1", "job0"])
ok("counts reported", r["returned"] == 3 and r["total_in_memory"] == 3)
first = r["jobs"][0]
ok("carries the useful fields",
   {"job_id", "kind", "status", "elapsed_seconds", "turns", "cwd", "prompt"} <= set(first))
ok("session_id exposed for resume", first.get("session_id") == "sess-job2")

print("\nfiltering")
r = call(status="done")
ok("filters by status", r["returned"] == 1 and r["jobs"][0]["job_id"] == "job0")
ok("matching vs total distinguished", r["matching"] == 1 and r["total_in_memory"] == 3)
try:
    call(status="bogus")
    ok("rejects an unknown status", False)
except ValueError as e:
    ok("rejects an unknown status", "must be one of" in str(e))

print("\nlimits")
r = call(limit=2)
ok("honours limit", r["returned"] == 2 and r["matching"] == 3)
ok("limit floor", call(limit=0)["returned"] == 1)
for i in range(60):
    mk(f"bulk{i}", "done", result="x")
ok("limit capped at 50", call(limit=999)["returned"] == 50)

print("\ntruncation")
srv.JOBS.clear()
mk("long", "done", prompt="p" * 500, result="r" * 900)
j = call()["jobs"][0]
ok("prompt truncated", len(j["prompt"]) <= 120, f"{len(j['prompt'])} chars")
ok("result truncated", len(j["result"]) <= 200, f"{len(j['result'])} chars")
ok("prompt keeps its content", j["prompt"].startswith("ppppp") and j["prompt"].endswith("..."))
ok("result keeps its content", j["result"].startswith("rrrrr") and j["result"].endswith("..."))
srv.JOBS.clear()
mk("short", "done", prompt="fix the auth bug", result="done")
j = call()["jobs"][0]
ok("short strings untouched", j["prompt"] == "fix the auth bug" and j["result"] == "done")

print("\nprune cap (LOCUM_MAX_JOBS=5)")
srv.JOBS.clear()
running = mk("keepme", "running")
for i in range(20):
    mk(f"old{i}", "done", result="x")
    srv._prune_jobs()
ok("history bounded", len(srv.JOBS) <= 5, f"{len(srv.JOBS)} jobs held")
ok("running job never pruned", "keepme" in srv.JOBS)

print("\nmodel and effort")
captured = {}
_spawn_real, _require_real = srv._spawn, srv._require
srv._spawn = lambda job, argv, *a, **k: captured.update(argv=argv, job=job) or {"job_id": job.id}
# Argv construction is what is under test, not whether the CLIs are installed,
# so the suite stays runnable on a machine (or CI runner) without them.
srv._require = lambda binary: f"/usr/bin/{binary}"
imp = lambda t: getattr(t, "fn", t)

asyncio.run(imp(srv.delegate_to_claude)("p", cwd=ROOT, model="opus", effort="max"))
a = captured["argv"]
ok("claude passes --model", "--model" in a and a[a.index("--model") + 1] == "opus")
ok("claude passes --effort", "--effort" in a and a[a.index("--effort") + 1] == "max")
ok("job records what was used",
   captured["job"].model == "opus" and captured["job"].effort == "max")

asyncio.run(imp(srv.delegate_to_codex)("p", cwd=ROOT, effort="max"))
a = captured["argv"]
ok("codex uses -c, not --effort", "-c" in a and "--effort" not in a)
ok("codex maps max onto high", 'model_reasoning_effort="high"' in a)
# -c must precede the subcommand or codex rejects it
ok("codex -c precedes exec", a.index("-c") < a.index("exec"))

asyncio.run(imp(srv.resume_claude)("sess-1", "p", cwd=ROOT, effort="low"))
a = captured["argv"]
ok("resume accepts effort", "--effort" in a and a[a.index("--effort") + 1] == "low")

for bad in ("turbo", "MAXIMUM", ""):
    try:
        asyncio.run(imp(srv.delegate_to_claude)("p", cwd=ROOT, effort=bad))
        ok(f"rejects effort {bad!r}", False)
    except ValueError:
        ok(f"rejects effort {bad!r}", True)

asyncio.run(imp(srv.delegate_to_claude)("p", cwd=ROOT, effort="  HIGH  "))
ok("effort is normalised", captured["job"].effort == "high")

asyncio.run(imp(srv.delegate_to_claude)("p", cwd=ROOT))
a = captured["argv"]
ok("omitted by default", "--effort" not in a and "--model" not in a)
srv._spawn, srv._require = _spawn_real, _require_real

print("\ncodex event parsing")
j = srv.Job(id="cx", kind="codex", prompt="p", cwd=ROOT)
j.status = "running"      # parsers only ever see live jobs
srv._note_codex_event(j, {"type": "thread.started", "thread_id": "th-42"})
ok("thread_id becomes session_id", j.session_id == "th-42")
srv._note_codex_event(j, {"type": "item.completed",
                          "item": {"type": "command_execution", "command": "pytest -q"}})
ok("item detail captured", j.activity and "pytest -q" in j.activity[-1])
srv._note_codex_event(j, {"type": "turn.completed", "usage": {}})
ok("turn.completed counts a turn", j.turns == 1)
srv._note_codex_event(j, {"type": "turn.failed", "error": {"message": "model refused"}})
ok("turn.failed records the error", "refused" in (j.error or ""))
ok("turn.failed leaves status to _run", j.status == "running")

print("\ncodex parser hardening")
def fresh():
    j = srv.Job(id="cx", kind="codex", prompt="p", cwd=ROOT)
    j.status = "running"      # parsers only ever see live jobs
    return j

for label, evt in [("bare string", "error"), ("number", 123), ("list", ["a"]),
                   ("error as string", {"type": "turn.failed", "error": "boom"}),
                   ("item as string", {"type": "item.completed", "item": "oops"}),
                   ("null item", {"type": "item.completed", "item": None})]:
    j = fresh()
    try:
        srv._note_codex_event(j, evt)
        ok(f"survives {label}", True)
    except Exception as e:
        ok(f"survives {label}", False, f"{type(e).__name__}: {e}")

j = fresh()
srv._note_codex_event(j, {"type": "turn.failed", "error": "boom"})
ok("failure recorded", j.error == "boom")
ok("but status left running", j.status == "running", "so cancel_job and prune stay correct")

j = fresh()
srv._note_codex_event(j, {"type": "error", "message": "transient"})
srv._note_codex_event(j, {"type": "turn.completed"})
ok("transient error does not freeze the job", j.status == "running" and j.turns == 1)

j = fresh()
for e in ({"type": "agent_message"}, {"type": "task_complete"}):
    srv._note_codex_event(j, e)
ok("old vocabulary counts one turn", j.turns == 1, f"got {j.turns}")
j2 = fresh()
srv._note_codex_event(j2, {"type": "task_complete"})
ok("task_complete alone is not a turn", j2.turns == 0, f"got {j2.turns}")

j = fresh()
srv._note_codex_event(j, {"type": "turn.completed"})
srv._note_codex_event(j, {"type": "agent_message"})
ok("new vocabulary wins once seen", j.turns == 1, f"got {j.turns}")

j = fresh()
for e in ({"type": "item.started"}, {"type": "item.updated"}, {"type": "token_count"}):
    srv._note_codex_event(j, e)
ok("noisy events do not flood activity", len(j.activity) == 0)

j = fresh()
srv._note_codex_event(j, {"msg": {"type": "agent_message", "session_id": "old-1"}})
ok("old msg envelope yields session_id", j.session_id == "old-1")

print("\nresume_codex")
captured = {}
_spawn_real, _require_real = srv._spawn, srv._require
srv._spawn = lambda job, argv, *a, **k: captured.update(argv=argv, job=job) or {"job_id": job.id}
srv._require = lambda binary: f"/usr/bin/{binary}"

asyncio.run(imp(srv.resume_codex)("th-1", "follow up", cwd=ROOT, effort="max"))
a = captured["argv"]
e = a.index("exec")
ok("resume argv shape", a[e:e + 4] == ["exec", "resume", "th-1", "follow up"], " ".join(a))
ok("resume keeps --json and output file", "--json" in a and "--output-last-message" in a)
ok("resume passes no --cd (unsupported)", "--cd" not in a)
ok("resume maps max onto high", 'model_reasoning_effort="high"' in a)
ok("resume -c precedes exec", a.index("-c") < a.index("exec"))
ok("resume presets the requested thread", captured["job"].session_id == "th-1")
ok("resume keeps the bypass flag", "--dangerously-bypass-approvals-and-sandbox" in a)

asyncio.run(imp(srv.resume_codex)("th-1", "p", cwd=ROOT, model="gpt-5"))
ok("resume passes --model", "--model" in captured["argv"])

srv.AUTONOMY = "ask"
asyncio.run(imp(srv.resume_codex)("th-1", "p", cwd=ROOT))
a = captured["argv"]
ok("ask-mode resume has no --sandbox (unsupported)", "--sandbox" not in a)
ok("ask-mode resume drops the bypass flag",
   "--dangerously-bypass-approvals-and-sandbox" not in a)
asyncio.run(imp(srv.delegate_to_codex)("p", cwd=ROOT))
ok("ask-mode fresh delegate keeps --sandbox", "--sandbox" in captured["argv"])
srv.AUTONOMY = "bypass"
srv._spawn, srv._require = _spawn_real, _require_real

try:
    asyncio.run(imp(srv.resume_codex)("th-1", "p", cwd=ROOT, effort="turbo"))
    ok("resume rejects a bad effort", False)
except ValueError:
    ok("resume rejects a bad effort", True)

print("\nresume thread mismatch")
import tempfile
def roundtrip(expect, reported):
    j = srv.Job(id="rx", kind="codex", prompt="p", cwd=ROOT)
    j.status = "running"         # finalize only runs on live jobs
    j.session_id = reported      # what the event stream carried back
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("last message")
        out = pathlib.Path(f.name)
    srv._finalize_codex_output(j, out, expect_thread=expect)
    leftover = out.exists()
    if leftover:
        out.unlink()
    return j, leftover

j, leftover = roundtrip("th-1", "th-1")
ok("matching thread finishes", j.status == "done" and j.result == "last message")
ok("matching thread cleans up", not leftover)
j, _ = roundtrip("th-1", "th-OTHER")
ok("fresh thread fails loudly",
   j.status == "error" and "th-OTHER" in (j.error or "") and "th-1" in (j.error or ""),
   (j.error or "")[:80])
j = srv.Job(id="dx", kind="codex", prompt="p", cwd=ROOT)
j.status = "running"         # finalize only runs on live jobs
j.session_id = "whatever"
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write("fresh run")
    out = pathlib.Path(f.name)
srv._finalize_codex_output(j, out)
out.unlink(missing_ok=True)
ok("fresh delegate ignores thread check", j.status == "done" and j.result == "fresh run")

print("\nqueued vs running")
ok("new jobs start queued",
   srv.Job(id="nq", kind="claude", prompt="p", cwd=ROOT).status == "queued")

srv.JOBS.clear()
mk("only-q", "queued")
r = call(status="queued")
ok("filters by queued", r["returned"] == 1 and r["jobs"][0]["job_id"] == "only-q")

srv.JOBS.clear()
mk("keepq", "queued")
for i in range(20):
    mk(f"old{i}", "done", result="x")
    srv._prune_jobs()
ok("queued job never pruned", "keepq" in srv.JOBS)

async def _wait_until(pred, timeout=15):
    start = time.time()
    while not pred():
        if time.time() - start > timeout:
            return False
        await asyncio.sleep(0.02)
    return True

async def queue_scenario():
    srv.SEM = asyncio.Semaphore(1)
    srv.NARRATE = False
    srv.JOBS.clear()
    try:
        noop = lambda j, e: None
        argv = [sys.executable, "-c", "import time; time.sleep(0.4)"]
        j1 = srv.Job(id="q1", kind="claude", prompt="p", cwd=ROOT)
        j2 = srv.Job(id="q2", kind="claude", prompt="p", cwd=ROOT)
        r1 = srv._spawn(j1, argv, noop)
        r2 = srv._spawn(j2, argv, noop)
        at_spawn = (j1.status, j2.status)
        started = await _wait_until(lambda: j1.status == "running")
        mid = (j1.status, j2.status)
        snap = await imp(srv.check_job)("q2")
        await asyncio.gather(j1._task, j2._task)
        return r1, r2, at_spawn, started, mid, snap, (j1.status, j2.status)
    finally:
        srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
        srv.NARRATE = True

r1, r2, at_spawn, started, mid, snap, end = asyncio.run(queue_scenario())
ok("_spawn reports queued", r1["status"] == "queued" and r2["status"] == "queued")
ok("next_step says queued", "already queued" in r1["next_step"])
ok("both queued at spawn", at_spawn == ("queued", "queued"), f"got {at_spawn}")
ok("first job observed running", started)
ok("second job queues behind it", mid == ("running", "queued"), f"got {mid}")
ok("check_job reports queued with a position",
   snap["status"] == "queued" and snap["queue_position"] == 1, str(snap))
ok("queued hint points at the slot", "free slot" in snap["hint"])
ok("queued job runs after", end == ("done", "done"), f"got {end}")

async def cancel_queued_scenario():
    srv.SEM = asyncio.Semaphore(1)
    srv.NARRATE = False
    srv.JOBS.clear()
    try:
        noop = lambda j, e: None
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        j1 = srv.Job(id="c1", kind="claude", prompt="p", cwd=ROOT)
        j2 = srv.Job(id="c2", kind="claude", prompt="p", cwd=ROOT)
        srv._spawn(j1, argv, noop)
        srv._spawn(j2, argv, noop)
        started = await _wait_until(lambda: j1.status == "running")
        res = await imp(srv.cancel_job)("c2")
        await imp(srv.cancel_job)("c1")
        await asyncio.gather(j1._task, j2._task, return_exceptions=True)
        return started, res, (j1.status, j2.status)
    finally:
        srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
        srv.NARRATE = True

started, res, end = asyncio.run(cancel_queued_scenario())
ok("cancel accepts a queued job", res["status"] == "cancelling")
ok("queued cancel lands", end[1] == "cancelled", f"got {end}")
ok("running cancel still lands", end[0] == "cancelled", f"got {end}")

print("\njob persistence")
import json
_journal_dir = tempfile.mkdtemp(prefix="locum-persist-test-")
_saved_jobs_file, _saved_jobs = srv.JOBS_FILE, dict(srv.JOBS)
srv.JOBS_FILE = pathlib.Path(_journal_dir) / "jobs.jsonl"
try:
    async def persist_scenario():
        srv.SEM = asyncio.Semaphore(4)
        srv.NARRATE = False
        srv.JOBS.clear()
        try:
            j = srv.Job(id="p1", kind="codex", prompt="persist me", cwd=ROOT)
            srv._spawn(j, [sys.executable, "-c", "pass"], lambda j, e: None)
            await j._task
            return j.status
        finally:
            srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
            srv.NARRATE = True

    ok("spawned job completes", asyncio.run(persist_scenario()) == "done")
    seen = [json.loads(line)["status"] for line in srv.JOBS_FILE.read_text().splitlines()]
    ok("spawn journals every transition", seen == ["queued", "running", "done"], str(seen))

    srv.JOBS.clear()
    j = srv.Job(id="ps", kind="claude", prompt="p", cwd=ROOT)
    j.status = "running"
    srv.JOBS["ps"] = j
    srv._note_claude_event(j, {"type": "system", "subtype": "init",
                               "session_id": "sess-9"})
    last = json.loads(srv.JOBS_FILE.read_text().splitlines()[-1])
    ok("claude init journals the session",
       last["job_id"] == "ps" and last["session_id"] == "sess-9")
    n0 = len(srv.JOBS_FILE.read_text().splitlines())
    srv._note_claude_event(j, {"type": "system", "subtype": "init",
                               "session_id": "sess-9"})
    ok("repeat sighting writes nothing",
       len(srv.JOBS_FILE.read_text().splitlines()) == n0)
    jx = srv.Job(id="px", kind="codex", prompt="p", cwd=ROOT)
    jx.status = "running"
    srv.JOBS["px"] = jx
    srv._note_codex_event(jx, {"type": "thread.started", "thread_id": "th-9"})
    last = json.loads(srv.JOBS_FILE.read_text().splitlines()[-1])
    ok("codex thread journals the session", last["session_id"] == "th-9")

    srv.JOBS.clear()
    srv.JOBS_FILE.unlink()
    d = srv.Job(id="rd", kind="claude", prompt="did work", cwd=ROOT)
    d.status, d.finished = "done", time.time()
    d.result, d.session_id, d.cost_usd = "r" * 5000, "sess-rd", 0.1234
    d.model, d.effort, d.turns = "opus", "high", 7
    d.tokens = {"input_tokens": 100}
    srv._persist_job(d)
    e = srv.Job(id="re", kind="codex", prompt="broke", cwd=ROOT)
    e.status, e.finished, e.error = "error", time.time(), "boom"
    srv._persist_job(e)
    r = srv.Job(id="rr", kind="claude", prompt="mid-flight", cwd=ROOT)
    r.status, r.session_id = "running", "sess-rr"
    srv._persist_job(r)
    q = srv.Job(id="rq", kind="codex", prompt="waiting", cwd=ROOT)
    q.session_id = "th-rq"
    srv._persist_job(q)
    w = srv.Job(id="rw", kind="claude", prompt="twice", cwd=ROOT)
    w.status = "running"
    srv._persist_job(w)
    w.status, w.result, w.finished = "done", "second write wins", time.time()
    srv._persist_job(w)
    with open(srv.JOBS_FILE, "a", encoding="utf-8") as f:
        f.write("not json\n[1,2]\n{\"no_id\": true}\n")
    srv.JOBS.clear()
    srv._load_jobs()
    ok("done job restored",
       srv.JOBS["rd"].status == "done" and srv.JOBS["rd"].cost_usd == 0.1234)
    ok("result bounded", len(srv.JOBS["rd"].result or "") <= 2000)
    ok("tuning restored",
       srv.JOBS["rd"].model == "opus" and srv.JOBS["rd"].effort == "high")
    ok("turns and tokens restored",
       srv.JOBS["rd"].turns == 7 and srv.JOBS["rd"].tokens == {"input_tokens": 100})
    ok("error job restored",
       srv.JOBS["re"].status == "error" and srv.JOBS["re"].error == "boom")
    ok("running job becomes a loud error",
       srv.JOBS["rr"].status == "error" and "did not survive" in (srv.JOBS["rr"].error or ""))
    ok("interrupted session kept for resume", srv.JOBS["rr"].session_id == "sess-rr")
    ok("queued job marked too",
       srv.JOBS["rq"].status == "error" and "queued" in (srv.JOBS["rq"].error or ""))
    ok("last write wins",
       srv.JOBS["rw"].status == "done" and srv.JOBS["rw"].result == "second write wins")
    ok("garbage lines skipped", len(srv.JOBS) == 5, f"{len(srv.JOBS)} jobs")
    order = [j["job_id"] for j in call(limit=50)["jobs"]]
    ok("restored order newest-first",
       order == sorted(order, key=lambda i: srv.JOBS[i].started, reverse=True), str(order))

    srv.JOBS.clear()
    srv.JOBS_FILE = pathlib.Path(_journal_dir) / "absent.jsonl"
    srv._load_jobs()
    ok("missing journal loads empty", srv.JOBS == {})
    srv.JOBS_FILE = pathlib.Path(_journal_dir) / "jobs.jsonl"

    mode = oct(srv.JOBS_FILE.stat().st_mode & 0o777)
    ok("journal is owner-only", mode == "0o600", mode)

    srv._load_jobs()
    for i in range(3):
        x = srv.Job(id=f"ex{i}", kind="claude", prompt="extra", cwd=ROOT)
        x.status, x.finished = "done", time.time()
        srv.JOBS[x.id] = x
        srv._persist_job(x)
    srv._prune_jobs()
    survivors = set(srv.JOBS)
    file_ids = [json.loads(line)["job_id"]
                for line in srv.JOBS_FILE.read_text().splitlines()]
    ok("prune bounds memory", len(srv.JOBS) <= 5, f"{len(srv.JOBS)} jobs")
    ok("prune compacts the journal", sorted(file_ids) == sorted(survivors),
       f"{len(file_ids)} lines")
finally:
    srv.JOBS.clear()
    srv.JOBS.update(_saved_jobs)
    srv.JOBS_FILE = _saved_jobs_file

print("\ncompletion webhook")
import http.server
import threading
received = []


class _Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        received.append({"headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": json.loads(self.rfile.read(length) or b"{}")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


_hookd = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
threading.Thread(target=_hookd.serve_forever, daemon=True).start()
_hook_url = f"http://127.0.0.1:{_hookd.server_port}/hook"
_saved_webhook = srv.WEBHOOK_URL
srv.WEBHOOK_URL = _hook_url
try:
    srv._deliver_webhook({"job_id": "w1", "status": "done"})
    ok("webhook delivered",
       len(received) == 1 and received[0]["body"]["job_id"] == "w1")
    ok("webhook posts json",
       received[0]["headers"].get("content-type") == "application/json")
    ua = received[0]["headers"].get("user-agent", "")
    ok("webhook user-agent set",
       ua.startswith("locum/") and "Python-urllib" not in ua, ua)

    srv.WEBHOOK_URL = "http://127.0.0.1:1/none"
    try:
        srv._deliver_webhook({"job_id": "w2"})
        ok("dead endpoint does not raise", True)
    except Exception as e:                            # noqa: BLE001
        ok("dead endpoint does not raise", False, repr(e))
    srv.WEBHOOK_URL = _hook_url

    async def webhook_scenario(kind):
        srv.SEM = asyncio.Semaphore(4)
        srv.NARRATE = False
        srv.JOBS.clear()
        try:
            jid = f"w-{kind}"
            j = srv.Job(id=jid, kind="claude", prompt="p", cwd=ROOT)
            if kind == "cancel":
                srv._spawn(j, [sys.executable, "-c", "import time; time.sleep(30)"],
                           lambda j, e: None)
                await _wait_until(lambda: j.status == "running")
                await imp(srv.cancel_job)(jid)
                await j._task
                await asyncio.sleep(0.3)
            else:
                argv = ([sys.executable, "-c", "pass"] if kind == "done"
                       else [sys.executable, "-c", "import sys; sys.exit(1)"])
                srv._spawn(j, argv, lambda j, e: None)
                await j._task
                for _ in range(100):
                    if any(r["body"].get("job_id") == jid for r in received):
                        break
                    await asyncio.sleep(0.05)
            return j.status
        finally:
            srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
            srv.NARRATE = True

    ok("done job completes", asyncio.run(webhook_scenario("done")) == "done")
    hits = [r for r in received if r["body"].get("job_id") == "w-done"]
    ok("completion fires once", len(hits) == 1, f"{len(hits)} hits")
    ok("payload mirrors check_job",
       hits[0]["body"].get("status") == "done" and "turns" in hits[0]["body"],
       str(sorted(hits[0]["body"])))
    ok("error job completes", asyncio.run(webhook_scenario("error")) == "error")
    ehits = [r for r in received if r["body"].get("job_id") == "w-error"]
    ok("error fires too",
       len(ehits) == 1 and ehits[0]["body"].get("status") == "error")
    ok("cancelled job completes",
       asyncio.run(webhook_scenario("cancel")) == "cancelled")
    ok("cancelled job stays silent",
       not any(r["body"].get("job_id") == "w-cancel" for r in received))

    srv.WEBHOOK_URL = ""

    async def quiet_scenario():
        srv.SEM = asyncio.Semaphore(4)
        srv.NARRATE = False
        srv.JOBS.clear()
        try:
            j = srv.Job(id="w-quiet", kind="claude", prompt="p", cwd=ROOT)
            srv._spawn(j, [sys.executable, "-c", "pass"], lambda j, e: None)
            await j._task
            await asyncio.sleep(0.3)
        finally:
            srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
            srv.NARRATE = True

    asyncio.run(quiet_scenario())
    ok("disabled webhook stays silent",
       not any(r["body"].get("job_id") == "w-quiet" for r in received))
finally:
    srv.WEBHOOK_URL = _saved_webhook
    _hookd.shutdown()
    _hookd.server_close()

print("\nstatus tool")
import shutil
import subprocess
srv.JOBS.clear()
mk("st-r", "running")
mk("st-q", "queued")
mk("st-d", "done", result="x")
s = asyncio.run(imp(srv.status)())
ok("status roots", s["roots"] == [str(r) for r in srv.ROOTS])
ok("status binaries keyed", set(s["binaries"]) == {"claude", "codex"})
ok("status binaries are paths or null",
   all(v is None or (isinstance(v, str) and os.path.isabs(v))
       for v in s["binaries"].values()),
   str(s["binaries"]))
ok("status counts", s["running"] == 1 and s["queued"] == 1)
ok("status free slots",
   s["free_slots"] == srv.MAX_CONCURRENT - 1
   and s["max_concurrent"] == srv.MAX_CONCURRENT)
names = [t.name for t in asyncio.run(srv.mcp.list_tools())]
ok("status registered", "status" in names)

print("\ngit changes on completion")
plain = tempfile.mkdtemp(prefix="locum-plain-test-")
ok("non-repo yields nothing", srv._git_changes(plain) is None)
has_git = bool(shutil.which("git"))
if not has_git:
    ok("git absent: annotation cleanly disabled", srv._git_changes(plain) is None)
else:
    repo = tempfile.mkdtemp(prefix="locum-git-test-")
    subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    (pathlib.Path(repo) / "a.txt").write_text("one\n")
    ch = srv._git_changes(repo)
    ok("untracked file shows in status",
       ch is not None and "a.txt" in ch["status_short"], str(ch))
    ok("nothing tracked means empty diff", ch["diff_stat"] == "")
    subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    ch = srv._git_changes(repo)
    ok("clean repo yields empty strings",
       ch == {"status_short": "", "diff_stat": ""}, str(ch))
    (pathlib.Path(repo) / "a.txt").write_text("one\ntwo\n")
    ch = srv._git_changes(repo)
    ok("modification shows in diff",
       "a.txt" in ch["diff_stat"] and "1 insertion" in ch["diff_stat"],
       ch["diff_stat"])

    async def git_scenario():
        srv.SEM = asyncio.Semaphore(4)
        srv.NARRATE = False
        srv.JOBS.clear()
        try:
            work = tempfile.mkdtemp(prefix="locum-work-test-")
            subprocess.run(["git", "init", "-q", work], check=True,
                           capture_output=True)
            j = srv.Job(id="g1", kind="claude", prompt="p", cwd=work)
            srv._spawn(j, [sys.executable, "-c",
                           "import pathlib; pathlib.Path('made.txt').write_text('hi')"],
                       lambda j, e: None)
            await j._task
            return j.status, await imp(srv.check_job)("g1")
        finally:
            srv.SEM = asyncio.Semaphore(srv.MAX_CONCURRENT)
            srv.NARRATE = True

    st, snap = asyncio.run(git_scenario())
    ok("job in repo completes", st == "done")
    ok("check_job carries what landed",
       "made.txt" in (snap.get("git_changes") or {}).get("status_short", ""),
       str(snap.get("git_changes")))
    srv._persist_job(srv.JOBS["g1"])
    srv.JOBS.clear()
    srv._load_jobs()
    ok("git annotation survives a restart",
       "made.txt" in ((srv.JOBS["g1"].git_changes or {}).get("status_short", "")))

print("\ndashboard controls")


class _FakeTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Send:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.status = msg["status"]
            self.headers = {k.decode(): v.decode() for k, v in msg["headers"]}
        else:
            self.body += msg.get("body", b"")


def _authed():
    return {"headers": [(b"cookie", f"locum_dash={srv.AuthGateway(None, 't')._cookie()}".encode())]}


def _api(path, method="GET", scope=None):
    gw = srv.AuthGateway(None, "t")
    send = _Send()
    asyncio.run(gw._api(scope if scope is not None else _authed(), send, path, method))
    return send.status, json.loads(send.body or b"{}"), send.headers


srv.JOBS.clear()
try:
    srv._cancel_job_by_id("nope")
    ok("helper rejects unknown jobs", False)
except ValueError:
    ok("helper rejects unknown jobs", True)
mk("fin", "done", result="x")
r = srv._cancel_job_by_id("fin")
ok("helper leaves finished jobs", r["status"] == "done" and "note" in r)
mk("live", "running")
r = srv._cancel_job_by_id("live")
ok("helper needs a task", r["status"] == "running" and "note" in r)
srv.JOBS["live"]._task = _FakeTask()
r = srv._cancel_job_by_id("live")
ok("helper cancels running jobs",
   r["status"] == "cancelling" and srv.JOBS["live"]._task.cancelled)
mk("wait", "queued")
srv.JOBS["wait"]._task = _FakeTask()
r = srv._cancel_job_by_id("wait")
ok("helper cancels queued jobs",
   r["status"] == "cancelling" and srv.JOBS["wait"]._task.cancelled)
r = asyncio.run(imp(srv.cancel_job)("fin"))
ok("tool delegates to helper", r["status"] == "done" and "note" in r)

s, b, _ = _api("/api/jobs/live/cancel", "POST")
ok("endpoint cancels", s == 200 and b["status"] == "cancelling", f"{s} {b}")
s, b, _ = _api("/api/jobs/nope/cancel", "POST")
ok("endpoint 404s unknown jobs", s == 404, f"{s} {b}")
s, b, h = _api("/api/jobs/live/cancel", "GET")
ok("endpoint refuses GET", s == 405 and h.get("allow") == "POST", f"{s} {b}")
s, b, _ = _api("/api/jobs/live/cancel", "POST", scope={"headers": []})
ok("endpoint needs the cookie", s == 401, f"{s} {b}")
s, b, _ = _api("/api/jobs")
ok("jobs listing still serves", s == 200 and isinstance(b.get("jobs"), list))
s, b, _ = _api("/api/jobs/fin")
ok("job detail still serves", s == 200 and b.get("job_id") == "fin")

srv.JOBS.clear()
j = srv.Job(id="bs", kind="claude", prompt="p", cwd=ROOT)
j.status = "running"
srv.JOBS["bs"] = j
q: asyncio.Queue = asyncio.Queue()
srv.SUBSCRIBERS.add(q)
try:
    srv._note_claude_event(j, {"type": "system", "subtype": "init",
                               "session_id": "sess-live"})
    msg = q.get_nowait()
    ok("session sighting rebroadcasts the job",
       msg["type"] == "job" and msg["job"].get("session_id") == "sess-live")
    srv._note_claude_event(j, {"type": "system", "subtype": "init",
                               "session_id": "sess-live"})
    ok("repeat sighting stays quiet", q.empty())
finally:
    srv.SUBSCRIBERS.discard(q)

print("\ndoctor")
_docbin = tempfile.mkdtemp(prefix="locum-doctor-test-")
pathlib.Path(_docbin, "claude").write_text('#!/bin/sh\necho "2.9.9 (Fake Claude Code)"\n')
pathlib.Path(_docbin, "codex").write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n')
os.chmod(pathlib.Path(_docbin) / "claude", 0o755)
os.chmod(pathlib.Path(_docbin) / "codex", 0o755)


def _in_env(fn, **kw):
    """Run fn with LOCUM_* cleared and kw applied. Restores everything. The
    journal defaults to a temp path so no test can depend on (or read) the
    operator's real history; journal tests override it explicitly."""
    kw.setdefault("LOCUM_JOBS_FILE", str(pathlib.Path(_docbin) / "jobs.jsonl"))
    saved = dict(os.environ)
    try:
        for k in [k for k in os.environ if k.startswith("LOCUM_")]:
            del os.environ[k]
        os.environ.update(kw)
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _doclevels(**kw):
    return {n: (d, lv) for n, d, lv in _in_env(srv._doctor_checks, **kw)}


_full_path = _docbin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
_healthy = {"LOCUM_TOKEN": "x" * 10, "LOCUM_ROOTS": ROOT, "PATH": _full_path}
lv = _doclevels(**_healthy)
ok("healthy setup has no failures", all(lv != "fail" for _, lv in lv.values()))
ok("doctor reports cli versions",
   "9.9.9" in lv["codex"][0] and "2.9.9" in lv["claude"][0])
ok("no verdict line when a cli exists", "agents" not in lv)
ok("token length, never the token", lv["token"] == ("set (10 chars)", "ok"))
ok("healthy exits 0", _in_env(srv._run_doctor, **_healthy) == 0)
ok("broken exits 1", _in_env(srv._run_doctor) == 1)

lv = _doclevels(PATH=_full_path)
ok("missing token fails", lv["token"][1] == "fail")
lv = _doclevels(LOCUM_TOKEN="x", LOCUM_ROOTS="/definitely/not/here")
ok("missing roots fail",
   lv["roots"][1] == "fail" and "not/here" in lv["roots"][0])
lv = _doclevels(LOCUM_TOKEN="x", LOCUM_ROOTS=":::")
ok("empty roots fail", lv["roots"][1] == "fail")

_emptybin = tempfile.mkdtemp(prefix="locum-empty-test-")
lv = _doclevels(LOCUM_TOKEN="x", LOCUM_ROOTS=ROOT, PATH=_emptybin)
ok("missing clis warn each",
   lv["claude"][1] == "warn" and lv["codex"][1] == "warn")
ok("no agent at all fails", lv["agents"][1] == "fail")

lv = _doclevels(**_healthy, LOCUM_PORT="abc", LOCUM_AUTONOMY="maybe",
                 LOCUM_MAX_CONCURRENT="0", LOCUM_MAX_JOBS="lots")
ok("bad port fails", lv["port"][1] == "fail")
ok("bad autonomy fails", lv["autonomy"][1] == "fail")
ok("zero concurrency fails", lv["concurrency"][1] == "fail")
ok("bad max_jobs fails", lv["max_jobs"][1] == "fail")
ok("valid knobs stay silent", "max_events" not in _doclevels(**_healthy))
lv = _doclevels(**_healthy, LOCUM_PORT="65536")
ok("huge port fails without traceback", lv["port"][1] == "fail")
lv = _doclevels(**_healthy, LOCUM_PORT="0")
ok("zero port fails", lv["port"][1] == "fail")
lv = _doclevels(**_healthy, LOCUM_JOB_TIMEOUT="0", LOCUM_MAX_EVENTS="-1")
ok("zero timeout fails", lv["timeout"][1] == "fail")
ok("negative max_events fails", lv["max_events"][1] == "fail")
lv = _doclevels(**_healthy, LOCUM_MAX_EVENTS="0")
ok("zero max_events stays silent", "max_events" not in lv)
lv = _doclevels(**_healthy,
                 LOCUM_COMPLETION_WEBHOOK="https://hooks.example.com:bad/x")
ok("bad webhook port fails", lv["webhook"][1] == "fail")

import socket as _sock
_s = _sock.socket()
_s.bind(("127.0.0.1", 0))
try:
    lv = _doclevels(**_healthy, LOCUM_PORT=str(_s.getsockname()[1]))
    ok("used port reported, not failed",
       lv["port"][1] == "ok" and "in use" in lv["port"][0])
finally:
    _s.close()

lv = _doclevels(**_healthy, LOCUM_COMPLETION_WEBHOOK="not a url")
ok("bad webhook fails", lv["webhook"][1] == "fail")
lv = _doclevels(**_healthy,
                 LOCUM_COMPLETION_WEBHOOK="https://hooks.example.com/x?token=secret")
ok("webhook shows host only",
   lv["webhook"] == ("set (https://hooks.example.com)", "ok"))

_jd = tempfile.mkdtemp(prefix="locum-doctor-journal-")
lv = _doclevels(**_healthy,
                 LOCUM_JOBS_FILE=str(pathlib.Path(_jd) / "sub" / "jobs.jsonl"))
ok("missing journal is fine when creatable", lv["journal"][1] == "ok")
_jf = pathlib.Path(_jd) / "jobs.jsonl"
_jf.write_text('{"job_id": "a"}\nnot json\n{"job_id": "a"}\n{"job_id": "b"}\n')
lv = _doclevels(**_healthy, LOCUM_JOBS_FILE=str(_jf))
ok("journal counts distinct jobs", lv["journal"][0].endswith("(2 jobs)"),
   lv["journal"][0])
_ro = pathlib.Path(_jd) / "readonly.jsonl"
_ro.write_text('{"job_id": "a"}\n')
_ro.chmod(0o444)
if os.geteuid() == 0:
    ok("read-only journal fails", True, "skipped as root")
else:
    lv = _doclevels(**_healthy, LOCUM_JOBS_FILE=str(_ro))
    ok("read-only journal fails", lv["journal"][1] == "fail")


def _doctor_cli(**kw):
    kw.setdefault("LOCUM_JOBS_FILE", str(pathlib.Path(_jd) / "cli.jsonl"))
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("LOCUM_")]:
        del env[k]
    env.update(kw)
    p = subprocess.run([sys.executable, "server.py", "--doctor"],
                       cwd=str(pathlib.Path(__file__).parent),
                       env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout


rc, out = _doctor_cli(LOCUM_TOKEN="x", LOCUM_ROOTS=ROOT,
                     PATH=_docbin + os.pathsep + os.environ.get("PATH", ""))
ok("cli healthy exits 0", rc == 0 and "locum doctor" in out, f"rc={rc}")
rc, out = _doctor_cli(LOCUM_ROOTS=ROOT)
ok("cli missing token exits 1", rc == 1 and "FAIL" in out, f"rc={rc}")

print("\nspend cap")
_saved_cost, _saved_jobs = srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY
try:
    srv.JOBS.clear()
    a = mk("s1", "done", result="x")
    a.cost_usd = 6.0
    b = mk("s2", "done", result="x")
    b.cost_usd = 5.0
    old = srv.Job(id="sold", kind="claude", prompt="p", cwd=ROOT)
    old.status, old.finished, old.cost_usd = "done", time.time(), 100.0
    old.started = time.time() - 90000
    srv.JOBS["sold"] = old
    spent, started = srv._usage_24h()
    ok("usage sums the window", spent == 11.0 and started == 2,
       f"{spent} {started}")

    weird = mk("s3", "done", result="x")
    weird.cost_usd = "bogus"
    spent, started = srv._usage_24h()
    ok("non-numeric cost ignored", spent == 11.0 and started == 3,
       f"{spent} {started}")
    del srv.JOBS["s3"]

    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 10.0, 0
    try:
        srv._check_caps()
        ok("cost cap refuses", False)
    except ValueError as e:
        ok("cost cap refuses", "10.00" in str(e) and "24h" in str(e),
           str(e)[:80])
    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 0, 2
    try:
        srv._check_caps()
        ok("job cap refuses", False)
    except ValueError as e:
        ok("job cap refuses", "2 of 2" in str(e), str(e)[:80])
    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 0, 3
    srv._check_caps()
    ok("headroom passes", True)
    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 0, 0
    srv._check_caps()
    ok("unset disables", True)

    _require_real = srv._require
    srv._require = lambda binary: f"/usr/bin/{binary}"
    try:
        srv.MAX_JOBS_PER_DAY = 2
        try:
            asyncio.run(imp(srv.delegate_to_claude)("p", cwd=ROOT))
            ok("delegate_to_claude honors the cap", False)
        except ValueError as e:
            ok("delegate_to_claude honors the cap", "job cap" in str(e))
        try:
            asyncio.run(imp(srv.resume_claude)("sess-1", "p", cwd=ROOT))
            ok("resume_claude honors the cap", False)
        except ValueError:
            ok("resume_claude honors the cap", True)
        try:
            asyncio.run(imp(srv.delegate_to_codex)("p", cwd=ROOT))
            ok("delegate_to_codex honors the cap", False)
        except ValueError:
            ok("delegate_to_codex honors the cap", True)
        try:
            asyncio.run(imp(srv.resume_codex)("th-1", "p", cwd=ROOT))
            ok("resume_codex honors the cap", False)
        except ValueError:
            ok("resume_codex honors the cap", True)
    finally:
        srv._require = _require_real
    ok("refusals spawn nothing", len(srv.JOBS) == 3 and "s1" in srv.JOBS)

    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 10.0, 5
    s = asyncio.run(imp(srv.status)())
    ok("status reports budget",
       s["budget"] == {"max_cost_usd": 10.0, "spent_24h": 11.0,
                       "max_jobs_per_day": 5, "started_24h": 2},
       str(s["budget"]))
    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = 0, 0
    s = asyncio.run(imp(srv.status)())
    ok("unset caps read null",
       s["budget"]["max_cost_usd"] is None
       and s["budget"]["max_jobs_per_day"] is None)

    srv.JOBS.clear()
    srv.MAX_COST_USD = -1
    try:
        srv._check_caps()
        ok("negative cap fails closed", False)
    except ValueError:
        ok("negative cap fails closed", True)

    lv = _doclevels(**_healthy, LOCUM_MAX_COST_USD="-5")
    ok("negative spend cap fails doctor", lv["spend_cap"][1] == "fail")
    lv = _doclevels(**_healthy, LOCUM_MAX_JOBS_PER_DAY="2.5")
    ok("fractional job cap fails doctor", lv["job_cap"][1] == "fail")
    lv = _doclevels(**_healthy, LOCUM_MAX_COST_USD="25",
                     LOCUM_MAX_JOBS_PER_DAY="50")
    ok("valid caps stay silent", "spend_cap" not in lv and "job_cap" not in lv)
finally:
    srv.MAX_COST_USD, srv.MAX_JOBS_PER_DAY = _saved_cost, _saved_jobs

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
