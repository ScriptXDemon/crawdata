"""Inline HTML dashboard for the Crawler API (port 8099).

Lets you generate jobs from the seed, inspect them, and trigger crawl runs —
all from the browser without needing the orchestrator.
"""
from __future__ import annotations

import html


def _esc(v) -> str:
    return html.escape(str(v))


def render() -> str:
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mallory Crawler API — Job Dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin:0;
         background:#0f172a; color:#e2e8f0; }
  header { padding:18px 24px; background:#111827; border-bottom:1px solid #1f2937; }
  h1 { margin:0 0 4px; font-size:18px; }
  .sub { font-size:12px; color:#64748b; }
  .row { padding: 16px 24px; }
  button { background:#2563eb; color:#fff; border:none; padding:8px 18px;
           border-radius:8px; font-size:13px; cursor:pointer; margin-right:8px; }
  button:hover { background:#1d4ed8; }
  button.danger { background:#b91c1c; }
  button.danger:hover { background:#991b1b; }
  button.green { background:#15803d; }
  button.green:hover { background:#166534; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr));
           gap:12px; padding:0 24px 24px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }
  .card h3 { margin:0 0 6px; font-size:14px; }
  .card .meta { font-size:11px; color:#94a3b8; margin-bottom:4px; }
  .card .urls { font-size:11px; color:#60a5fa; word-break:break-all; margin-bottom:6px; }
  .badge { display:inline-block; color:#fff; padding:2px 8px; border-radius:6px;
           font-size:11px; font-weight:600; margin-right:4px; }
  .b-news { background:#2563eb; }
  .b-tender { background:#b45309; }
  .b-profile { background:#0d9488; }
  .b-spec { background:#7c3aed; }
  .b-tech { background:#15803d; }
  .b-market { background:#2563eb; }
  .status { font-size:12px; margin-top:8px; padding:6px 10px; border-radius:6px; }
  .status.ok { background:#064e3b; color:#a7f3d0; }
  .status.err { background:#7c2d12; color:#fed7aa; }
  .status.wait { background:#1e3a5f; color:#93c5fd; }
  .summary { margin-top:6px; font-size:11px; color:#cbd5e1; }
  .summary span { margin-right:12px; }
  .tabs { display:flex; gap:1px; padding:0 24px; margin-top:12px; }
  .tab { padding:7px 16px; background:#1e293b; border:1px solid #334155; border-bottom:none;
         border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; color:#94a3b8; }
  .tab.active { background:#0f172a; color:#e2e8f0; }
  .batch { padding:0 24px 16px; }
  .batch textarea { width:100%; min-height:120px; background:#1e293b; color:#e2e8f0;
                     border:1px solid #334155; border-radius:8px; padding:10px;
                     font:13px monospace; margin-bottom:8px; }
  .batch select { background:#1e293b; color:#e2e8f0; border:1px solid #334155; padding:6px;
                  border-radius:6px; font-size:13px; margin-left:8px; }
  code { background:#0f172a; padding:1px 6px; border-radius:4px; }
  a { color:#60a5fa; }
  #joblist { max-height:600px; overflow-y:auto; }
  .stats-bar { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  .stat { background:#1e293b; padding:3px 9px; border-radius:12px; font-size:12px; }
</style></head><body>
<header>
  <h1>🚀 Mallory Crawler API <span style="color:#64748b">(Layer 1)</span></h1>
  <div class="sub">
    <a href="/health">/health</a> · <a href="/v1/docs">/v1/docs</a> ·
    <a href="/v1/schema">/v1/schema</a> ·
    <span>→</span> <a href="http://localhost:9090">Ingest Dashboard :9090</a>
  </div>
  <div class="stats-bar" id="statsBar"></div>
</header>

<div class="row">
  <button onclick="generateJobs()" id="btnGen">⚡ Generate Jobs from Seed</button>
  <button onclick="runAllJobs()" id="btnRunAll" class="green" disabled>▶ Run All Jobs</button>
  <label style="margin-left:12px;font-size:13px;color:#cbd5e1;cursor:pointer;">
    <input type="checkbox" id="freshnessToggle" checked onchange="updateFreshnessLabel()">
    <span id="freshnessLabel">Freshness filter ON</span>
  </label>
  <input id="l2Url" type="text" placeholder="L2 Ingest URL (e.g. http://192.168.5.153:8000)" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;padding:7px 12px;border-radius:8px;font-size:13px;width:320px;margin-left:12px;">
  <span id="jobCount" style="font-size:13px;color:#64748b;margin-left:8px;"></span>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('jobs')" id="tabJobs">Jobs</div>
  <div class="tab" onclick="showTab('batch')" id="tabBatch">Manual Batch</div>
</div>

<div id="tabJobsPane">
  <div class="cards" id="joblist">
    <p style="padding:20px;color:#94a3b8;">Click "⚡ Generate Jobs from Seed" to create crawl jobs.</p>
  </div>
</div>
<div id="tabBatchPane" style="display:none">
  <div class="batch">
    <p style="font-size:13px;color:#94a3b8;">Paste JSON array of jobs or a single job object.</p>
    <textarea id="batchInput" placeholder='[{"job_id":"manual_1","job_type":"news","seed_urls":["https://example.com"],...}]'></textarea>
    <button onclick="runBatch()" class="green">▶ Run Batch</button>
    <select id="batchFwd">
      <option value="false">Pull only (return records)</option>
      <option value="true">Push to Ingest API too</option>
    </select>
  </div>
  <div id="batchResult" style="padding:0 24px;"></div>
</div>

<script>
window.jobs = [];
window.results = {};

async function health() {
  const r = await fetch('/health');
  const d = await r.json();
  document.getElementById('statsBar').innerHTML =
    `<span class="stat">Entities: <b>${d.entities}</b></span>` +
    `<span class="stat">Sources: <b>${d.sources}</b></span>`;
}

async function generateJobs() {
  const btn = document.getElementById('btnGen');
  btn.disabled = true;
  btn.textContent = '⏳ Generating...';
  try {
    const r = await fetch('/v1/generate-jobs');
    const d = await r.json();
    window.jobs = d.jobs;
    renderJobs();
    document.getElementById('btnRunAll').disabled = false;
    document.getElementById('jobCount').textContent = `${d.jobs.length} jobs · ${d.by_type_summary}`;
  } catch(e) {
    document.getElementById('joblist').innerHTML =
      `<p class="status err">Error: ${e.message}</p>`;
  }
  btn.disabled = false;
  btn.textContent = '⚡ Generate Jobs from Seed';
}

function badge(jobType) {
  const cls = 'b-' + (jobType || 'news');
  return `<span class="badge ${cls}">${jobType}</span>`;
}

function renderJobs() {
  const el = document.getElementById('joblist');
  el.innerHTML = window.jobs.map((j,i) => `
    <div class="card">
      <h3>${badge(j.job_type)} ${j.job_id}</h3>
      <div class="meta">entity: ${j.target_entity || '—'} · pages: ${j.max_pages} · depth: ${j.max_depth}</div>
      <div class="urls">${j.seed_urls.map(u => `<a href="${u}" target="_blank">${u.substring(0,60)}...</a>`).join('<br>')}</div>
      <div class="meta">keywords: ${(j.keywords||[]).join(', ') || '—'} · capture: ${(j.capture||[]).join(', ')}</div>
      <div id="res-${i}"></div>
      <button onclick="runOne(${i})" style="margin-top:8px;">▶ Run</button>
    </div>
  `).join('');
}

async function runOne(idx) {
  const job = window.jobs[idx];
  const l2Url = document.getElementById('l2Url').value.trim();
  const freshnessOn = document.getElementById('freshnessToggle').checked;
  const el = document.getElementById('res-' + idx);
  el.innerHTML = '<div class="status wait">⏳ Running...</div>';
  try {
    const body = {...job, forward_to_ingest: true};
    if (!freshnessOn) body.freshness_days = null;
    if (l2Url) body.l2_ingest_url = l2Url;
    const r = await fetch('/v1/crawl', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    const s = d.summary;
    const targets = s.forwarded_to || [];
    el.innerHTML = `<div class="status ok">✓ Done</div>
      <div class="summary">
        <span>fetched: <b>${s.fetched}</b></span>
        <span>kept: <b>${s.kept}</b></span>
        <span>emitted: <b>${s.records_emitted}</b></span>
        <span>accepted: <b>${s.records_accepted}</b></span>
        <span>rejected: <b>${s.records_rejected}</b></span>
        ${targets.length ? '<span>📤 forwarded: <b>' + targets.join(', ') + '</b></span>' : ''}
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="status err">✗ ${e.message}</div>`;
  }
}

async function runAllJobs() {
  const btn = document.getElementById('btnRunAll');
  btn.disabled = true;
  btn.textContent = '⏳ Running...';
  for (let i = 0; i < window.jobs.length; i++) {
    await runOne(i);
  }
  btn.disabled = false;
  btn.textContent = '▶ Run All Jobs';
}

async function runBatch() {
  const input = document.getElementById('batchInput').value.trim();
  const forward = document.getElementById('batchFwd').value === 'true';
  const l2Url = document.getElementById('l2Url').value.trim();
  const el = document.getElementById('batchResult');
  if (!input) { el.innerHTML = '<p class="status err">Paste job JSON first.</p>'; return; }
  el.innerHTML = '<div class="status wait">⏳ Submitting...</div>';
  try {
    const parsed = JSON.parse(input);
    let jobs;
    if (Array.isArray(parsed)) {
      jobs = parsed;
    } else if (parsed.jobs && Array.isArray(parsed.jobs)) {
      jobs = parsed.jobs;
    } else {
      jobs = [parsed];
    }
    const body = {jobs: jobs, forward_to_ingest: forward};
    if (l2Url) body.l2_ingest_url = l2Url;
    fetch('/v1/crawl/batch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    }).then(r => { if(r.ok) r.json().then(d => showBatchResults(d)); else r.json().then(d => el.innerHTML = `<div class="status err">✗ HTTP ${r.status}: ${JSON.stringify(d).substring(0,300)}</div>`); });
    // Poll for progress
    pollProgress(el);
  } catch(e) {
    el.innerHTML = `<div class="status err">✗ ${e.message}</div>`;
  }
}

function pollProgress(el) {
  fetch('/v1/batch/status').then(r => r.json()).then(s => {
    if (!s.running && s.done > 0) return; // results already shown
    if (s.total > 0) {
      const pct = Math.round(s.done / s.total * 100);
      el.innerHTML = `<div class="status wait">⏳ Running batch... ${s.done}/${s.total} (${pct}%) — currently: ${s.current_job || 'done'}</div>`;
      s.results.forEach(r => {
        el.innerHTML += `<div class="summary">✓ <b>${r.job_id}</b>: fetched ${r.fetched} · kept ${r.kept} · emitted ${r.emitted} · accepted ${r.accepted}</div>`;
      });
    }
    if (s.running || s.done < s.total) {
      setTimeout(() => pollProgress(el), 2000);
    }
  });
}

function showBatchResults(d) {
  const el = document.getElementById('batchResult');
  if (!d.results) return;
  el.innerHTML = `<div class="status ok">✓ ${d.jobs} job(s) completed</div>`;
  d.results.forEach((res) => {
    const s = res.summary || {};
    el.innerHTML += `<div class="summary">
      <b>${res.job_id || '?'}</b>:
      fetched ${s.fetched || 0} · kept ${s.kept || 0} ·
      emitted ${s.records_emitted || 0} · accepted ${s.records_accepted || 0}
    </div>`;
  });
}

function showTab(tab) {
  document.getElementById('tabJobs').className = tab==='jobs'?'tab active':'tab';
  document.getElementById('tabBatch').className = tab==='batch'?'tab active':'tab';
  document.getElementById('tabJobsPane').style.display = tab==='jobs'?'block':'none';
  document.getElementById('tabBatchPane').style.display = tab==='batch'?'block':'none';
}

function updateFreshnessLabel() {
  const on = document.getElementById('freshnessToggle').checked;
  document.getElementById('freshnessLabel').textContent = on ? 'Freshness filter ON' : 'Freshness filter OFF';
  document.getElementById('freshnessLabel').style.color = on ? '#cbd5e1' : '#f87171';
}

health();
</script>
</body></html>"""
