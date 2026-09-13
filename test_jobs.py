"""Exercise list_jobs against a throwaway server: shape, ordering, filtering,
limits, truncation, and the prune cap. No `claude` needed -- jobs are injected
directly into the registry."""
import asyncio, importlib.util, os, pathlib, sys, time

os.environ.setdefault("LOCUM_TOKEN", "t")
os.environ.setdefault("LOCUM_ROOTS", "/tmp")
os.environ.setdefault("LOCUM_MAX_JOBS", "5")

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

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
