"""Inline HTML dashboard for the Crawler API.

Generate jobs from the seed, paste manual jobs, run crawls, and forward the kept pages
to the L2 intelligence layer — all from the browser. The summary counters read the exact
fields the crawl API returns (fetched / kept / sent / accepted), so numbers are truthful.
"""
from __future__ import annotations

import html


def _esc(v) -> str:
    return html.escape(str(v))


# Default L2 ingest URL prefilled in the form (override in the field for a remote L2).
DEFAULT_L2_URL = "http://127.0.0.1:8000"


def render() -> str:
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mallory Crawler API — Job Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin:0;
         background:#0f172a; color:#e2e8f0; }
  header { padding:18px 24px; background:#111827; border-bottom:1px solid #1f2937; }
  h1 { margin:0 0 4px; font-size:18px; }
  .sub { font-size:12px; color:#64748b; }
  .sub a { color:#60a5fa; }
  .stats-bar { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  .stat { background:#1e293b; padding:3px 9px; border-radius:12px; font-size:12px; }
  .toolbar { padding:16px 24px; display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
  button { background:#2563eb; color:#fff; border:none; padding:8px 18px;
           border-radius:8px; font-size:13px; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.green { background:#15803d; }
  button.purple { background:#7c3aed; }
  label.chk { font-size:13px; color:#cbd5e1; cursor:pointer; user-select:none; }
  input[type=text] { background:#1e293b; color:#e2e8f0; border:1px solid #334155;
                     padding:7px 12px; border-radius:8px; font-size:13px; }
  .tabs { display:flex; gap:2px; padding:0 24px; margin-top:6px; }
  .tab { padding:8px 18px; background:#1e293b; border:1px solid #334155; border-bottom:none;
         border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; color:#94a3b8; }
  .tab.active { background:#0f172a; color:#e2e8f0; font-weight:600; }
  .pane { padding:16px 24px 32px; }
  textarea { width:100%; min-height:180px; background:#1e293b; color:#e2e8f0;
             border:1px solid #334155; border-radius:8px; padding:12px;
             font:13px/1.5 monospace; }
  select { background:#1e293b; color:#e2e8f0; border:1px solid #334155; padding:8px;
           border-radius:8px; font-size:13px; }
  .status { font-size:13px; margin-top:12px; padding:8px 12px; border-radius:8px; }
  .status.ok { background:#064e3b; color:#a7f3d0; }
  .status.err { background:#7c2d12; color:#fed7aa; }
  .status.wait { background:#1e3a5f; color:#93c5fd; }
  .resrow { margin-top:8px; padding:10px 12px; background:#1e293b; border:1px solid #334155;
            border-radius:8px; font-size:13px; display:flex; gap:16px; flex-wrap:wrap; align-items:center; }
  .resrow .jid { font-weight:600; min-width:120px; }
  .chip { padding:2px 9px; border-radius:10px; font-size:12px; background:#0f172a; }
  .chip.acc { background:#064e3b; color:#a7f3d0; }
  .chip.zero { background:#3f1d1d; color:#fecaca; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:12px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }
  .card h3 { margin:0 0 6px; font-size:14px; }
  .card .meta { font-size:11px; color:#94a3b8; margin-bottom:4px; }
  .card .urls { font-size:11px; color:#60a5fa; word-break:break-all; margin-bottom:6px; }
  .badge { display:inline-block; color:#fff; padding:2px 8px; border-radius:6px;
           font-size:11px; font-weight:600; margin-right:4px; background:#2563eb; }
  .hint { font-size:12px; color:#64748b; margin-bottom:8px; }
  code { background:#0f172a; padding:1px 6px; border-radius:4px; }
</style></head><body>
<header>
  <h1>&#128640; Mallory Crawler API <span style="color:#64748b">(Layer 1)</span></h1>
  <div class="sub">
    <a href="/health">/health</a> &middot; <a href="/v1/docs">/v1/docs</a> &middot;
    &rarr; <a href="http://localhost:9090" target="_blank">Ingest Dashboard :9090</a> &middot;
    &rarr; <a href="http://localhost:8000/dashboard" target="_blank">L2 Intelligence :8000</a>
  </div>
  <div class="stats-bar" id="statsBar"></div>
</header>

<div class="toolbar">
  <button onclick="generateJobs()" id="btnGen">&#9889; Generate Jobs from Seed</button>
  <button onclick="runAllJobs()" id="btnRunAll" class="green" disabled>&#9654; Run All Jobs</button>
  <label class="chk"><input type="checkbox" id="freshnessToggle" onchange="updFresh()">
    <span id="freshnessLabel" style="color:#f87171">Freshness filter OFF</span></label>
  <input type="text" id="l2Url" value=\"""" + DEFAULT_L2_URL + """\" placeholder="L2 Ingest URL" style="width:260px;">
  <button onclick="processL2()" class="purple" title="Trigger L2 to process pending pages into intelligence">&#129504; Process in L2</button>
  <span id="jobCount" style="font-size:13px;color:#64748b;"></span>
</div>

<div class="tabs">
  <div class="tab" onclick="showTab('batch')" id="tabBatch">Manual Batch</div>
  <div class="tab" onclick="showTab('jobs')" id="tabJobs">Generated Jobs</div>
</div>

<div id="paneBatch" class="pane">
  <div class="hint">Paste a JSON array of jobs (or a single job). Pages are pushed to
    <b>9090</b> (audit) and, when the L2 URL is set, to <b>8000</b> (intelligence).</div>
  <textarea id="batchInput" spellcheck="false" placeholder='[{"job_id":"m1","job_type":"news","seed_urls":["https://..."],"keywords":["defence"],"max_pages":1,"max_depth":0,"render_js":false,"capture":["html","text"]}]'></textarea>
  <div style="margin-top:10px;display:flex;gap:10px;align-items:center;">
    <button onclick="runBatch()" class="green" id="btnBatch">&#9654; Run Batch</button>
    <select id="batchFwd">
      <option value="true" selected>Push to Ingest API too</option>
      <option value="false">Pull only (return records)</option>
    </select>
  </div>
  <div id="batchResult"></div>
</div>

<div id="paneJobs" class="pane" style="display:none">
  <div class="cards" id="joblist">
    <p style="color:#94a3b8;">Click &#9889; Generate Jobs from Seed to create crawl jobs.</p>
  </div>
</div>

<script>
window.jobs = [];

async function health() {
  try {
    const d = await (await fetch('/health')).json();
    document.getElementById('statsBar').innerHTML =
      `<span class="stat">Entities: <b>${d.entities}</b></span>` +
      `<span class="stat">Sources: <b>${d.sources}</b></span>`;
  } catch(e) {}
}

function updFresh() {
  const on = document.getElementById('freshnessToggle').checked;
  const l = document.getElementById('freshnessLabel');
  l.textContent = on ? 'Freshness filter ON' : 'Freshness filter OFF';
  l.style.color = on ? '#cbd5e1' : '#f87171';
}

function showTab(t) {
  document.getElementById('tabBatch').className = t==='batch'?'tab active':'tab';
  document.getElementById('tabJobs').className  = t==='jobs' ?'tab active':'tab';
  document.getElementById('paneBatch').style.display = t==='batch'?'block':'none';
  document.getElementById('paneJobs').style.display  = t==='jobs' ?'block':'none';
}

// Render one truthful result row from a crawl summary.
function resRow(jobId, s) {
  const acc = s.accepted || 0;
  const cls = acc > 0 ? 'acc' : 'zero';
  const fwd = (s.forwarded_to || []).length
      ? `<span class="chip">&#128228; ${(s.forwarded_to).join(', ')}</span>` : '';
  return `<div class="resrow">
      <span class="jid">${jobId}</span>
      <span class="chip">fetched ${s.fetched||0}</span>
      <span class="chip">kept ${s.kept||0}</span>
      <span class="chip">sent ${s.sent||0}</span>
      <span class="chip ${cls}">accepted ${acc}</span>
      ${fwd}
    </div>`;
}

async function runBatch() {
  const input = document.getElementById('batchInput').value.trim();
  const forward = document.getElementById('batchFwd').value === 'true';
  const l2Url = document.getElementById('l2Url').value.trim();
  const freshnessOn = document.getElementById('freshnessToggle').checked;
  const el = document.getElementById('batchResult');
  const btn = document.getElementById('btnBatch');
  if (!input) { el.innerHTML = '<div class="status err">Paste job JSON first.</div>'; return; }
  let parsed;
  try { parsed = JSON.parse(input); }
  catch(e) { el.innerHTML = `<div class="status err">Invalid JSON: ${e.message}</div>`; return; }
  let jobs = Array.isArray(parsed) ? parsed
           : (parsed.jobs && Array.isArray(parsed.jobs)) ? parsed.jobs : [parsed];
  // Freshness OFF -> don't drop old articles before the keyword gate.
  if (!freshnessOn) jobs = jobs.map(j => ({...j, freshness_days: null}));

  // The crawler forwards to L2 from the SERVER side, so it needs the URL reachable from the
  // crawler process (a container name like http://l2:8000 in Docker). Prefer the server's
  // configured L2 URL (/v1/config); fall back to whatever the operator typed.
  let fwdUrl = l2Url;
  try {
    const cfg = await (await fetch('/v1/config')).json();
    if (cfg.l2_forward_url) fwdUrl = cfg.l2_forward_url;
  } catch(e) {}

  const body = {jobs, forward_to_ingest: forward};
  if (fwdUrl) body.l2_ingest_url = fwdUrl;

  btn.disabled = true;
  el.innerHTML = '<div class="status wait">&#9203; Crawling...</div>';
  try {
    const r = await fetch('/v1/crawl/batch', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!r.ok) {
      const t = await r.text();
      el.innerHTML = `<div class="status err">HTTP ${r.status}: ${t.substring(0,300)}</div>`;
      btn.disabled = false; return;
    }
    const d = await r.json();
    const rows = (d.results||[]).map(res => resRow(res.job_id||'?', res.summary||{})).join('');
    const totAcc = (d.results||[]).reduce((n,res)=>n+((res.summary||{}).accepted||0),0);
    const tone = totAcc>0 ? 'ok':'err';
    el.innerHTML = `<div class="status ${tone}">&#10003; ${(d.results||[]).length} job(s) done &middot; ${totAcc} pages accepted into L2/ingest</div>` + rows;
  } catch(e) {
    el.innerHTML = `<div class="status err">${e.message}</div>`;
  }
  btn.disabled = false;
}

async function processL2() {
  const l2Url = document.getElementById('l2Url').value.trim() || '""" + DEFAULT_L2_URL + """';
  const el = document.getElementById('batchResult');
  el.innerHTML = '<div class="status wait">&#129504; L2 processing pending pages (via the farm)...</div>';
  try {
    const r = await fetch(l2Url + '/ops/process', {method:'POST'});
    const d = await r.json();
    el.innerHTML = `<div class="status ok">&#10003; L2 processed: signals ${d.signals_processed} &middot; tenders ${d.tenders_processed} &middot; partnerships ${d.partnerships_processed} &middot; geo ${d.geo_processed}.
      View at <a href="${l2Url}/dashboard" target="_blank">${l2Url}/dashboard</a></div>`;
  } catch(e) {
    el.innerHTML = `<div class="status err">Could not reach L2 at ${l2Url}: ${e.message}</div>`;
  }
}

// ── generated jobs (from seed) ──
async function generateJobs() {
  const btn = document.getElementById('btnGen'); btn.disabled = true; btn.textContent = '&#9203; Generating...';
  try {
    const d = await (await fetch('/v1/generate-jobs')).json();
    window.jobs = d.jobs; renderJobs();
    document.getElementById('btnRunAll').disabled = false;
    document.getElementById('jobCount').textContent = `${d.jobs.length} jobs · ${d.by_type_summary}`;
    showTab('jobs');
  } catch(e) {
    document.getElementById('joblist').innerHTML = `<p class="status err">${e.message}</p>`;
  }
  btn.disabled = false; btn.innerHTML = '&#9889; Generate Jobs from Seed';
}

function renderJobs() {
  document.getElementById('joblist').innerHTML = window.jobs.map((j,i) => `
    <div class="card">
      <h3><span class="badge">${j.job_type}</span> ${j.job_id}</h3>
      <div class="meta">entity: ${j.target_entity||'—'} · pages: ${j.max_pages} · depth: ${j.max_depth}</div>
      <div class="urls">${j.seed_urls.map(u=>`<a href="${u}" target="_blank">${u.substring(0,60)}…</a>`).join('<br>')}</div>
      <div class="meta">keywords: ${(j.keywords||[]).join(', ')||'—'}</div>
      <div id="res-${i}"></div>
      <button onclick="runOne(${i})" style="margin-top:8px;">&#9654; Run</button>
    </div>`).join('');
}

async function runOne(i) {
  const job = window.jobs[i];
  const l2Url = document.getElementById('l2Url').value.trim();
  const freshnessOn = document.getElementById('freshnessToggle').checked;
  const el = document.getElementById('res-'+i);
  el.innerHTML = '<div class="status wait">&#9203; Running...</div>';
  try {
    const body = {...job, forward_to_ingest: true};
    if (!freshnessOn) body.freshness_days = null;
    if (l2Url) body.l2_ingest_url = l2Url;
    const d = await (await fetch('/v1/crawl', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    })).json();
    el.innerHTML = resRow(job.job_id, d.summary||{});
  } catch(e) { el.innerHTML = `<div class="status err">${e.message}</div>`; }
}

async function runAllJobs() {
  const btn = document.getElementById('btnRunAll'); btn.disabled = true; btn.innerHTML = '&#9203; Running...';
  for (let i=0;i<window.jobs.length;i++) await runOne(i);
  btn.disabled = false; btn.innerHTML = '&#9654; Run All Jobs';
}

showTab('batch');
health();
</script>
</body></html>"""
