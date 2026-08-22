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

results = []
def ok(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    results.append(cond)

def mk(jid, status, prompt="do a thing", result=None, kind="claude"):
    j = srv.Job(id=jid, kind=kind, prompt=prompt, cwd="/tmp")
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

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
