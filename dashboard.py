"""The Locum dashboard: one page showing every delegated session, its full
transcript, and a live feed while work is running.

Kept out of server.py because it is presentation, not protocol. server.py
imports render() and hands it the job registry.

Auth deliberately mirrors the MCP side rather than inventing a second scheme:
the same LOCUM_TOKEN, exchanged once for a signed cookie so a browser is not
pasting a bearer header on every request. /authorize already sits on a public
tunnel; the dashboard must not be the soft way in.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Locum</title>
<style>
  :root {
    --bg:#0b0b0d; --panel:#141417; --panel-2:#1a1a1e; --line:#26262c;
    --fg:#e7e7ea; --fg-2:#9a9aa4; --fg-3:#6e6e78;
    --ok:#7ec699; --run:#e0b568; --bad:#e08a8a;
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  * { box-sizing:border-box }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,system-ui,sans-serif;
    height:100vh; display:flex; flex-direction:column;
  }
  header {
    display:flex; align-items:baseline; gap:18px; flex-wrap:wrap;
    padding:14px 20px; border-bottom:1px solid var(--line);
  }
  h1 { font-size:15px; margin:0; letter-spacing:.02em }
  h1 span { color:var(--fg-3); font-weight:400 }
  .stats { display:flex; gap:20px; margin-left:auto; flex-wrap:wrap }
  .stat b { font:600 15px/1 var(--mono) }
  .stat span { color:var(--fg-3); font-size:11px; text-transform:uppercase; letter-spacing:.07em }
  #live { font-size:11px; color:var(--fg-3) }
  #live.on { color:var(--ok) }

  main { flex:1; display:grid; grid-template-columns:minmax(300px,380px) 1fr; min-height:0 }
  @media (max-width:820px) { main { grid-template-columns:1fr; grid-template-rows:40% 1fr } }

  #leftcol { border-right:1px solid var(--line); display:flex; flex-direction:column; min-height:0; min-width:0 }
  #listhead { padding:9px 16px; border-bottom:1px solid var(--line) }
  #listhead select { width:100%; background:var(--panel); color:var(--fg); border:1px solid var(--line);
                     border-radius:6px; padding:6px 8px; font:12px inherit }
  #list { overflow-y:auto; min-height:0 }
  .btn { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:6px;
         padding:4px 11px; font:12px inherit; cursor:pointer }
  .btn:hover { background:var(--panel-2) }
  .btn.danger { color:var(--bad); border-color:var(--bad) }
  .btn:disabled { opacity:.45; cursor:default }
  #detailhead { display:flex; align-items:center; gap:10px; margin:0 0 16px }
  #detailhead .spacer { flex:1 }
  .okmsg { color:var(--ok); font-size:12px }
  .errmsg { color:var(--bad); font-size:12px }
  .job {
    padding:11px 16px; border-bottom:1px solid var(--line); cursor:pointer;
    display:grid; grid-template-columns:auto 1fr auto; gap:4px 9px; align-items:baseline;
  }
  .job:hover { background:var(--panel) }
  .job[aria-selected=true] { background:var(--panel-2); box-shadow:inset 2px 0 0 var(--fg) }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--fg-3) }
  .dot.done { background:var(--ok) } .dot.running { background:var(--run) }
  .dot.queued { background:transparent; border:1px solid var(--run) }
  .dot.error, .dot.timeout, .dot.cancelled { background:var(--bad) }
  .job .p { grid-column:2; color:var(--fg); overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
  .job .m { grid-column:2/4; color:var(--fg-3); font:11px/1.4 var(--mono) }
  .job time { color:var(--fg-3); font:11px/1 var(--mono) }

  #detail { overflow-y:auto; min-height:0; padding:20px 24px }
  .empty { color:var(--fg-3); padding:40px 0; text-align:center }
  dl { display:grid; grid-template-columns:auto 1fr; gap:5px 16px; margin:0 0 20px; font:12px/1.5 var(--mono) }
  dt { color:var(--fg-3) } dd { margin:0; word-break:break-all }

  .term {
    background:#08080a; border:1px solid var(--line); border-radius:8px;
    padding:14px 16px; font:12px/1.65 var(--mono); white-space:pre-wrap; word-break:break-word;
  }
  .ev { display:grid; grid-template-columns:62px 1fr; gap:10px; padding:2px 0 }
  .ev .t { color:var(--fg-3) }
  .ev .l { color:var(--fg) }
  .ev.thinking .l { color:#9b8fd0; font-style:italic }
  .ev.text .l { color:var(--fg-2) }
  .ev.result .l { color:var(--fg-3) }
  .ev.error .l { color:var(--bad) }
  .ev details summary { cursor:pointer; color:inherit }
  .ev details div { color:var(--fg-2); padding:6px 0 6px 12px; border-left:1px solid var(--line); margin-top:5px }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--fg-3); margin:22px 0 9px }
  pre.out { background:var(--panel); border:1px solid var(--line); border-radius:8px;
            padding:13px 15px; font:12px/1.6 var(--mono); white-space:pre-wrap; margin:0 }

  form.login { max-width:340px; margin:16vh auto; padding:0 20px }
  form.login input { width:100%; padding:11px 13px; border-radius:8px; border:1px solid var(--line);
                     background:var(--panel); color:var(--fg); font:inherit; margin:12px 0 }
  form.login button { width:100%; padding:11px; border:0; border-radius:8px;
                      background:var(--fg); color:var(--bg); font:600 14px inherit; cursor:pointer }
  .err { color:var(--bad); font-size:13px }
</style>

<header>
  <h1>Locum <span id="host"></span></h1>
  <div class="stats">
    <div class="stat"><b id="s-jobs">0</b> <span>jobs</span></div>
    <div class="stat"><b id="s-run">0</b> <span>running</span></div>
    <div class="stat"><b id="s-tok">0</b> <span>tokens</span></div>
    <div class="stat"><b id="s-cost">$0</b> <span>reported</span></div>
    <div class="stat"><b id="s-time">0s</b> <span>agent time</span></div>
  </div>
  <span id="live">connecting</span>
</header>

<main>
  <div id="leftcol">
    <div id="listhead"><select id="filter" aria-label="Filter by status">
      <option value="">All statuses</option>
      <option value="queued">Queued</option>
      <option value="running">Running</option>
      <option value="done">Done</option>
      <option value="error">Error</option>
      <option value="timeout">Timeout</option>
      <option value="cancelled">Cancelled</option>
    </select></div>
    <div id="list"></div>
  </div>
  <div id="detail"><p class="empty">Select a session.</p></div>
</main>

<script>
const $ = s => document.querySelector(s);
const jobs = new Map();
let selected = location.hash.slice(1) || null;
let statusFilter = "";

const ago = ts => {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return Math.round(d) + "s ago";
  if (d < 3600) return Math.round(d / 60) + "m ago";
  return Math.round(d / 3600) + "h ago";
};
const clock = ts => new Date(ts * 1000).toTimeString().slice(0, 8);
const num = n => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n);
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const copyText = async t => {
  try { await navigator.clipboard.writeText(t); return true; }
  catch { /* fall through to the legacy path */ }
  const ta = document.createElement("textarea");
  ta.value = t; document.body.appendChild(ta); ta.select();
  try { return document.execCommand("copy"); } catch { return false; }
  finally { ta.remove(); }
};

function stats() {
  const all = [...jobs.values()];
  $("#s-jobs").textContent = all.length;
  $("#s-run").textContent = all.filter(j => j.status === "running").length;
  const tok = all.reduce((n, j) => n + Object.values(j.tokens || {}).reduce((a, b) => a + b, 0), 0);
  $("#s-tok").textContent = num(tok);
  const cost = all.reduce((n, j) => n + (j.cost_usd || 0), 0);
  $("#s-cost").textContent = "$" + cost.toFixed(2);
  const secs = all.reduce((n, j) => n + (j.elapsed_seconds || 0), 0);
  $("#s-time").textContent = secs > 90 ? Math.round(secs / 60) + "m" : Math.round(secs) + "s";
}

function renderList() {
  const sorted = [...jobs.values()]
    .filter(j => !statusFilter || j.status === statusFilter)
    .sort((a, b) => b.started - a.started);
  $("#list").innerHTML = sorted.map(j => `
    <div class="job" data-id="${j.job_id}" aria-selected="${j.job_id === selected}">
      <span class="dot ${j.status}"></span>
      <span class="p">${esc(j.prompt)}</span>
      <time>${ago(j.started)}</time>
      <span class="m">${j.kind}${j.model ? " / " + esc(j.model) : ""}${j.effort ? " / " + esc(j.effort) : ""}
        &middot; ${j.status} &middot; ${j.turns} turns &middot; ${j.elapsed_seconds}s${j.resumed_from ? " &middot; resumed" : ""}</span>
    </div>`).join("") || `<p class="empty">No sessions yet.</p>`;
  document.querySelectorAll(".job").forEach(el =>
    el.onclick = () => { selected = el.dataset.id; location.hash = selected; renderList(); openDetail(); });
  stats();
}

function evLine(e) {
  const body = e.detail
    ? `<details><summary>${esc(e.label)}</summary><div>${esc(e.detail)}</div></details>`
    : esc(e.label);
  return `<div class="ev ${e.kind}"><span class="t">${clock(e.t)}</span><span class="l">${body}</span></div>`;
}

async function openDetail() {
  if (!selected) return;
  const r = await fetch(`/api/jobs/${selected}`, { credentials: "same-origin" });
  if (!r.ok) { $("#detail").innerHTML = `<p class="empty">Gone from memory.</p>`; return; }
  const j = await r.json();
  const tok = Object.entries(j.tokens || {}).map(([k, v]) => `${k.replace(/_tokens$/, "")} ${num(v)}`).join("  ");
  const live = j.status === "queued" || j.status === "running";
  $("#detail").innerHTML = `
    <div id="detailhead">
      ${live ? `<button class="btn danger" id="cancelbtn">Cancel job</button>` : ""}
      <span class="spacer"></span><span id="detailmsg"></span>
    </div>
    <dl>
      <dt>job</dt><dd>${j.job_id} &middot; ${j.kind} &middot; ${j.status}</dd>
      <dt>prompt</dt><dd>${esc(j.prompt_full || j.prompt)}</dd>
      <dt>cwd</dt><dd>${esc(j.cwd)}</dd>
      ${j.model || j.effort ? `<dt>tuning</dt><dd>${esc(j.model || "default")} / ${esc(j.effort || "default")}</dd>` : ""}
      ${j.session_id ? `<dt>session</dt><dd>${esc(j.session_id)}${j.resumed_from ? " (resumed)" : ""} <button class="btn" id="copybtn">copy</button></dd>` : ""}
      <dt>elapsed</dt><dd>${j.elapsed_seconds}s &middot; ${j.turns} turns${j.cost_usd ? " &middot; $" + j.cost_usd : ""}</dd>
      ${tok ? `<dt>tokens</dt><dd>${tok}</dd>` : ""}
    </dl>
    <h2>Transcript</h2>
    <div class="term" id="term">${(j.events || []).map(evLine).join("") || "<span class='t'>no events recorded</span>"}</div>
    ${j.result ? `<h2>Result</h2><pre class="out">${esc(j.result)}</pre>` : ""}
    ${j.error ? `<h2>Error</h2><pre class="out">${esc(j.error)}${j.stderr_tail ? "\\n\\n" + esc(j.stderr_tail.join("\\n")) : ""}</pre>` : ""}`;

  const msg = $("#detailmsg");
  const say = (t, bad) => { if (msg) { msg.textContent = t; msg.className = bad ? "errmsg" : "okmsg"; } };
  const cb = $("#copybtn");
  if (cb) cb.onclick = async () => {
    const ok = await copyText(j.session_id);
    say(ok ? "copied session id" : "copy failed", !ok);
  };
  const kb = $("#cancelbtn");
  if (kb) kb.onclick = async () => {
    if (!confirm(`Cancel ${j.kind} job ${j.job_id}?`)) return;
    kb.disabled = true;
    try {
      const cr = await fetch(`/api/jobs/${j.job_id}/cancel`,
        { method: "POST", credentials: "same-origin" });
      say(cr.ok ? "cancelling…" : `cancel failed: ${cr.status}`, !cr.ok);
      // The live feed re-renders this pane when the job's status lands.
      if (cr.ok) setTimeout(openDetail, 800);
    } catch { say("cancel failed: network error", true); }
    kb.disabled = false;
  };
}

async function boot() {
  $("#host").textContent = location.host;
  $("#filter").onchange = e => { statusFilter = e.target.value; renderList(); };
  const r = await fetch("/api/jobs", { credentials: "same-origin" });
  (await r.json()).jobs.forEach(j => jobs.set(j.job_id, j));
  renderList();
  if (selected) openDetail();

  const es = new EventSource("/api/stream");
  es.onopen = () => { $("#live").textContent = "live"; $("#live").className = "on"; };
  es.onerror = () => { $("#live").textContent = "reconnecting"; $("#live").className = ""; };
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "job") { jobs.set(m.job.job_id, m.job); renderList(); if (m.job.job_id === selected) openDetail(); }
    // Append live rather than refetching, so a running transcript streams.
    if (m.type === "event" && m.job_id === selected) {
      const t = $("#term");
      if (t) { t.insertAdjacentHTML("beforeend", evLine(m.event)); t.scrollTop = t.scrollHeight; }
    }
  };
}
boot();
</script>
"""

LOGIN = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Locum</title>
<style>
  body {{ margin:0; background:#0b0b0d; color:#e7e7ea;
         font:14px/1.5 -apple-system,system-ui,sans-serif }}
  form {{ max-width:340px; margin:16vh auto; padding:0 20px }}
  h1 {{ font-size:16px; margin:0 0 4px }}
  p {{ color:#9a9aa4; font-size:13px; margin:0 }}
  input {{ width:100%; box-sizing:border-box; padding:11px 13px; border-radius:8px;
          border:1px solid #26262c; background:#141417; color:#e7e7ea;
          font:inherit; margin:16px 0 12px }}
  button {{ width:100%; padding:11px; border:0; border-radius:8px; background:#e7e7ea;
           color:#0b0b0d; font:600 14px inherit; cursor:pointer }}
  .err {{ color:#e08a8a; font-size:13px; margin-top:12px }}
</style>
<form method="post">
  <h1>Locum</h1>
  <p>Enter LOCUM_TOKEN to view delegated sessions.</p>
  <input type="password" name="passphrase" placeholder="LOCUM_TOKEN" autofocus required>
  <button type="submit">Open</button>
  {error}
</form>
"""
