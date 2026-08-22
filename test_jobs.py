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
srv._note_codex_event(j, {"type": "thread.started", "thread_id": "th-42"})
ok("thread_id becomes session_id", j.session_id == "th-42")
srv._note_codex_event(j, {"type": "item.completed",
                          "item": {"type": "command_execution", "command": "pytest -q"}})
ok("item detail captured", j.activity and "pytest -q" in j.activity[-1])
srv._note_codex_event(j, {"type": "turn.completed", "usage": {}})
ok("turn.completed counts a turn", j.turns == 1)
srv._note_codex_event(j, {"type": "turn.failed", "error": {"message": "model refused"}})
ok("turn.failed marks the job errored", j.status == "error" and "refused" in j.error)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
