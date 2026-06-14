/* ═══════════════════════════════════════════════════════════════════
   TikTok Scanner — app.js
   ═══════════════════════════════════════════════════════════════════ */

/* ─── Utilities ─────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const esc = s => s == null ? '' : String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

function toast(msg, type = '', duration = 3200) {
  const c = $('toast-container'), t = document.createElement('div');
  t.className = 'toast' + (type ? ' ' + type : '');
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity='0'; t.style.transform='translateY(8px)'; t.style.transition='.2s'; setTimeout(()=>t.remove(),220); }, duration);
}

async function api(path, opts) {
  const res = await fetch('/api' + path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const d = await res.json(); msg = d.detail || JSON.stringify(d); } catch { try { msg = await res.text(); } catch {} }
    throw new Error(msg);
  }
  return res.json();
}

const fmtTime = iso => iso
  ? new Date(iso+'Z').toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})
  : 'never';
const fmtBytes = b => b==null ? '—' : b >= 1024**3 ? (b/1024**3).toFixed(1)+' GB' : (b/1024**2).toFixed(0)+' MB';

/* ─── Theme ─────────────────────────────────────────────────────── */
const root = document.documentElement;
(function() {
  const t = localStorage.getItem('theme') || 'light';
  root.setAttribute('data-theme', t);
  applyThemeUI(t);
})();
function applyThemeUI(t) {
  $('theme-label').textContent = t === 'dark' ? 'Light mode' : 'Dark mode';
  $('theme-icon-sun').style.display = t === 'dark' ? 'block' : 'none';
  $('theme-icon-moon').style.display = t === 'dark' ? 'none' : 'block';
}
$('theme-toggle').onclick = () => {
  const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  applyThemeUI(next);
  redrawCharts();
};

/* ─── Navigation ────────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.tab-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-btn[data-tab]').forEach(b => b.classList.remove('active'));
  const sec = $('tab-'+name); if (sec) sec.classList.add('active');
  const btn = document.querySelector(`.nav-btn[data-tab="${name}"]`); if (btn) btn.classList.add('active');
  if (name==='logs') loadLogs();
  if (name==='dashboard') { loadStatus(); loadStats(); }
  if (name==='categories') loadCategories();
  if (name==='search') populateSearchCatFilter();
}
document.querySelectorAll('.nav-btn[data-tab]').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
document.querySelectorAll('[data-tab-link]').forEach(el => el.addEventListener('click', () => switchTab(el.dataset.tabLink)));

/* ─── Sidebar status dot ────────────────────────────────────────── */
function setSidebarDot(scanning_paused, health_ok) {
  const dot = $('sidebar-status-dot');
  if (scanning_paused) { dot.textContent='● Paused'; dot.className='sidebar-subtitle warn'; }
  else if (health_ok === false) { dot.textContent='⚠ Health issue'; dot.className='sidebar-subtitle err'; }
  else { dot.textContent='● Active'; dot.className='sidebar-subtitle ok'; }
}

/* ─── Dashboard status ──────────────────────────────────────────── */
async function loadStatus() {
  try {
    const s = await api('/status');
    const scanEl = $('s-scanning');
    if (s.scanning_paused) { scanEl.textContent='Paused'; scanEl.className='stat-value warn'; }
    else { scanEl.textContent='Active'; scanEl.className='stat-value ok'; }
    $('s-last-scan').textContent = fmtTime(s.worker_last_run);
    $('s-disk').textContent = s.disk_free_bytes != null ? fmtBytes(s.disk_free_bytes)+' free' : '—';
    const llmEl = $('s-llm');
    if (s.ollama_reachable===true) { llmEl.textContent='Reachable'; llmEl.className='stat-value ok'; }
    else if (s.ollama_reachable===false) { llmEl.textContent='Unreachable'; llmEl.className='stat-value err'; }
    else { llmEl.textContent='API mode'; llmEl.className='stat-value'; }
    const hEl = $('s-health');
    if (s.scraper_health_ok===true) { hEl.textContent='OK'; hEl.className='stat-value ok'; }
    else if (s.scraper_health_ok===false) { hEl.textContent='Issue'; hEl.className='stat-value err'; }
    else { hEl.textContent='Unchecked'; hEl.className='stat-value'; }
    $('pause-label').textContent = s.scanning_paused ? 'Resume scanning' : 'Pause scanning';
    $('pause-toggle').classList.toggle('paused', !!s.scanning_paused);
    $('pause-toggle').dataset.paused = s.scanning_paused ? '1' : '0';
    setSidebarDot(s.scanning_paused, s.scraper_health_ok);
  } catch(e) { console.error(e); }
}

$('refresh-dash-btn').onclick = () => { loadStatus(); loadStats(); };
$('pause-toggle').onclick = async () => {
  const paused = $('pause-toggle').dataset.paused === '1';
  try {
    await api('/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({scanning_paused:!paused}) });
    toast(paused ? 'Scanning resumed' : 'Scanning paused', paused ? 'success' : '');
    loadStatus();
  } catch(e) { toast(e.message, 'error'); }
};

async function scanAll(feedbackEl) {
  try {
    const r = await api('/categories/scan-all', {method:'POST'});
    toast(`Scanning ${r.started.length} categories — check Logs for progress`, 'success');
    if (feedbackEl) feedbackEl.textContent = `Scan started for ${r.started.length} categories.`;
  } catch(e) { toast(e.message,'error'); }
}
['scan-all-btn','scan-all-btn2','scan-all-cats-btn'].forEach(id => {
  const el = $(id); if(el) el.onclick = () => scanAll($('dash-feedback'));
});

/* ─── Charts ────────────────────────────────────────────────────── */
let chartCats = null, chartDaily = null;

function chartColors() {
  const dark = root.getAttribute('data-theme') === 'dark';
  return {
    grid: dark ? 'rgba(255,255,255,.06)' : 'rgba(0,0,0,.06)',
    text: dark ? '#9b9b9b' : '#6b6b6b',
    accent: '#10a37f',
    bar: dark ? 'rgba(16,163,127,.7)' : 'rgba(16,163,127,.8)',
  };
}

function makeChart(id, type, data, options) {
  const el = $(id);
  if (!el) return null;
  const ctx = el.getContext('2d');
  return new Chart(ctx, { type, data, options });
}

function redrawCharts() {
  if (chartCats) { chartCats.destroy(); chartCats = null; }
  if (chartDaily) { chartDaily.destroy(); chartDaily = null; }
  loadStats();
}

async function loadStats() {
  try {
    const stats = await api('/stats');
    const cc = chartColors();

    // Total matches
    const totalMatches = stats.categories.reduce((s,c) => s + (c.matches||0), 0);
    $('s-total-matches').textContent = totalMatches.toLocaleString();

    // Category match rate bar table
    renderDashCatTable(stats.categories);

    // Chart: matches per category
    if (chartCats) chartCats.destroy();
    chartCats = makeChart('chart-cats', 'bar', {
      labels: stats.categories.map(c => c.name.length > 14 ? c.name.slice(0,14)+'…' : c.name),
      datasets: [{
        label: 'Matches', data: stats.categories.map(c => c.matches||0),
        backgroundColor: cc.bar, borderRadius: 4, borderSkipped: false,
      }, {
        label: 'Rejected', data: stats.categories.map(c => c.rejected||0),
        backgroundColor: cc.grid, borderRadius: 4, borderSkipped: false,
      }]
    }, {
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{labels:{color:cc.text,font:{size:11,family:'Inter'}}} },
      scales:{
        x:{ ticks:{color:cc.text,font:{size:11}}, grid:{color:cc.grid} },
        y:{ ticks:{color:cc.text,font:{size:11}}, grid:{color:cc.grid}, beginAtZero:true }
      }
    });

    // Chart: daily videos found
    const days = stats.daily.map(d => d.day.slice(5)); // MM-DD
    const counts = stats.daily.map(d => d.count);
    if (chartDaily) chartDaily.destroy();
    chartDaily = makeChart('chart-daily', 'line', {
      labels: days,
      datasets: [{
        label: 'Videos found', data: counts,
        borderColor: cc.accent, backgroundColor: 'rgba(16,163,127,.1)',
        fill: true, tension: .35, pointRadius: 3, pointBackgroundColor: cc.accent,
      }]
    }, {
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{labels:{color:cc.text,font:{size:11,family:'Inter'}}} },
      scales:{
        x:{ ticks:{color:cc.text,font:{size:11}}, grid:{color:cc.grid} },
        y:{ ticks:{color:cc.text,font:{size:11}}, grid:{color:cc.grid}, beginAtZero:true }
      }
    });

  } catch(e) { console.error('stats error', e); }
}

function renderDashCatTable(cats) {
  const wrap = $('dash-cat-table');
  if (!cats.length) { wrap.innerHTML = '<p class="text-subtle" style="padding:16px">No categories yet.</p>'; return; }
  const rows = cats.map(c => {
    const pct = c.total > 0 ? Math.round((c.matches||0) / c.total * 100) : 0;
    return `<tr>
      <td><strong>${esc(c.name)}</strong></td>
      <td>${(c.matches||0).toLocaleString()}</td>
      <td>${(c.rejected||0).toLocaleString()}</td>
      <td>
        <div class="match-bar-wrap">
          <div class="match-bar-bg"><div class="match-bar-fill" style="width:${pct}%"></div></div>
          <span class="match-pct">${pct}%</span>
        </div>
      </td>
      <td><button class="btn btn-secondary btn-xs" onclick="scanOne(${c.id}, '${esc(c.name)}')">Scan</button></td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class="dash-table">
    <thead><tr><th>Category</th><th>Matches</th><th>Rejected</th><th style="min-width:140px">Match rate</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function scanOne(catId, name) {
  try {
    await api(`/categories/${catId}/scan`, {method:'POST'});
    toast(`Scan started for "${name}"`, 'success');
  } catch(e) { toast(e.message,'error'); }
}
window.scanOne = scanOne;

/* ─── Categories ────────────────────────────────────────────────── */
const expandedCats = new Set();

async function loadCategories() {
  const c = $('categories-list');
  c.innerHTML = '<div class="text-muted" style="padding:8px">Loading…</div>';
  try {
    const cats = await api('/categories');
    c.innerHTML = '';
    if (!cats.length) {
      c.innerHTML = `<div class="empty-state"><p>No categories yet. <button class="btn btn-ghost btn-sm" data-tab-link="new-category">Add one →</button></p></div>`;
      c.querySelector('[data-tab-link]')?.addEventListener('click', () => switchTab('new-category'));
      return;
    }
    cats.forEach(cat => c.appendChild(buildCategoryCard(cat)));
  } catch(e) { c.innerHTML = `<p class="text-muted">Error: ${esc(e.message)}</p>`; }
}

function buildCategoryCard(cat) {
  const card = document.createElement('div');
  card.className = 'category-card';
  const streak = cat.empty_scan_streak || 0;
  card.innerHTML = `
    <div class="category-card-header">
      <div class="category-card-title">
        ${esc(cat.name)}
        ${cat.enabled ? '' : '<span class="badge badge-off">disabled</span>'}
        ${streak >= 3 ? `<span class="badge badge-warn">⚠ ${streak} empty scans</span>` : ''}
      </div>
    </div>
    <div class="category-card-meta">
      <div class="meta-row"><strong>Terms:</strong> ${cat.search_terms.map(esc).join(', ')}</div>
      <div class="meta-row"><strong>Prompt:</strong> ${esc(cat.prompt.slice(0,120))}${cat.prompt.length>120?'…':''}</div>
      <div class="meta-row"><strong>Last scanned:</strong> ${fmtTime(cat.last_scanned)}</div>
    </div>
    <div class="category-card-actions">
      <button class="btn btn-primary btn-sm" data-a="scan">Scan now</button>
      <button class="btn btn-secondary btn-sm" data-a="videos">Show videos</button>
      <button class="btn btn-secondary btn-sm" data-a="edit">Edit</button>
      <button class="btn btn-secondary btn-sm" data-a="csv">Export CSV</button>
      <button class="btn btn-secondary btn-sm" data-a="json">Export JSON</button>
      <button class="btn btn-secondary btn-sm" data-a="dlall">Download all</button>
      <button class="btn btn-danger btn-sm" data-a="del">Delete</button>
    </div>
    <div class="videos-panel" style="display:none"></div>`;

  const vp = card.querySelector('.videos-panel');
  const vBtn = card.querySelector('[data-a="videos"]');

  card.querySelector('[data-a="scan"]').onclick = async () => {
    try { await api(`/categories/${cat.id}/scan`,{method:'POST'}); toast(`Scan started for "${cat.name}"`, 'success'); }
    catch(e) { toast(e.message,'error'); }
  };
  card.querySelector('[data-a="edit"]').onclick = () => editCategory(cat);
  card.querySelector('[data-a="csv"]').onclick = () => { location.href=`/api/categories/${cat.id}/export?format=csv`; };
  card.querySelector('[data-a="json"]').onclick = () => { location.href=`/api/categories/${cat.id}/export?format=json`; };
  card.querySelector('[data-a="dlall"]').onclick = async () => {
    try {
      const res = await fetch(`/api/categories/${cat.id}/download-all`);
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const a = Object.assign(document.createElement('a'), {href:URL.createObjectURL(blob), download:cat.name.replace(/[^a-zA-Z0-9_-]+/g,'_')+'_videos.zip'});
      document.body.appendChild(a); a.click(); a.remove();
    } catch(e) { toast('Download failed: '+e.message,'error'); }
  };
  card.querySelector('[data-a="del"]').onclick = async () => {
    if (!confirm(`Delete "${cat.name}"?`)) return;
    try { await api(`/categories/${cat.id}`,{method:'DELETE'}); toast(`"${cat.name}" deleted`); loadCategories(); }
    catch(e) { toast(e.message,'error'); }
  };
  vBtn.onclick = async () => {
    const open = vp.style.display !== 'none';
    vp.style.display = open ? 'none' : 'block';
    vBtn.textContent = open ? 'Show videos' : 'Hide videos';
    if (open) { expandedCats.delete(cat.id); } else { expandedCats.add(cat.id); await loadVideosInto(cat.id, vp); }
  };
  if (expandedCats.has(cat.id)) { vp.style.display='block'; vBtn.textContent='Hide videos'; loadVideosInto(cat.id,vp); }
  return card;
}

async function loadVideosInto(catId, panel) {
  panel.innerHTML = '<div class="videos-grid"><div class="text-muted">Loading…</div></div>';
  try {
    const [dl, ma] = await Promise.all([api(`/categories/${catId}/videos?status=downloaded`), api(`/categories/${catId}/videos?status=matched`)]);
    const all = [...dl, ...ma];
    if (!all.length) { panel.innerHTML='<div class="videos-grid"><p class="text-subtle">No matches yet.</p></div>'; return; }
    const grid = document.createElement('div'); grid.className='videos-grid';
    all.forEach(v => grid.appendChild(buildVideoItem(v)));
    panel.innerHTML=''; panel.appendChild(grid);
  } catch(e) { panel.innerHTML=`<div class="videos-grid text-muted">Error: ${esc(e.message)}</div>`; }
}

function buildVideoItem(v) {
  const item = document.createElement('div'); item.className='video-item';
  const thumb = v.thumbnail
    ? `<img class="video-thumb" src="${esc(v.thumbnail)}" alt="" loading="lazy">`
    : `<div class="video-thumb-placeholder"><svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" d="M15 10l4.553-2.069A1 1 0 0121 8.82v6.36a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg></div>`;
  const dl = v.file_path ? `<a href="/api/videos/${v.id}/download">Download</a>` : `<span>Cache expired</span>`;
  item.innerHTML = `${thumb}
    <div class="video-body">
      <div class="video-title">${esc((v.title||'(no title)').slice(0,160))}</div>
      <div class="video-sub">by ${esc(v.author||'unknown')} · <span class="badge ${v.status==='downloaded'?'badge-active':'badge-match'}">${esc(v.status)}</span>${v.category_name?` · <span class="text-subtle">${esc(v.category_name)}</span>`:''}</div>
      ${v.reasoning ? `<div class="video-reason">${esc(v.reasoning.slice(0,160))}</div>` : ''}
      <div class="video-links"><a href="${esc(v.url)}" target="_blank" rel="noopener">View on TikTok</a>${dl}</div>
    </div>`;
  return item;
}

/* ─── Category form ─────────────────────────────────────────────── */
function editCategory(cat) {
  $('cat-id').value = cat.id;
  $('cat-name').value = cat.name;
  $('cat-terms').value = cat.search_terms.join(', ');
  $('cat-prompt').value = cat.prompt;
  $('cat-enabled').checked = !!cat.enabled;
  $('cat-cancel').style.display = 'inline-flex';
  $('form-page-title').textContent = `Edit: ${cat.name}`;
  $('examples-box').style.display = 'block';
  $('test-prompt-result').innerHTML = '';
  loadExamples(cat.id);
  switchTab('new-category');
}
function resetCatForm() {
  $('category-form').reset();
  $('cat-id').value=''; $('cat-enabled').checked=true;
  $('cat-cancel').style.display='none';
  $('form-page-title').textContent='New Category';
  $('examples-box').style.display='none';
  $('examples-list').innerHTML=''; $('test-prompt-result').innerHTML='';
}
$('cat-cancel').onclick = () => { resetCatForm(); switchTab('categories'); };
$('category-form').onsubmit = async e => {
  e.preventDefault();
  const id = $('cat-id').value;
  const terms = $('cat-terms').value.split(',').map(s=>s.trim()).filter(Boolean);
  if (!terms.length) { toast('Enter at least one search term','warn'); return; }
  const payload = { name:$('cat-name').value.trim(), search_terms:terms, prompt:$('cat-prompt').value.trim(), enabled:$('cat-enabled').checked };
  try {
    if (id) {
      await api(`/categories/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      toast('Category updated','success');
    } else {
      const res = await api('/categories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      $('cat-id').value=res.id; $('form-page-title').textContent=`Edit: ${payload.name}`;
      $('cat-cancel').style.display='inline-flex'; $('examples-box').style.display='block';
      loadExamples(res.id); toast('Category created','success');
    }
    loadCategories(); loadStats();
  } catch(e) { toast('Error: '+e.message,'error'); }
};

/* ─── Test prompt ───────────────────────────────────────────────── */
$('test-prompt-btn').onclick = async () => {
  const url=$('test-prompt-url').value.trim(), prompt=$('cat-prompt').value.trim(), rd=$('test-prompt-result');
  if (!url) { toast('Enter a TikTok URL','warn'); return; }
  if (!prompt) { toast('Enter a match prompt first','warn'); return; }
  rd.innerHTML='<div class="text-muted">Fetching metadata and running AI — may take a minute…</div>';
  try {
    const catId=$('cat-id').value;
    const body={url,prompt}; if(catId) body.category_id=parseInt(catId,10);
    const r=await api('/test-prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const badge=r.match?'<span class="badge badge-match">MATCH</span>':'<span class="badge badge-off">NO MATCH</span>';
    rd.innerHTML=`<div class="result-box ${r.match?'match':'no-match'}">
      <div style="margin-bottom:6px">${badge}</div>
      <div style="font-weight:600;margin-bottom:3px">${esc((r.meta.title||'(no title)').slice(0,200))}</div>
      <div class="text-subtle">by ${esc(r.meta.author||'unknown')}</div>
      ${r.meta.tags.length?`<div class="text-subtle">Tags: ${r.meta.tags.map(esc).join(', ')}</div>`:''}
      <div class="mt-8"><strong>Reason:</strong> ${esc(r.reason)}</div>
    </div>`;
  } catch(e) { rd.innerHTML=`<div class="result-box error">Error: ${esc(e.message)}</div>`; }
};

/* ─── Examples ──────────────────────────────────────────────────── */
async function loadExamples(catId) {
  const list=$('examples-list'); list.innerHTML='<div class="text-muted">Loading…</div>';
  try {
    const exs=await api(`/categories/${catId}/examples`);
    if (!exs.length) { list.innerHTML='<div class="text-subtle">No examples yet.</div>'; return; }
    list.innerHTML='';
    exs.forEach(ex => {
      const badge=ex.label==='match'?'<span class="badge badge-match">Match</span>':'<span class="badge badge-off">Not a match</span>';
      const d=document.createElement('div'); d.className='result-box'; d.style.marginBottom='8px';
      d.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <div>${badge} <span style="font-weight:500;margin-left:6px">${esc((ex.title||'(no title)').slice(0,120))}</span> <span class="text-subtle">— ${esc(ex.author||'unknown')}</span></div>
        <button class="btn btn-danger btn-xs rem-ex" data-id="${ex.id}">Remove</button>
      </div>`;
      d.querySelector('.rem-ex').onclick=async()=>{
        try { await api(`/categories/${catId}/examples/${ex.id}`,{method:'DELETE'}); loadExamples(catId); }
        catch(e) { toast(e.message,'error'); }
      };
      list.appendChild(d);
    });
  } catch(e) { list.innerHTML=`<div class="text-muted">Error: ${esc(e.message)}</div>`; }
}
$('add-example-btn').onclick=async()=>{
  const catId=$('cat-id').value;
  if (!catId) { toast('Save the category first','warn'); return; }
  const url=$('example-url').value.trim();
  if (!url) { toast('Enter a TikTok URL','warn'); return; }
  const btn=$('add-example-btn'); btn.disabled=true; btn.textContent='Adding…';
  try {
    await api(`/categories/${catId}/examples`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,label:$('example-label').value})});
    $('example-url').value=''; loadExamples(catId); toast('Example added','success');
  } catch(e) { toast('Error: '+e.message,'error'); }
  finally { btn.disabled=false; btn.textContent='Add'; }
};

/* ─── Search ────────────────────────────────────────────────────── */
async function populateSearchCatFilter() {
  try {
    const cats=await api('/categories');
    const sel=$('search-cat');
    // keep first option
    while(sel.options.length>1) sel.remove(1);
    cats.forEach(c => { const o=document.createElement('option'); o.value=c.id; o.textContent=c.name; sel.appendChild(o); });
  } catch {}
}
async function doSearch() {
  const q=$('search-q').value.trim();
  if (!q) { toast('Enter a search term','warn'); return; }
  const catId=$('search-cat').value, status=$('search-status').value;
  let path=`/videos/search?q=${encodeURIComponent(q)}`;
  if (catId) path+=`&cat_id=${catId}`;
  if (status) path+=`&status=${status}`;
  const res=$('search-results'), cnt=$('search-count');
  res.innerHTML='<div class="text-muted">Searching…</div>';
  try {
    const videos=await api(path);
    cnt.textContent=`${videos.length} result${videos.length!==1?'s':''}`;
    if (!videos.length) { res.innerHTML='<div class="empty-state"><p>No videos found.</p></div>'; return; }
    const grid=document.createElement('div'); grid.className='card';
    const inner=document.createElement('div'); inner.className='videos-grid';
    videos.forEach(v => inner.appendChild(buildVideoItem(v)));
    grid.appendChild(inner); res.innerHTML=''; res.appendChild(grid);
  } catch(e) { res.innerHTML=`<div class="text-muted">Error: ${esc(e.message)}</div>`; }
}
$('search-btn').onclick=doSearch;
$('search-q').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });

/* ─── Live log stream ───────────────────────────────────────────── */
const logLines = [];
let logLevelFilter = 'ALL';
let logTextFilter = '';
let logEs = null;

function startLogStream() {
  if (logEs) logEs.close();
  logEs = new EventSource('/api/logs/stream');
  logEs.onopen = () => {
    $('log-stream-status').textContent = 'Live';
    $('log-live-dot').classList.add('live');
  };
  logEs.onerror = () => {
    $('log-stream-status').textContent = 'Disconnected — will retry';
    $('log-live-dot').classList.remove('live');
  };
  logEs.onmessage = e => {
    try {
      const line = JSON.parse(e.data);
      logLines.unshift(line); // newest first
      if (logLines.length > 1000) logLines.pop();
      renderLogs();
    } catch {}
  };
}

function renderLogs() {
  const out = $('logs-output');
  const filtered = logLines.filter(l => {
    if (logLevelFilter !== 'ALL' && l.level !== logLevelFilter) return false;
    if (logTextFilter && !(`${l.ts} ${l.level} ${l.message}`).toLowerCase().includes(logTextFilter)) return false;
    return true;
  });
  out.innerHTML = filtered.slice(0, 500).map(l =>
    `<span class="log-${l.level}">[${esc(l.ts)}] ${esc(l.level)}: ${esc(l.message)}</span>`
  ).join('\n');
}

document.querySelectorAll('.log-filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.log-filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    logLevelFilter = btn.dataset.level;
    renderLogs();
  });
});
$('log-search').addEventListener('input', e => {
  logTextFilter = e.target.value.toLowerCase();
  renderLogs();
});
$('clear-logs-btn').onclick = () => { logLines.length=0; renderLogs(); };

async function loadLogs() {
  // Load historical logs, then the SSE stream adds new ones
  try {
    const logs = await api('/logs?limit=500');
    logLines.length = 0;
    logs.forEach(l => logLines.push(l)); // already newest first from API
    renderLogs();
  } catch(e) { $('logs-output').textContent='Failed to load logs: '+e.message; }
}
$('refresh-logs').onclick = loadLogs;
startLogStream();

/* ─── Settings ──────────────────────────────────────────────────── */
function toggleProviderUI() {
  const p=$('llm-provider').value;
  $('ollama-settings').style.display=p==='ollama'?'block':'none';
  $('api-settings').style.display=p==='api'?'block':'none';
}
$('llm-provider').onchange=toggleProviderUI;

async function loadSettings() {
  try {
    const s=await api('/settings');
    $('llm-provider').value=s.llm_provider;
    $('ollama-url').value=s.ollama_url;
    $('ollama-model').value=s.ollama_model;
    $('api-base-url').value=s.api_base_url;
    $('api-key').value='';
    $('api-key').placeholder=s.api_key==='***'?'(saved — leave blank to keep)':'Enter API key';
    $('api-model').value=s.api_model;
    $('storage-path').value=s.storage_path;
    $('cookies-path').value=s.cookies_path||'';
    $('scan-interval').value=s.scan_interval_minutes;
    $('videos-per-scan').value=s.videos_per_scan;
    $('scrape-concurrency').value=s.scrape_concurrency;
    $('request-delay').value=s.request_delay_seconds;
    $('discovery-retries').value=s.discovery_retries;
    $('llm-batch-size').value=s.llm_batch_size;
    $('cache-ttl').value=s.cache_ttl_hours;
    $('delete-after-download').checked=!!s.delete_after_download;
    $('ollama-think').checked=!!s.ollama_think;
    $('ollama-num-ctx').value=s.ollama_num_ctx;
    $('ollama-num-predict').value=s.ollama_num_predict;
    $('ollama-keep-alive').value=s.ollama_keep_alive;
    $('webhook-url').value=s.webhook_url||'';
    $('auto-update-ytdlp').checked=s.auto_update_ytdlp!==false;
    toggleProviderUI();
    try { const v=await api('/yt-dlp-version'); $('ytdlp-version-text').textContent=v.version?`yt-dlp ${v.version}`:''; } catch {}
    if (s.llm_provider==='ollama') {
      try { const m=await api('/ollama-models'); $('ollama-models-hint').textContent=Array.isArray(m)&&m.length?'Available: '+m.join(', '):''; } catch {}
    }
    try {
      const boot=await api('/boot-status');
      $('start-on-boot').disabled=!boot.ok;
      $('start-on-boot').checked=boot.ok&&!!boot.enabled;
      $('boot-toggle-hint').textContent=boot.ok?'':('Unavailable: '+(boot.error||'cannot check systemd'));
    } catch(e) { $('start-on-boot').disabled=true; $('boot-toggle-hint').textContent='Unavailable: '+e.message; }
  } catch(e) { toast('Could not load settings: '+e.message,'error'); }
}

$('settings-form').onsubmit=async e=>{
  e.preventDefault();
  const payload={
    llm_provider:$('llm-provider').value,
    ollama_url:$('ollama-url').value.trim(),
    ollama_model:$('ollama-model').value.trim(),
    api_base_url:$('api-base-url').value.trim(),
    api_model:$('api-model').value.trim(),
    storage_path:$('storage-path').value.trim(),
    cookies_path:$('cookies-path').value.trim(),
    scan_interval_minutes:parseInt($('scan-interval').value,10)||30,
    videos_per_scan:parseInt($('videos-per-scan').value,10)||15,
    scrape_concurrency:parseInt($('scrape-concurrency').value,10)||4,
    request_delay_seconds:parseFloat($('request-delay').value)||0,
    discovery_retries:parseInt($('discovery-retries').value,10)||0,
    llm_batch_size:parseInt($('llm-batch-size').value,10)||10,
    cache_ttl_hours:parseInt($('cache-ttl').value,10)||0,
    delete_after_download:$('delete-after-download').checked,
    ollama_think:$('ollama-think').checked,
    ollama_num_ctx:parseInt($('ollama-num-ctx').value,10)||2048,
    ollama_num_predict:parseInt($('ollama-num-predict').value,10)||200,
    ollama_keep_alive:$('ollama-keep-alive').value.trim()||'5m',
    webhook_url:$('webhook-url').value.trim(),
    auto_update_ytdlp:$('auto-update-ytdlp').checked,
  };
  const key=$('api-key').value.trim(); if(key) payload.api_key=key;
  try {
    await api('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    toast('Settings saved','success'); loadSettings();
  } catch(e) { toast('Error: '+e.message,'error'); }
};

$('test-connection').onclick=async()=>{
  const el=$('test-connection-result'); el.textContent='Testing…';
  try {
    const r=await api('/test-connection',{method:'POST'});
    if (r.ok) {
      let msg=`✓ Connected to ${r.url}`;
      if (r.provider==='ollama') {
        msg+=r.models?.length?' — models: '+r.models.join(', '):'  — no models found';
        if (!r.configured_model_available) msg+=` ⚠ "${r.configured_model}" not found — run: ollama pull ${r.configured_model}`;
      }
      el.textContent=msg;
    } else { el.textContent='✗ '+r.error; }
  } catch(e) { el.textContent='✗ '+e.message; }
};

$('start-on-boot').onchange=async e=>{
  const en=e.target.checked; e.target.disabled=true;
  try { await api('/boot-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})}); toast(en?'Start on boot enabled':'Disabled','success'); }
  catch(e) { e.target.checked=!en; toast(e.message,'error'); }
  finally { e.target.disabled=false; }
};

$('apply-low-resource-preset').onclick=()=>{
  $('ollama-think').checked=false; $('ollama-num-ctx').value=1024; $('ollama-num-predict').value=100;
  $('ollama-keep-alive').value='5m'; $('llm-batch-size').value=5; $('videos-per-scan').value=8;
  $('scan-interval').value=Math.max(parseInt($('scan-interval').value,10)||0,30);
  toast('Low-resource preset applied — click Save to apply','success');
};
$('update-ytdlp-now').onclick=async()=>{
  const btn=$('update-ytdlp-now'); btn.disabled=true; btn.textContent='Updating…';
  $('ytdlp-version-text').textContent='Running update…';
  try { const r=await api('/yt-dlp-update',{method:'POST'}); $('ytdlp-version-text').textContent=r.ok?`yt-dlp ${r.version||'updated'}`:'Update failed — check logs'; toast(r.ok?'yt-dlp updated':'Update failed',r.ok?'success':'warn'); }
  catch(e) { $('ytdlp-version-text').textContent='Error: '+e.message; }
  finally { btn.disabled=false; btn.textContent='Update yt-dlp'; }
};
$('run-health-check').onclick=async()=>{
  const btn=$('run-health-check'); btn.disabled=true; btn.textContent='Checking…';
  $('health-check-text').textContent='Probing TikTok via #fyp…';
  try { const r=await api('/scraper-health-check',{method:'POST'}); $('health-check-text').textContent=r.ok?'✓ Scraper health OK':'⚠ '+r.detail; loadStatus(); }
  catch(e) { $('health-check-text').textContent='Error: '+e.message; }
  finally { btn.disabled=false; btn.textContent='Health check'; }
};

/* ─── Init ──────────────────────────────────────────────────────── */
loadStatus();
loadStats();
loadSettings();
loadCategories();

setInterval(loadStatus, 30_000);
setInterval(() => {
  const active = document.querySelector('.tab-section.active')?.id;
  if (active==='tab-categories') loadCategories();
  if (active==='tab-dashboard') { loadStatus(); loadStats(); }
}, 60_000);
