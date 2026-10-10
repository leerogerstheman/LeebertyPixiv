'use strict';

/* LeebertyPixiv · 网页版前端
 *
 * 爬取是长任务：后端把 run_crawl 的 progress_cb 存进有界缓冲，这里轮询
 * /api/crawl/status。只在「正在跑」的时候轮询，停下来就不打扰服务器。
 */

const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  r18: 'hide',
  running: false,
  poll: 0,
  rows: [],
};

/* ------------------------------------------------------------ 小工具 -- */

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'err' ? ' err' : '');
  el.textContent = msg;
  $('toastHost').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 8000 : 4000);
}

async function api(path, body) {
  const opt = body === undefined
    ? undefined
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok || !data || data.ok === false) {
    throw new Error((data && data.error) || (res.status + ' ' + res.statusText));
  }
  return data;
}

const text = (id, v) => { const el = $(id); if (el) el.textContent = v; };
const val = (id) => ($(id) ? $(id).value : '');

function csv(id) {
  return val(id).split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
}

function num(id, dflt) {
  const n = parseInt(val(id), 10);
  return Number.isFinite(n) ? n : (dflt || 0);
}

/* ------------------------------------------------------------ 页签 -- */

function wireTabs() {
  for (const t of document.querySelectorAll('.tab')) {
    t.onclick = () => {
      for (const x of document.querySelectorAll('.tab')) x.classList.toggle('active', x === t);
      for (const p of document.querySelectorAll('.panel')) {
        p.classList.toggle('active', p.id === 'panel-' + t.dataset.tab);
      }
      if (t.dataset.tab === 'library') loadStats();
    };
  }
}

/* ------------------------------------------------------------ 元信息 -- */

function renderR18(meta) {
  const box = $('r18Chips');
  box.innerHTML = '';
  for (const m of meta.r18Modes) {
    const b = document.createElement('button');
    b.className = 'chip-btn' + (m.value === state.r18 ? ' on' : '');
    b.textContent = m.label;
    b.title = m.value;
    b.onclick = () => {
      state.r18 = m.value;
      for (const x of box.children) x.classList.toggle('on', x === b);
    };
    box.appendChild(b);
  }
  // 检索页的分级下拉用同一份选项
  const sel = $('sR18');
  sel.innerHTML = '';
  for (const m of meta.r18Modes) {
    const o = document.createElement('option');
    o.value = m.value;
    o.textContent = m.label;
    sel.appendChild(o);
  }
  sel.value = 'hide';
}

function renderConfig(cfg, meta) {
  const chip = $('credChip');
  if (cfg.hasCookie || cfg.hasToken) {
    chip.textContent = '已登录' + (cfg.hasCookie ? '（cookie）' : '') + (cfg.hasToken ? '（token）' : '');
    chip.className = 'chip ok';
  } else {
    chip.textContent = '未登录 · 只能看图库';
    chip.className = 'chip warn';
  }
  text('libChip', '图库 ' + (meta.stats.works || 0) + ' 作品');

  $('pagesInput').value = cfg.pages || 1;
  $('orderSel').value = cfg.order === 'popular_desc' ? 'popular_desc' : 'date';
  $('maxInput').value = cfg.maxWorksPerRun || 0;
  $('fromInput').value = cfg.dateFrom || '';
  $('toInput').value = cfg.dateTo || '';
  $('ugoiraSel').value = cfg.ugoiraFormat || 'none';
  $('likesInput').value = cfg.minLikes || 0;
  $('bmInput').value = cfg.minBookmarks || 0;
  $('mpwInput').value = cfg.maxPagesPerWork || 0;
  $('aiSel').value = 'all';

  $('sRps').value = cfg.requestsPerSecond ?? '';
  $('sConc').value = cfg.concurrency ?? '';
  $('sTimeout').value = cfg.timeout ?? '';
  $('sRetry').value = cfg.maxRetries ?? '';
  $('sDelay').value = cfg.searchDelay ?? '';
  $('sProxy').value = cfg.proxy || '';
  $('sPages').value = cfg.pages || 1;
  $('sOrder').value = cfg.order || '';
  $('sStype').value = cfg.searchType || '';
  $('sMax').value = cfg.maxWorksPerRun || 0;
  $('sLikes').value = cfg.minLikes || 0;
  $('sBm').value = cfg.minBookmarks || 0;
  $('sFfmpeg').value = cfg.ffmpegPath || '';
  $('sUgo').value = cfg.ugoiraFormat || '';
  $('sMpw').value = cfg.maxPagesPerWork || 0;

  const hist = $('history');
  if (meta.history && meta.history.length) {
    hist.textContent = meta.history.map((h) =>
      (h.time || '') + '  ' + (h.keyword || '') + '  新增 ' + (h.new ?? '-') + ' / 共 ' + (h.total ?? '-')
    ).join('\n');
  } else {
    hist.textContent = '（无）';
  }
}

/* ------------------------------------------------------------ 爬取 -- */

function crawlPayload() {
  const m = (state.meta.r18Modes || []).find((x) => x.value === state.r18);
  return {
    keywords: csv('kwInput'),
    scope: val('scopeSel'),
    artistIds: csv('aidInput'),
    pages: num('pagesInput', 1),
    order: val('orderSel'),
    maxWorks: num('maxInput', 0),
    dateFrom: val('fromInput'),
    dateTo: val('toInput'),
    ugoiraFormat: val('ugoiraSel'),
    minLikes: num('likesInput', 0),
    minBookmarks: num('bmInput', 0),
    maxPagesPerWork: num('mpwInput', 0),
    mode: state.r18,
    r18Levels: m ? m.levels : [],
    aiMode: val('aiSel'),
    deep: $('deepChk').checked,
    dryRun: $('dryChk').checked,
    force: $('forceChk').checked,
  };
}

async function doStart() {
  if (!csv('kwInput').length) { toast('请先填关键词', 'err'); return; }
  $('startBtn').disabled = true;
  try {
    const d = await api('/api/crawl/start', crawlPayload());
    state.running = true;
    $('stopBtn').disabled = false;
    $('log').textContent = '已开始：' + d.keywords.join(' / ');
    startPolling();
  } catch (err) {
    $('startBtn').disabled = false;
    toast('无法开始：' + err.message, 'err');
  }
}

async function doStop() {
  $('stopBtn').disabled = true;
  try {
    const d = await api('/api/crawl/stop', {});
    toast(d.stopped ? '已请求安全停止' : d.message);
  } catch (err) {
    toast('停止失败：' + err.message, 'err');
  }
}

function startPolling() {
  clearInterval(state.poll);
  state.poll = setInterval(pollStatus, 1000);
  pollStatus();
}

async function pollStatus() {
  let d;
  try { d = await api('/api/crawl/status'); } catch (e) { return; }

  const c = d.counters || {};
  text('cDl', c.downloaded || 0);
  text('cSkip', c.skipped || 0);
  text('cFail', c.failed || 0);
  text('cR18', (c.skipped_r18 || 0) + (c.skipped_r18g || 0));
  text('cUgo', c.skipped_ugoira || 0);
  text('cElapsed', (d.elapsed || 0) + 's');

  const pct = d.total > 0 ? Math.min(100, Math.round((d.done / d.total) * 100)) : 0;
  $('bar').style.width = pct + '%';
  text('stage', [d.stage, d.detail, d.total ? d.done + '/' + d.total : ''].filter(Boolean).join(' · ') || '进行中…');

  if (d.log && d.log.length) {
    const box = $('log');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.textContent = d.log.join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  if (!d.running && state.running) {
    state.running = false;
    clearInterval(state.poll);
    $('startBtn').disabled = false;
    $('stopBtn').disabled = true;
    if (d.error) toast('爬取出错：' + d.error, 'err');
    else toast('爬取结束');
    loadStats();
  }
}

/* ------------------------------------------------------------ 检索 -- */

function searchPayload() {
  return {
    text: val('sText'),
    tags: csv('sTags'),
    tagsMatchAll: $('sTagsAll').checked,
    author: val('sAuthor'),
    authorIds: csv('sAids'),
    query: val('sQuery'),
    dtags: csv('sDtags'),
    dtagsMatchAll: $('sDtagsAll').checked,
    dateFrom: val('sFrom'),
    dateTo: val('sTo'),
    minLikes: num('sLikes', 0),
    minBookmarks: num('sBm', 0),
    r18Mode: val('sR18'),
    aiMode: val('sAi'),
    sortBy: val('sortSel'),
  };
}

async function doSearch() {
  const btn = $('searchBtn');
  btn.disabled = true;
  try {
    const d = await api('/api/search', searchPayload());
    state.rows = d.rows;
    renderRows(d.rows);
    text('hits', '命中 ' + d.total + ' 条' + (d.shown < d.total ? '（表格显示前 ' + d.shown + ' 条）' : ''));
    text('tableNote', d.total === 0 ? '没有命中。图库可能是空的——先爬一批，或放宽条件。' : '');
  } catch (err) {
    toast('检索失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function renderRows(rows) {
  const tb = $('rows');
  tb.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8; td.className = 'empty'; td.textContent = '没有命中。';
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');

    const id = document.createElement('td');
    id.className = 'mono'; id.textContent = r.id + (r.page ? ' p' + r.page : '');

    const ttl = document.createElement('td');
    ttl.className = 'ttl'; ttl.textContent = r.title || '(无标题)';

    const au = document.createElement('td');
    au.textContent = r.author || '—';

    const dt = document.createElement('td');
    dt.className = 'mono'; dt.textContent = (r.date || '—').slice(0, 10);

    const xr = document.createElement('td');
    if (r.xr) {
      const b = document.createElement('span');
      b.className = 'badge r18'; b.textContent = r.xr === 2 ? 'R-18G' : 'R-18';
      xr.appendChild(b);
    } else if (r.ai) {
      const b = document.createElement('span');
      b.className = 'badge ai'; b.textContent = 'AI';
      xr.appendChild(b);
    } else { xr.textContent = '—'; }

    const bm = document.createElement('td');
    bm.className = 'mono'; bm.textContent = r.bookmarks || 0;

    const tg = document.createElement('td');
    for (const t of (r.tags || []).slice(0, 4)) {
      const s = document.createElement('span');
      s.className = 'tag'; s.textContent = t;
      tg.appendChild(s);
    }

    const act = document.createElement('td');
    const open = document.createElement('button');
    open.className = 'btn btn-text'; open.textContent = '详情';
    open.onclick = () => openRecord(r.key);
    act.appendChild(open);

    tr.append(id, ttl, au, dt, xr, bm, tg, act);
    tb.appendChild(tr);
  }
}

async function openRecord(key) {
  try {
    const d = await api('/api/record?key=' + encodeURIComponent(key));
    text('dlgTitle', (d.record.title || '作品详情') + '  #' + (d.record.id || ''));
    $('dlgBody').textContent = JSON.stringify(d.record, null, 2);
    $('dialogHost').hidden = false;
  } catch (err) {
    toast('取详情失败：' + err.message, 'err');
  }
}

function clearFilters() {
  for (const id of ['sText', 'sTags', 'sAuthor', 'sAids', 'sQuery', 'sDtags', 'sFrom', 'sTo', 'sLikes', 'sBm']) {
    $(id).value = '';
  }
  $('sR18').value = 'hide';
  $('sAi').value = 'all';
  $('sortSel').value = 'default';
  $('sTagsAll').checked = true;
  $('sDtagsAll').checked = true;
}

function exportHits() {
  toast('导出走「图库」页的按钮（导出全部入库记录）');
}

/* ------------------------------------------------------------ 图库 -- */

function renderStats(s) {
  const g = $('statsGrid');
  const items = [
    [s.works || 0, '作品数'],
    [s.images || 0, '图片数'],
    [s.authors || 0, '画师数'],
    [s.tags || 0, '标签数'],
    [s.bytesText || '0 B', '原图占用'],
    [s.diskFreeText || '—', '磁盘剩余'],
  ];
  g.innerHTML = '';
  for (const [v, label] of items) {
    const d = document.createElement('div');
    const b = document.createElement('b'); b.textContent = v;
    const sp = document.createElement('span'); sp.textContent = label;
    d.append(b, sp); g.appendChild(d);
  }
  const top = $('topTags');
  top.textContent = (s.topTags && s.topTags.length)
    ? 'Top 标签：\n' + s.topTags.slice(0, 15).map((t) => String(t.count).padStart(6) + '  ' + t.value).join('\n')
    : '（图库为空）';
}

async function loadStats() {
  try {
    const d = await api('/api/library/stats');
    renderStats(d.stats);
    text('libChip', '图库 ' + (d.stats.works || 0) + ' 作品');
  } catch (err) {
    $('topTags').textContent = '读取失败：' + err.message;
  }
}

async function doReindex() {
  const btn = $('reindexBtn');
  btn.disabled = true;
  text('reindexNote', '正在重建…');
  try {
    const d = await api('/api/library/reindex', {});
    text('reindexNote', '完成：' + (d.result.records ?? 0) + ' 条');
    toast('索引已重建');
    loadStats();
  } catch (err) {
    text('reindexNote', '失败：' + err.message);
    toast('重建失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function doExport(fmt) {
  text('exportNote', '正在导出 ' + fmt + '…');
  try {
    const d = await api('/api/export', { format: fmt });
    text('exportNote', '已导出 ' + d.name + '（' + Math.round(d.size / 1024) + ' KB）\n' + d.path);
    toast('已导出 ' + d.name);
  } catch (err) {
    text('exportNote', '失败：' + err.message);
    toast('导出失败：' + err.message, 'err');
  }
}

/* ------------------------------------------------------------ 设置 -- */

async function saveConfig() {
  const payload = {
    requestsPerSecond: val('sRps'),
    concurrency: val('sConc'),
    timeout: val('sTimeout'),
    maxRetries: val('sRetry'),
    searchDelay: val('sDelay'),
    proxy: val('sProxy'),
    pages: val('sPages'),
    order: val('sOrder'),
    searchType: val('sStype'),
    maxWorksPerRun: val('sMax'),
    minLikes: val('sLikes'),
    minBookmarks: val('sBm'),
    ffmpegPath: val('sFfmpeg'),
    ugoiraFormat: val('sUgo'),
    maxPagesPerWork: val('sMpw'),
  };
  try {
    const d = await api('/api/config', payload);
    text('saveNote', '已保存 ' + Object.keys(d.changed).length + ' 项');
    toast('设置已保存');
    await boot(true);
  } catch (err) {
    text('saveNote', '失败：' + err.message);
    toast('保存失败：' + err.message, 'err');
  }
}

/* ------------------------------------------------------------ 启动 -- */

function wire() {
  $('startBtn').onclick = doStart;
  $('stopBtn').onclick = doStop;
  $('searchBtn').onclick = doSearch;
  $('clearBtn').onclick = clearFilters;
  $('exportHitsBtn').onclick = exportHits;
  $('reindexBtn').onclick = doReindex;
  $('statsBtn').onclick = loadStats;
  $('saveBtn').onclick = saveConfig;
  for (const b of document.querySelectorAll('[data-fmt]')) {
    b.onclick = () => doExport(b.dataset.fmt);
  }

  $('dlgClose').onclick = () => { $('dialogHost').hidden = true; };
  $('dialogHost').onclick = (e) => { if (e.target === $('dialogHost')) $('dialogHost').hidden = true; };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('dialogHost').hidden = true; });
}

async function boot(quiet) {
  try {
    state.meta = await api('/api/meta');
  } catch (err) {
    toast('无法连接后端：' + err.message, 'err');
    return;
  }
  renderR18(state.meta);
  renderConfig(state.meta.config, state.meta);
  renderStats(state.meta.stats);
  if (!quiet) {
    $('kwInput').value = '';
    $('aidInput').value = '';
  }
}

wireTabs();
wire();
boot();
