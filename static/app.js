/* ============================================================
   IndexTTS2 长文本批量合成 — 前端逻辑
   ============================================================ */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const EMO_LABELS = ['快乐', '愤怒', '悲伤', '恐惧', '厌恶', '低落', '惊讶', '平静'];
const STATUS_TEXT = {
  queued: '排队中', running: '合成中', merging: '合并中',
  done: '已完成', error: '失败', partial: '部分完成', canceled: '已取消',
  pending: '等待中', error_chunk: '失败',
};

const state = {
  files: [],
  voices: [],
  defaults: {},
  jobs: new Map(),      // id -> 快照
  sources: new Map(),   // id -> EventSource
  nodes: new Map(),     // key -> DOM 引用
  pollTimer: null,
  activeJob: null,
  loading: false,
};

/* ---------------- 工具 ---------------- */
function toast(msg, kind = 'info', ms = 3200) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), ms);
}

function num(id, def) {
  const el = $('#' + id);
  const v = parseFloat(el.value);
  return Number.isFinite(v) ? v : def;
}

function fmtDur(sec) {
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(Math.floor(s)).padStart(2, '0')}`
    : `${m}:${String(Math.floor(s)).padStart(2, '0')}`;
}

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

/* ---------------- 初始化 ---------------- */
async function init() {
  buildEmoGrid();
  bindEvents();
  initFormMemory();
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    state.voices = d.voices || [];
    state.defaults = d.defaults || {};
    renderVoices();
    restoreForm();
    updateEngineBadge(d.engine || {});
  } catch (e) {
    toast('无法连接服务端：' + e.message, 'err');
  }
  refreshJobs();
  state.pollTimer = setInterval(refreshJobs, 2500);
  setInterval(refreshEngine, 4000);
}

function buildEmoGrid() {
  const g = $('#emoGrid');
  g.innerHTML = '';
  for (let i = 0; i < 8; i++) {
    const item = document.createElement('div');
    item.className = 'emo-item';
    item.innerHTML = `<span>${EMO_LABELS[i]} <b id="emoVal${i}">0.00</b></span>
      <input type="range" id="emoVec${i}" min="-1" max="1" step="0.01" value="0">`;
    g.appendChild(item);
  }
  for (let i = 0; i < 8; i++) {
    const s = $('#emoVec' + i);
    s.addEventListener('input', () => { $('#emoVal' + i).textContent = parseFloat(s.value).toFixed(2); });
  }
}

function renderVoices() {
  const sel = $('#voice');
  sel.innerHTML = '';
  const groups = {};
  for (const v of state.voices) {
    (groups[v.group || '其它'] = groups[v.group || '其它'] || []).push(v);
  }
  for (const [gname, list] of Object.entries(groups)) {
    const og = document.createElement('optgroup');
    og.label = gname;
    for (const v of list) {
      const o = document.createElement('option');
      o.value = v.id;
      o.textContent = v.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  const def = state.defaults.voice;
  if (def) sel.value = def;
}

async function refreshEngine() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    updateEngineBadge(d.engine || {});
  } catch (e) { /* 静默 */ }
}

function updateEngineBadge(eng) {
  const badge = $('#engineBadge');
  const text = $('#engineText');
  if (eng.load_error) {
    badge.className = 'badge err';
    text.textContent = '引擎异常';
    badge.title = eng.load_error;
  } else if (eng.loaded) {
    badge.className = 'badge ok';
    const v = eng.vram_used_mb ? ` · 显存 ${eng.vram_used_mb}/${eng.vram_total_mb}MB` : '';
    text.textContent = `模型就绪${v}`;
  } else if (eng.loading) {
    badge.className = 'badge busy';
    text.textContent = '模型加载中…';
  } else {
    badge.className = 'badge idle';
    text.textContent = eng.device === 'cuda' ? '待命（首次合成时加载）' : '待命 · CPU 模式';
  }
}

/* ---------------- 文件选择 ---------------- */
function bindEvents() {
  const dz = $('#dropzone');
  const input = $('#fileInput');
  dz.addEventListener('click', () => input.click());
  $('#pickFiles').addEventListener('click', (e) => { e.stopPropagation(); input.click(); });
  input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });

  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove('drag');
  }));
  dz.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));

  $('#emoMode').addEventListener('change', syncEmoUI);
  $('#emoWeight').addEventListener('input', (e) => $('#emoWeightVal').textContent = (+e.target.value).toFixed(2));
  $('#emoWeight2').addEventListener('input', (e) => $('#emoWeightVal2').textContent = (+e.target.value).toFixed(2));
  $('#uploadVoice').addEventListener('click', () => $('#voiceInput').click());
  $('#voiceInput').addEventListener('change', uploadVoice);
  $('#startBtn').addEventListener('click', startJob);
  $('#cancelBtn').addEventListener('click', cancelActive);
  $('#previewBtn').addEventListener('click', previewSplit);
  $('#clearTempBtn').addEventListener('click', clearTemp);
  $('#clearFinishedBtn').addEventListener('click', clearFinished);
  syncEmoUI();
}

function addFiles(list) {
  for (const f of list) {
    const name = f.name || '';
    const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
    if (ext && !['.txt', '.text', '.md', '.markdown'].includes(ext)) {
      toast(`已跳过非文本文件：${name}`, 'err', 2600);
      continue;
    }
    if (state.files.some((x) => x.name === f.name && x.size === f.size)) continue;
    state.files.push(f);
  }
  renderFiles();
}

function renderFiles() {
  const ul = $('#fileList');
  ul.innerHTML = '';
  for (let i = 0; i < state.files.length; i++) {
    const f = state.files[i];
    const li = document.createElement('li');
    li.className = 'file-item';
    li.innerHTML = `<span class="fname" title="${f.name}">${f.name}</span>
      <span class="fmeta">${humanSize(f.size)}</span>
      <button class="rm" title="移除">×</button>`;
    li.querySelector('.rm').addEventListener('click', () => {
      state.files.splice(i, 1);
      renderFiles();
    });
    ul.appendChild(li);
  }
  if (!state.files.length) ul.innerHTML = '';
}

function syncEmoUI() {
  const m = $('#emoMode').value;
  $('#emoRefWrap').classList.toggle('hidden', m !== '1');
  $('#emoVecWrap').classList.toggle('hidden', m !== '2');
  $('#emoTextWrap').classList.toggle('hidden', m !== '3');
}

/* ---------------- 参数收集 ---------------- */
function collectParams() {
  const vec = [];
  for (let i = 0; i < 8; i++) vec.push(+$('#emoVec' + i).value);
  const mode = +$('#emoMode').value;
  const w = mode === '2' ? +$('#emoWeight2').value : +$('#emoWeight').value;
  return {
    voice: $('#voice').value,
    emo_mode: mode,
    emo_weight: w,
    emo_vector: vec,
    emo_text: $('#emoText').value || '',
    use_random: $('#emoRandom').checked,
    chunk_chars: Math.round(num('chunkChars', 500)),
    hard_max_chars: Math.round(num('hardMaxChars', 700)),
    gap_ms: Math.round(num('gapMs', 200)),
    output_format: $('#outputFormat').value,
    export_dir: $('#exportDir').value.trim(),
    temperature: num('temperature', 0.8),
    top_p: num('topP', 0.8),
    top_k: Math.round(num('topK', 30)),
    num_beams: Math.round(num('numBeams', 3)),
    repetition_penalty: num('repPenalty', 10),
    max_text_tokens_per_segment: Math.round(num('maxTokensSeg', 120)),
    do_sample: $('#doSample').checked,
  };
}

/* ---------------- 表单记忆 ----------------
   「另存到目录」这类字段填一次就该一直有效。之前完全没有持久化，
   页面一刷新就清空，于是文件不会自动存到指定目录 —— 而界面上看不出任何异常。
   这里把参数存在 localStorage，下次打开自动恢复；恢复不了时回落到服务端
   config.json 预设的值（DEFAULT_PARAMS）。
*/
const FORM_STORE_KEY = 'ttsbatch.form.v1';

// 参数名 -> 控件 id。collectParams() 的反查表，用来把存的值写回界面。
const PARAM_TO_EL = {
  voice: 'voice',
  emo_mode: 'emoMode',
  emo_weight: 'emoWeight',
  emo_text: 'emoText',
  use_random: 'emoRandom',
  chunk_chars: 'chunkChars',
  hard_max_chars: 'hardMaxChars',
  gap_ms: 'gapMs',
  output_format: 'outputFormat',
  export_dir: 'exportDir',
  temperature: 'temperature',
  top_p: 'topP',
  top_k: 'topK',
  num_beams: 'numBeams',
  repetition_penalty: 'repPenalty',
  max_text_tokens_per_segment: 'maxTokensSeg',
  do_sample: 'doSample',
};

function loadFormStore() {
  try {
    const raw = localStorage.getItem(FORM_STORE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    return {};
  }
}

function saveFormStore() {
  // 委托监听会捕获全文档的 change/blur，collectParams() 读不到某个控件
  // 时会抛错，所以这里必须兜住，不能让异常从事件回调里冒出来。
  try {
    localStorage.setItem(FORM_STORE_KEY, JSON.stringify(collectParams()));
  } catch (e) { /* 隐私模式/配额满/控件缺失，忽略 */ }
}

// 控件类型决定写回方式：checkbox 用 checked，其余用 value
function setEl(el, val) {
  if (!el || val === undefined || val === null) return;
  if (el.type === 'checkbox') el.checked = !!val;
  else el.value = val;
}

function restoreForm() {
  const saved = loadFormStore();
  for (const [k, id] of Object.entries(PARAM_TO_EL)) {
    // localStorage 优先（用户最近一次的选择），其次服务端 config.json 预设
    const v = saved[k] !== undefined ? saved[k] : state.defaults[k];
    if (v === undefined || v === null) continue;
    setEl($('#' + id), v);
  }
  if (Array.isArray(saved.emo_vector)) {
    saved.emo_vector.forEach((v, i) => {
      const el = $('#emoVec' + i);
      if (el) el.value = v;
    });
  }
  syncEmoUI();
  // 这两个数值框旁边有实时标签，程序化赋值不会触发 input 事件，手动刷一下
  const w1 = $('#emoWeightVal'), w2 = $('#emoWeightVal2');
  if (w1) w1.textContent = (+$('#emoWeight').value).toFixed(2);
  if (w2) w2.textContent = (+$('#emoWeight2').value).toFixed(2);
}

function initFormMemory() {
  // 输入即存，不必等到点提交
  const save = () => saveFormStore();
  document.addEventListener('change', save, true);
  document.addEventListener('blur', save, true);
}

/* ---------------- 上传音色 ---------------- */
async function uploadVoice(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/api/voices', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '上传失败');
    const s = await fetch('/api/state').then((x) => x.json());
    state.voices = s.voices || [];
    renderVoices();
    $('#voice').value = d.id;
    toast(`音色「${d.name}」已添加`, 'ok');
  } catch (e) {
    toast('上传失败：' + e.message, 'err');
  }
  ev.target.value = '';
}

/* ---------------- 预览分块 ---------------- */
async function previewSplit() {
  if (!state.files.length) return toast('请先投入至少一个文本文件', 'err');
  const f = state.files[0];
  const btn = $('#previewBtn');
  btn.disabled = true;
  btn.textContent = '读取中…';
  try {
    const text = await f.text();
    const r = await fetch('/api/split-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        chunk_chars: Math.round(num('chunkChars', 500)),
        hard_max_chars: Math.round(num('hardMaxChars', 700)),
      }),
    });
    const d = await r.json();
    const box = $('#previewBox');
    box.classList.remove('hidden');
    box.innerHTML = `<div class="pv-head">《${f.name}》共 ${d.total_chars} 字 → 切为
      <b style="color:var(--accent)">${d.chunk_count}</b> 块（上限 ${d.hard_max_chars} 字）</div>`
      + d.chunks.map((c) => `<div class="pv-item">
          <span class="pv-idx">#${c.i}</span>
          <span class="pv-text" title="${escapeHtml(c.preview)}">${escapeHtml(c.preview)}</span>
          <span class="pv-n">${c.chars}字</span></div>`).join('');
    toast(`将切分为 ${d.chunk_count} 块`, 'ok');
  } catch (e) {
    toast('预览失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '预览分块效果';
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------------- 启动任务 ---------------- */
async function startJob() {
  if (!state.files.length) return toast('请先投入至少一个文本文件', 'err');
  if (state.loading) return toast('上一批任务还在进行中', 'err');

  const emoFile = $('#emoAudio').files[0];
  const fd = new FormData();
  for (const f of state.files) fd.append('files', f);
  fd.append('params', JSON.stringify(collectParams()));
  if (emoFile) fd.append('emo_audio', emoFile);

  const btn = $('#startBtn');
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const r = await fetch('/api/jobs', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '提交失败');
    state.activeJob = d.id;
    state.jobs.set(d.id, d);
    openStream(d.id);
    render();
    $('#cancelBtn').classList.remove('hidden');
    const total = d.tasks.reduce((a, t) => a + t.chunks_total, 0);
    toast(`已提交 ${d.tasks.length} 个文件，共 ${total} 个分块`, 'ok');
  } catch (e) {
    toast('提交失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '开始批量合成';
  }
}

async function cancelActive() {
  if (!state.activeJob) return;
  const id = state.activeJob;
  await fetch('/api/jobs/' + id, { method: 'DELETE' });
  // 取消会通过推理进度钩子立刻打断当前这一块，不必等它跑完
  const cur = state.jobs.get(id);
  if (cur) { cur.status = 'canceled'; render(); }
  updateQueueInfo();
  toast('已取消，正在打断当前合成', 'info');
}

/* ---------------- 任务拉取与推送 ---------------- */
async function refreshJobs() {
  try {
    const r = await fetch('/api/jobs');
    const d = await r.json();
    const list = d.jobs || [];
    const seen = new Set(list.map((s) => s.id));
    // 服务端才是唯一事实来源：它已经移除的（被清理、或超过 MAX_JOBS_KEPT 淘汰）
    // 前端也要跟着删，否则列表只增不减。
    for (const id of Array.from(state.jobs.keys())) {
      if (seen.has(id)) continue;
      state.jobs.delete(id);
      const es = state.sources.get(id);
      if (es) { es.close(); state.sources.delete(id); }
      if (state.activeJob === id) state.activeJob = null;
    }
    for (const s of list) {
      const cur = state.jobs.get(s.id);
      if (!cur || cur.status !== s.status || s.status === 'running' || s.status === 'queued') {
        const full = await fetch('/api/jobs/' + s.id).then((x) => x.json());
        state.jobs.set(s.id, full);
      }
    }
    render();
    updateQueueInfo();
  } catch (e) { /* 静默 */ }
}

function openStream(id) {
  if (state.sources.has(id)) return;
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (ev) => {
    try {
      state.jobs.set(id, JSON.parse(ev.data));
      render();
      updateQueueInfo();
    } catch (e) { /* 忽略 */ }
  };
  es.onerror = () => { es.close(); state.sources.delete(id); };
  state.sources.set(id, es);
}

const DONE_STATUSES = ['done', 'error', 'partial', 'canceled'];

function updateQueueInfo() {
  const jobs = Array.from(state.jobs.values());
  const running = jobs.filter((j) => j.status === 'running' || j.status === 'queued');
  const finished = jobs.filter((j) => DONE_STATUSES.includes(j.status)).length;
  state.loading = running.length > 0;
  $('#queueInfo').textContent = running.length
    ? `${running.length} 个任务进行中`
    : (jobs.length ? `共 ${jobs.length} 条记录` : '');
  const clearBtn = $('#clearFinishedBtn');
  if (clearBtn) {
    clearBtn.classList.toggle('hidden', finished === 0);
    clearBtn.textContent = `清理已完成 (${finished})`;
    clearBtn.disabled = finished === 0;
  }
  const btn = $('#startBtn');
  if (!state.loading) {
    btn.disabled = false;
    if (!$('#cancelBtn').classList.contains('hidden')) {
      $('#cancelBtn').classList.add('hidden');
      state.activeJob = null;
    }
  }
}

/* ---------------- 渲染 ---------------- */
function render() {
  const list = $('#taskList');
  const jobs = Array.from(state.jobs.values())
    .sort((a, b) => b.created - a.created)
    .slice(0, 8);

  if (!jobs.length) {
    list.innerHTML = `<div class="empty"><div class="empty-icon">♪</div>
      <p>还没有任务</p><p class="hint">在左侧投入文本文件后点击「开始批量合成」</p></div>`;
    state.nodes.clear();
    return;
  }

  // 移除已不存在的任务节点
  for (const [key, ref] of state.nodes) {
    if (!ref.el.isConnected) state.nodes.delete(key);
  }

  // 逐个任务渲染：已在页面中的节点原地更新，新任务追加，避免闪烁
  const existing = new Map();
  for (const el of Array.from(list.children)) {
    if (el.dataset && el.dataset.job) existing.set(el.dataset.job, el);
  }
  for (const job of jobs) {
    try {
      const el = existing.get(job.id);
      const newEl = renderJob(job);
      if (el && el.isConnected) {
        if (newEl !== el) list.replaceChild(newEl, el);
      } else {
        list.appendChild(newEl);
      }
    } catch (e) {
      // 单个任务渲染失败不能拖垮整个列表
      console.error('renderJob failed', job && job.id, e);
    }
  }
  for (const [id, el] of existing) {
    if (!jobs.some((j) => j.id === id) && el.isConnected) {
      for (const [k, ref] of state.nodes) {
        if (ref.group === id) state.nodes.delete(k);
      }
      el.remove();
    }
  }
  list.querySelector('.empty')?.remove();
}

function renderJob(job) {
  // /api/jobs 返回的是摘要（没有 tasks），只有 /api/jobs/{id} 才有完整结构。
  // 万一某次拿到的是摘要或请求 404，这里要挡住，不能让整个列表渲染抛异常
  // —— 那会导致后面所有任务都渲染不出来，看起来就像「任务突然消失」。
  if (!Array.isArray(job.tasks)) job = { ...job, tasks: [] };
  const key = 'job:' + job.id;
  let g = state.nodes.get(key);
  if (!g || !g.el.isConnected) {
    const el = document.createElement('div');
    el.className = 'job-group';
    el.dataset.job = job.id;
    el.innerHTML = `
      <div class="job-head">
        <span class="status-pill js-status"></span>
        <span class="jt js-title"></span>
        <span class="task-meta js-prog"></span>
        <button class="op-btn js-cancel" style="margin-left:auto">取消</button>
      </div>
      <div class="js-tasks"></div>`;
    g = { el, group: job.id, status: el.querySelector('.js-status'),
          title: el.querySelector('.js-title'), prog: el.querySelector('.js-prog'),
          tasks: el.querySelector('.js-tasks'), cancel: el.querySelector('.js-cancel') };
    g.cancel.addEventListener('click', async () => {
      await fetch('/api/jobs/' + job.id, { method: 'DELETE' });
      toast('已请求取消', 'info');
    });
    state.nodes.set(key, g);
  }

  g.status.className = 'status-pill js-status ' + job.status;
  g.status.textContent = STATUS_TEXT[job.status] || job.status;
  const done = job.tasks.filter((t) => t.status === 'done').length;
  g.title.textContent = `任务 ${job.id} · ${done}/${job.tasks.length} 个文件`;
  g.prog.textContent = Math.round((job.progress || 0) * 100) + '%'
    + (job.resume_count ? ` · 续跑 ${job.resume_count} 次` : '');
  g.cancel.style.display = ['done', 'error', 'partial', 'canceled'].includes(job.status) ? 'none' : '';

  for (const task of job.tasks) g.tasks.appendChild(renderTask(job, task));
  // 移除已消失的 task。
  // 注意：这里的 key 必须和上面 renderTask 里用的、以及 tkeys 的构造完全一致
  // （都是 'task:' + job.id + ':' + task.id）。早先这里只用了 el.dataset.task，
  // 少了 job.id 一段，永远匹配不上，于是每 2.5 秒渲染一次就把所有文件行
  // 删掉重建 —— 表现为「文件行闪一下就没了」。
  const tkeys = new Set(job.tasks.map((t) => 'task:' + job.id + ':' + t.id));
  for (const el of Array.from(g.tasks.children)) {
    const tk = 'task:' + job.id + ':' + el.dataset.task;
    if (el.dataset.task && !tkeys.has(tk) && el.isConnected) {
      state.nodes.delete(tk);
      el.remove();
    }
  }
  return g.el;
}

function renderTask(job, task) {
  const key = 'task:' + job.id + ':' + task.id;
  let t = state.nodes.get(key);
  if (!t || !t.el.isConnected) {
    const el = document.createElement('div');
    el.className = 'task-item';
    el.dataset.task = task.id;
    el.innerHTML = `
      <div class="task-top">
        <span class="task-name"></span>
        <span class="status-pill js-st"></span>
        <span class="task-meta js-meta"></span>
        <span class="task-ops">
          <button class="op-btn js-detail">分块</button>
          <a class="op-btn primary js-play">播放</a>
          <a class="op-btn js-dl">下载</a>
        </span>
      </div>
      <div class="bar"><i></i></div>
      <div class="chunks js-chunks"></div>
      <div class="err-msg js-err hidden"></div>
      <div class="chunk-list js-clist"></div>`;
    t = { el, st: el.querySelector('.js-st'), meta: el.querySelector('.js-meta'),
          bar: el.querySelector('.bar'), fill: el.querySelector('.bar > i'),
          chunks: el.querySelector('.js-chunks'), err: el.querySelector('.js-err'),
          clist: el.querySelector('.js-clist'),
          name: el.querySelector('.task-name'),
          play: el.querySelector('.js-play'), dl: el.querySelector('.js-dl'),
          detail: el.querySelector('.js-detail'), chunkEls: [] };
    el.querySelector('.js-detail').addEventListener('click', () => {
      t.clist.classList.toggle('open');
    });
    t.play.addEventListener('click', () => playTask(job, task));
    t.dl.addEventListener('click', (e) => { e.currentTarget.href = task.output_url + '?download=1'; });
    state.nodes.set(key, t);
  }

  t.name.textContent = task.name;
  t.name.title = task.raw_name || task.name;
  t.st.className = 'status-pill js-st ' + task.status;
  t.st.textContent = STATUS_TEXT[task.status] || task.status;

  const meta = [];
  meta.push(`${task.chunks_done}/${task.chunks_total} 块`);
  meta.push(`${(task.text_chars || 0).toLocaleString()} 字`);
  if (task.seconds) meta.push(`时长 ${fmtDur(task.seconds)}`);
  if (task.elapsed) meta.push(`用时 ${fmtDur(task.elapsed)}`);
  if (task.exported) meta.push('已另存');
  t.meta.textContent = meta.join(' · ');
  if (task.exported) t.meta.title = '已另存到 ' + task.exported;

  // 另存失败要看得见。以前只往服务端日志写一行，界面照样显示「完成」，
  // 用户等了半天还以为存好了。
  if (task.export_error) {
    t.meta.textContent += ' · ⚠ 另存失败：' + task.export_error;
    t.meta.classList.add('warn');
  } else {
    t.meta.classList.remove('warn');
  }

  const pct = Math.round((task.progress || 0) * 100);
  t.fill.style.width = pct + '%';
  t.bar.className = 'bar' + (task.status === 'done' ? ' done' : task.status === 'error' ? ' err' : '');

  const ready = !!task.output_url;
  t.play.style.display = ready ? '' : 'none';
  t.dl.style.display = ready ? '' : 'none';
  t.play.href = task.output_url || '#';
  t.dl.href = task.output_url ? task.output_url + '?download=1' : '#';

  if (task.error) {
    t.err.classList.remove('hidden');
    t.err.textContent = task.error;
  } else t.err.classList.add('hidden');

  // 分块格子
  const chunks = task.chunks || [];
  if (t.chunkEls.length !== chunks.length) {
    t.chunks.innerHTML = '';
    t.chunkEls = chunks.map((c) => {
      const d = document.createElement('div');
      d.className = 'chunk';
      t.chunks.appendChild(d);
      return d;
    });
  }
  chunks.forEach((c, i) => {
    const el = t.chunkEls[i];
    if (!el) return;
    let cls = 'chunk';
    if (c.status === 'done') cls += c.cached ? ' cached' : ' done';
    else if (c.status === 'error') cls += ' error';
    else if (c.status === 'running') cls += ' running';
    const tip = `#${c.i} · ${c.chars}字` + (c.seconds ? ` · ${c.seconds}s` : '')
      + (c.cached ? ' · 缓存命中' : '') + (c.cost ? ` · 耗时${c.cost}s` : '')
      + (c.note ? ` · ${c.note}` : '') + (c.error ? ` · ${c.error}` : '');
    if (el.className !== cls) el.className = cls;
    el.title = tip;
    const playable = c.status === 'done' && !el._bound;
    if (playable) {
      el._bound = true;
      el.classList.add('playable');
      el.addEventListener('click', () => {
        const a = $('#player');
        a.src = `/api/chunks/${job.id}/${task.id}/${c.i}`;
        a.play().catch(() => {});
        toast(`播放第 ${c.i} 块（${c.chars}字）`, 'info', 1800);
      });
    }
  });

  // 分块明细
  if (t.clist.classList.contains('open')) {
    t.clist.innerHTML = chunks.map((c) => `<div class="chunk-row">
      <span class="ci">#${c.i}</span>
      <span class="cc">${c.chars}字</span>
      <span class="cx" title="${escapeHtml(c.text || '')}">${escapeHtml(c.text || '')}</span>
      <span class="cs">${c.seconds ? c.seconds + 's' : (c.status === 'done' ? '' : STATUS_TEXT[c.status] || '')}</span>
    </div>`).join('');
  }

  return t.el;
}

function playTask(job, task) {
  if (!task.output_url) return;
  const a = $('#player');
  a.src = task.output_url;
  a.play().catch(() => toast('播放失败，请检查浏览器自动播放限制', 'err'));
}

/* ---------------- 清理 ---------------- */
async function clearFinished() {
  try {
    const r = await fetch('/api/jobs/clear-finished', { method: 'POST' });
    const d = await r.json();
    for (const id of d.job_ids || []) {
      state.jobs.delete(id);
      const es = state.sources.get(id);
      if (es) { es.close(); state.sources.delete(id); }
      if (state.activeJob === id) state.activeJob = null;
    }
    render();
    updateQueueInfo();
    toast(d.removed ? `已从列表移除 ${d.removed} 条已完成任务` : '没有已完成的任务', 'ok');
  } catch (e) {
    toast('清理失败：' + e.message, 'err');
  }
}

async function clearTemp() {
  if (!confirm('确定要删除所有分块临时缓存吗？\n已完成的合并音频不受影响。')) return;
  try {
    const r = await fetch('/api/temp/clear', { method: 'POST' });
    const d = await r.json();
    toast(`已清理 ${d.removed_dirs} 个目录，释放 ${d.freed_mb} MB`, 'ok');
  } catch (e) {
    toast('清理失败：' + e.message, 'err');
  }
}

document.addEventListener('DOMContentLoaded', init);
