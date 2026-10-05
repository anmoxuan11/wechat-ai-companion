// ---------------------------------------------------------------------------
// 3.0 扩展面板脚本：洞察 / 记忆库 / 调教
//
// 为什么要单独一个文件：dashboard.html + app.js 已经是 2.0 沉淀下来的稳定组合，
// 3.0 新功能全塞进去会让那个文件彻底改不动。这里用独立模块挂载，
// 通过 <script src="/assets/extra.js"> 加载，全部包在 IIFE 里避免污染。
//
// 轮询策略：页面不可见不刷；不在对应标签页不刷。别为了好看把 localhost 打满。
// ---------------------------------------------------------------------------
(function () {
  'use strict';

  const TOKEN = (document.querySelector('meta[name="panel-token"]') || {}).content || '';
  const $ = id => document.getElementById(id);

  function toast(msg) {
    const t = $('toast');
    if (!t) { return; }
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2200);
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function api(path, method, body) {
    try {
      const opt = { method: method || 'GET', headers: { 'Content-Type': 'application/json' } };
      if (TOKEN) { opt.headers['X-Panel-Token'] = TOKEN; }
      if (body) { opt.body = JSON.stringify(body); }
      const r = await fetch(path, opt);
      const j = await r.json().catch(() => ({}));
      if (r.status === 403) {
        toast(j.msg || '请求被拒绝（令牌无效），请刷新页面');
        return { ok: false };
      }
      return j;
    } catch (e) {
      return { ok: false, msg: '请求失败: ' + e.message };
    }
  }

  const activePage = () => {
    const el = document.querySelector('.nav-tab.active');
    return el ? el.dataset.page : '';
  };
  const pageVisible = () => document.visibilityState === 'visible';

  // =========================================================================
  // 洞察：概览 + 健康度 + 成本 + 事件线
  // =========================================================================
  const KIND_LABEL = {
    bot_start: '启动', bot_stop: '停止', reply: '回复', skip: '跳过',
    ai_error: 'AI故障', wechat_lost: '掉线', wechat_back: '重连',
    takeover: '接管', memory_summary: '记忆', schedule_run: '定时',
    guard_block: '护栏', backup: '备份', feedback: '反馈',
    config_change: '配置', error: '错误', test: '测试',
    mood: '情绪', reminder: '提醒',
  };

  async function loadInsights() {
    const r = await api('/api/insights');
    if (!r || !r.ok) { return; }
    $('iReply').textContent = r.replies_today || 0;
    $('iCost').textContent = r.cost_today || '—';
    $('iBlocked').textContent = r.blocked_today || 0;
    $('iErr').textContent = r.errors_today || 0;
    $('iMsgs').textContent = r.msgs_stored || 0;
    $('iDays').textContent = r.days || 0;

    const h = await api('/api/health');
    if (h && h.ok) {
      const badge = $('healthScore');
      badge.textContent = h.score + ' 分';
      badge.className = 'badge ' + (h.level === 'good' ? 'b-ok' : (h.level === 'warn' ? 'b-warn' : 'b-stop'));
      const extra = (h.restart_required || []).length
        ? '<div class="prop p-hot"><div class="p-content">改了「'
          + esc(h.restart_required.join('、')) + '」，需要重启机器人才生效'
          + '（其余配置都已经自动热更新了）</div></div>' : '';
      $('healthReasons').innerHTML = (h.reasons || []).map(x =>
        '<div class="prop"><div class="p-content">' + esc(x) + '</div></div>').join('') + extra;
    }
  }

  async function loadCost() {
    const r = await api('/api/cost?days=7');
    if (!r || !r.ok) { return; }
    $('costToday').textContent = r.today_text;
    $('costMonth').textContent = r.month_text;
    $('costCalls').textContent = (r.month && r.month.calls) || 0;

    const byDay = r.by_day || [];
    const max = Math.max.apply(null, byDay.map(d => d.cost).concat([0.0001]));
    $('costBars').innerHTML = byDay.length ? byDay.map(d => {
      const h = Math.max(2, Math.round(d.cost / max * 70));
      const label = (d.date || '').slice(5);
      const val = d.cost >= 0.01 ? d.cost.toFixed(2) : d.cost.toFixed(4);
      return '<div class="costbar' + (d.cost > 0 ? '' : ' zero') + '">'
        + '<div class="cb-val">' + val + '</div>'
        + '<div class="cb-bar" style="height:' + h + 'px"></div>'
        + '<div class="cb-day">' + esc(label) + '</div></div>';
    }).join('') : '<div class="emptybox">还没有用量记录</div>';
  }

  async function loadEvents() {
    const kind = $('evKind') ? $('evKind').value : '';
    const level = $('evLevel') ? $('evLevel').value : '';
    const q = '/api/events?limit=200' + (kind ? '&kind=' + encodeURIComponent(kind) : '')
      + (level ? '&level=' + encodeURIComponent(level) : '');
    const r = await api(q);
    if (!r || !r.ok) { return; }

    const sel = $('evKind');
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">全部事件类型</option>' + (r.kinds || []).map(k =>
        '<option value="' + esc(k) + '">' + esc(KIND_LABEL[k] || k) + '</option>').join('');
      sel.value = cur;
    }

    const rows = r.events || [];
    $('evList').innerHTML = rows.length ? rows.slice().reverse().map(e => {
      const time = (e.time || '').slice(11, 19);
      return '<div class="ev ' + esc(e.level) + '">'
        + '<span class="ev-dot"></span>'
        + '<span class="ev-t">' + esc(time) + '</span>'
        + '<span class="ev-k">' + esc(KIND_LABEL[e.kind] || e.kind) + '</span>'
        + '<span class="ev-m">' + esc(e.msg)
        + (e.data && e.data.streak !== undefined ? '（' + e.data.streak + '）' : '')
        + '</span></div>';
    }).join('') : '<div class="emptybox">还没有事件记录</div>';
  }

  // =========================================================================
  // 记忆库
  // =========================================================================
  const CAT_LABEL = {
    life: '生活', work: '学业工作', pref: '喜好', health: '身体',
    relation: '关系约定', plan: '计划', other: '其他',
  };

  let currentPreview = null;

  async function loadMemory() {
    const r = await api('/api/memory_status');
    if (!r || !r.ok) { return; }

    const w = r.worker || {};
    const phaseMap = { idle: '空闲', backfill: '补跑中', schedule: '定点沉淀', manual: '手动', unknown: '未知' };
    const badge = $('memPhase');
    badge.textContent = w.running ? '沉淀中…' : (phaseMap[w.phase] || '空闲');
    badge.className = 'badge ' + (w.running ? 'b-warn' : 'b-ok');

    $('memPending').textContent = '待沉淀 ' + (r.pending_count || 0) + ' 天';
    const done = (w.done || 0), failed = (w.failed || 0), cur = w.current || '';
    $('memDetail').textContent = cur
      ? ('正在处理 ' + cur + '（本次会话已完成 ' + done + ' 天 / 失败 ' + failed + ' 天）')
      : ('本次会话已沉淀 ' + done + ' 天，失败 ' + failed + ' 天'
         + '；运行中的机器人每天会自动跑一次');

    const days = (r.recent_days || []).slice().reverse();
    $('daySummaryList').innerHTML = days.length ? days.map(d =>
      '<div class="sumbox"><div class="s-date">' + esc(d.date) + '</div>'
      + '<div class="s-text">' + esc(d.text) + '</div></div>').join('')
      : '<div class="emptybox">还没有沉淀出任何摘要——点上面的按钮试试</div>';

    const weeks = (r.recent_weeks || []).slice().reverse();
    $('weekSummaryList').innerHTML = weeks.length ? weeks.map(w2 =>
      '<div class="sumbox"><div class="s-date">' + esc(w2.date) + '</div>'
      + '<div class="s-text">' + esc(w2.text) + '</div></div>').join('')
      : '<div class="emptybox">同一周攒够 3 天摘要后会自动生成周记</div>';

    loadFacts();
  }


  async function loadFacts() {
    const cat = $('factCat') ? $('factCat').value : '';
    const r = await api('/api/facts' + (cat ? '?category=' + encodeURIComponent(cat) : ''));
    if (!r || !r.ok) { return; }
    if ($('factTotal')) { $('factTotal').textContent = '（共 ' + (r.total || 0) + ' 条）'; }
    const rows = r.facts || [];
    $('factList').innerHTML = rows.length ? rows.map(f =>
      '<div class="fact">'
      + '<span class="f-tag">' + esc(CAT_LABEL[f.category] || f.category) + '</span>'
      + '<div class="f-body">' + esc(f.value)
      + '<div class="f-meta">记住 ' + (f.hits || 0) + ' 次 · 把握 '
      + Math.round((f.confidence || 0) * 100) + '%'
      + (f.source_date ? ' · 来源 ' + esc(f.source_date) : '') + '</div></div>'
      + '<button class="mini" data-delfact="' + f.id + '">删除</button></div>').join('')
      : '<div class="emptybox">还没有事实。沉淀记忆时会自动从对话里抽取。</div>';

    document.querySelectorAll('[data-delfact]').forEach(b => {
      b.onclick = async () => {
        const res = await api('/api/fact/delete', 'POST', { id: b.dataset.delfact, confirm: true });
        toast((res && res.msg) || '已处理');
        loadFacts();
      };
    });
  }

  async function runMemory(all) {
    toast(all ? '全部补跑中，耐心等一会儿…' : '开始沉淀…');
    const r = await api('/api/memory/run', 'POST', { all: !!all, confirm: true });
    toast((r && r.msg) || (r && r.ok ? '完成' : '失败'));
    loadMemory();
  }

  // =========================================================================
  // 调教：预演 / 滑杆 / 护栏
  // =========================================================================
  const DIALS = [
    ['温柔', '软不软'], ['撩', '推拉强度'], ['话痨', '话多不多'],
    ['调侃', '互怼程度'], ['直球', '感情表达直接度'],
  ];

  function renderDials(cfgDials) {
    const d = cfgDials || {};
    $('dialBox').innerHTML = DIALS.map(([key, hint]) => {
      const v = d[key] !== undefined ? Math.round(d[key] * 100) : 50;
      return '<div class="dial">'
        + '<div class="d-head"><span>' + esc(key) + '</span><span class="d-val" data-dv="' + esc(key) + '">' + v + '%</span></div>'
        + '<input type="range" min="0" max="100" value="' + v + '" data-dial="' + esc(key) + '">'
        + '<div class="d-hint">' + esc(hint) + '</div></div>';
    }).join('');
    document.querySelectorAll('[data-dial]').forEach(el => {
      el.oninput = () => {
        const k = el.dataset.dial;
        const target = document.querySelector('[data-dv="' + k + '"]');
        if (target) { target.textContent = el.value + '%'; }
      };
    });
  }

  function collectDials() {
    const out = {};
    document.querySelectorAll('[data-dial]').forEach(el => {
      out[el.dataset.dial] = Number(el.value) / 100;
    });
    return out;
  }

  async function loadDials() {
    const r = await api('/api/tone_dials');
    renderDials((r && r.dials) || {});
  }

  async function saveDials() {
    const r = await api('/api/tone_dials', 'POST', { dials: collectDials(), enabled: true, confirm: true });
    toast((r && r.msg) || '已保存');
  }

  async function doPreview() {
    const text = ($('pvInput').value || '').trim();
    if (!text) { toast('先输入一句她可能说的话'); return; }
    const btn = $('btnPreview');
    btn.disabled = true; btn.textContent = '生成中…';
    const r = await api('/api/preview', 'POST', { text: text, confirm: true });
    btn.disabled = false; btn.textContent = '生成预览';
    if (!r || !r.ok) { toast((r && r.msg) || '预览失败'); return; }

    currentPreview = { prompt: text, reply: r.used || r.raw };
    $('pvResult').style.display = 'block';
    const blocked = r.verdict === 'block';
    $('pvMeta').textContent = (r.latency ? r.latency + 's · ' : '') + (r.cost || '') + ' · ' + (r.model || '');
    $('pvBubbles').innerHTML = (r.bubbles || []).map(b =>
      '<div class="bubble' + (blocked ? ' blocked' : '') + '">' + esc(b) + '</div>').join('')
      || '<div class="emptybox">没有可发送的气泡</div>';
    $('pvRaw').innerHTML = r.reason
      ? ('审查结论：<b>' + esc(r.verdict === 'block' ? '拦下' : (r.verdict === 'warn' ? '放行但有提醒' : '通过'))
        + '</b> · ' + esc(r.reason))
      : '审查通过，这就是会真正发出去的内容。';
  }

  async function sendFeedback(score) {
    if (!currentPreview) { toast('先生成一个预览'); return; }
    const r = await api('/api/feedback', 'POST', {
      score: score, prompt: currentPreview.prompt, reply: currentPreview.reply,
      chat: 'preview', confirm: true,
    });
    if ($('pvRate')) {
      $('pvRate').textContent = (r && r.msg) || '';
    }
    toast((r && r.msg) || '已记录');
  }

  async function loadGuard() {
    const r = await api('/api/guard');
    if (!r || !r.ok) { return; }
    const g = r.guard || {};
    if ($('gdEnabled')) { $('gdEnabled').checked = g.enabled !== false; }
    if ($('gdStrict')) { $('gdStrict').checked = !!g.strict; }
    if ($('gdMaxH')) { $('gdMaxH').value = g.max_per_hour || 30; }
    if ($('gdDry')) { $('gdDry').value = g.dry_streak_limit || 3; }
    if ($('gdQuiet')) {
      const q = g.quiet_hours || [];
      $('gdQuiet').value = q.length ? q[0].join(':') : '';
    }
    if ($('gdQuietNow')) {
      $('gdQuietNow').textContent = (r.now_quiet && r.now_quiet[0])
        ? ('当前处于免打扰：' + r.now_quiet[1]) : '当前不在免打扰时段';
    }
  }

  async function saveGuard() {
    const q = ($('gdQuiet').value || '').trim();
    const payload = {
      enabled: $('gdEnabled').checked,
      strict: $('gdStrict').checked,
      max_per_hour: Number($('gdMaxH').value) || 30,
      dry_streak_limit: Number($('gdDry').value) || 3,
      quiet_hours: [],
      confirm: true,
    };
    if (q && q.indexOf('-') > 0) {
      const a = q.split('-')[0].trim(), b = q.split('-')[1].trim();
      payload.quiet_hours = [[a, b]];
    }
    const r = await api('/api/guard/save', 'POST', payload);
    toast((r && r.msg) || '已保存');
    loadGuard();
  }

  async function testGuard() {
    const t = ($('gdTest').value || '').trim();
    if (!t) { toast('输入一段文字'); return; }
    const r = await api('/api/guard/test', 'POST', { text: t, confirm: true });
    if (!r || !r.ok) { toast((r && r.msg) || '测试失败'); return; }
    $('gdTestOut').innerHTML = '结论：<b>' +
      esc(r.verdict === 'block' ? '拦下' : (r.verdict === 'warn' ? '放行但有提醒' : '通过')) + '</b>'
      + (r.reason ? ' · ' + esc(r.reason) : '')
      + (r.bubbles && r.bubbles.length ? '<br>清洗后会发出：' + esc(r.bubbles.join(' / ')) : '');
  }

  // =========================================================================
  // 挂载
  // =========================================================================
  function bind() {
    const on = (id, fn) => { const el = $(id); if (el) { el.onclick = fn; } };

    on('btnEvRefresh', loadEvents);
    on('btnEvClear', async () => {
      const r = await api('/api/events/clear', 'POST', { confirm: true });
      toast((r && r.msg) || '已清空'); loadEvents();
    });
    if ($('evKind')) { $('evKind').onchange = loadEvents; }
    if ($('evLevel')) { $('evLevel').onchange = loadEvents; }

    on('btnMemRun1', () => runMemory(false));
    on('btnMemRunAll', () => runMemory(true));
    if ($('factCat')) { $('factCat').onchange = loadFacts; }
    on('btnFactAdd', async () => {
      const key = ($('factKey').value || '').trim();
      const val = ($('factVal').value || '').trim();
      if (!key || !val) { toast('类别和内容都要填'); return; }
      const r = await api('/api/fact/add', 'POST', { key: key, value: val, confirm: true });
      toast((r && r.msg) || '已处理');
      if ($('factKey')) { $('factKey').value = ''; }
      if ($('factVal')) { $('factVal').value = ''; }
      loadFacts();
    });

    on('btnPreview', doPreview);
    on('pvUp', () => sendFeedback(1));
    on('pvDown', () => sendFeedback(-1));
    on('btnDialSave', saveDials);
    on('btnGdSave', saveGuard);
    on('btnGdTest', testGuard);
  }

  let lastTab = '';
  function tick() {
    if (!pageVisible()) { return; }
    const p = activePage();
    if (p === lastTab) {
      if (p === 'insight') { loadInsights(); loadEvents(); }
      return;
    }
    lastTab = p;
    if (p === 'insight') { loadInsights(); loadCost(); loadEvents(); }
    else if (p === 'brain') { loadMemory(); }
    else if (p === 'tune') { loadDials(); loadGuard(); }
  }

  function init() {
    if (!$('page-insight')) { return; }
    bind();
    tick();
    setInterval(tick, 4000);
    document.addEventListener('visibilitychange', () => { if (pageVisible()) { lastTab = ''; tick(); } });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  window.__extraTick = tick;
})();

// ---------------------------------------------------------------------------
// 3.1 面板增强：F2 接管横幅 / F3 日志着色 / F5 主题 / F6 命令面板
//
// 又一个独立 IIFE，理由同上：不动 2.0 的 app.js，也不动 3.0 的 extra.js 主体。
// 所有操作尽量复用页面上已有的按钮（直接 .click()），不重复实现一遍控制逻辑。
// ---------------------------------------------------------------------------
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const TOKEN = (document.querySelector('meta[name="panel-token"]') || {}).content || '';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function api(path, method, body) {
    try {
      const opt = { method: method || 'GET', headers: { 'Content-Type': 'application/json' } };
      if (TOKEN) { opt.headers['X-Panel-Token'] = TOKEN; }
      if (body) { opt.body = JSON.stringify(body); }
      const r = await fetch(path, opt);
      return await r.json().catch(() => ({}));
    } catch (e) {
      return {};
    }
  }

  // =========================================================================
  // F5 深浅色主题（记住选择）
  // =========================================================================
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('wb_theme', t); } catch (e) { /* 隐私模式忽略 */ }
    const b = $('btnTheme');
    if (b) { b.textContent = t === 'dark' ? '浅色' : '深色'; }
  }
  function initTheme() {
    let t = 'dark';
    try { t = localStorage.getItem('wb_theme') || 'dark'; } catch (e) { /* ignore */ }
    setTheme(t);
    const foot = document.querySelector('.foot');
    if (foot && !$('btnTheme')) {
      const b = document.createElement('button');
      b.id = 'btnTheme';
      b.className = 'mini themebtn';
      b.type = 'button';
      b.onclick = () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
      foot.insertBefore(b, foot.firstChild);
    }
  }

  // =========================================================================
  // F2 接管中醒目横幅 + 标签页标题
  // =========================================================================
  function ensureBar() {
    let bar = $('takeoverBar');
    if (!bar) {
      const wrap = document.querySelector('.wrap');
      if (!wrap) { return null; }
      bar = document.createElement('div');
      bar.id = 'takeoverBar';
      bar.className = 'tbar';
      bar.innerHTML = '<span class="tb-dot"></span>'
        + '<span><b>已暂停</b> · AI 不会自动回复她，现在是你自己在聊</span>'
        + '<button class="mini tb-btn" id="tbResume" type="button">恢复自动回复</button>';
      wrap.insertBefore(bar, wrap.firstChild);
      const rb = $('tbResume');
      if (rb) { rb.onclick = () => { const t = $('btnResume'); if (t) { t.click(); } }; }
    }
    return bar;
  }

  async function refreshBar() {
    const bar = ensureBar();
    if (!bar) { return; }
    const s = await api('/api/status');
    if (!s || typeof s.paused === 'undefined') { return; }
    const paused = !!s.paused;
    bar.classList.toggle('show', paused);
    const base = '微信 AI 陪聊 · 控制面板';
    document.title = paused ? '[已暂停] ' + base : (s.running ? '' : '[未运行] ') + base;
  }

  // =========================================================================
  // F3 日志着色与过滤
  // =========================================================================
  let logLines = [];
  let lastRaw = '';
  let decorating = false;

  function classify(line) {
    if (/\[ERROR\]|Traceback|Exception/.test(line)) { return 'err'; }
    if (/\[WARNING\]/.test(line)) { return 'warn'; }
    if (/收到 \[/.test(line)) { return 'her'; }
    if (/回复 \[|主动发/.test(line)) { return 'ai'; }
    return 'sys';
  }

  function renderLog() {
    const box = $('logbox');
    if (!box) { return; }
    const kw = ($('logKw') ? $('logKw').value : '').trim().toLowerCase();
    const onlyBad = $('logOnlyErr') ? $('logOnlyErr').checked : false;
    let rows = logLines;
    if (onlyBad) { rows = rows.filter(l => classify(l) === 'err' || classify(l) === 'warn'); }
    if (kw) { rows = rows.filter(l => l.toLowerCase().indexOf(kw) >= 0); }
    decorating = true;
    box.innerHTML = rows.length
      ? rows.map(l => '<div class="logline ' + classify(l) + '">' + esc(l) + '</div>').join('\n')
      : '<div class="logline l-sys">（没有匹配的行）</div>';
    decorating = false;
    if ($('logAuto') && $('logAuto').checked) { box.scrollTop = box.scrollHeight; }
    const c = $('logCount');
    if (c) { c.textContent = rows.length + ' / ' + logLines.length + ' 行'; }
  }

  function decorateLog() {
    const box = $('logbox');
    if (!box || decorating) { return; }
    const raw = box.textContent || '';
    if (raw === lastRaw) { return; }
    lastRaw = raw;
    logLines = raw.split('\n');
    renderLog();
  }

  function initLogTools() {
    const box = $('logbox');
    if (!box || !box.parentNode || $('logKw')) { return; }
    const tools = document.createElement('div');
    tools.className = 'logtools';
    tools.innerHTML = '<input type="text" id="logKw" placeholder="过滤关键字，如 收到 / 回复 / 失败">'
      + '<label class="sw"><input type="checkbox" id="logOnlyErr">只看异常</label>'
      + '<label class="sw"><input type="checkbox" id="logAuto" checked>自动滚动</label>'
      + '<span class="lt-cnt" id="logCount"></span>';
    box.parentNode.insertBefore(tools, box);
    $('logKw').oninput = renderLog;
    $('logOnlyErr').onchange = renderLog;
    new MutationObserver(decorateLog).observe(box, { childList: true, characterData: true, subtree: true });
    decorateLog();
  }

  // =========================================================================
  // F6 Ctrl+K 命令面板
  // =========================================================================
  const PAGE_LABEL = {
    console: '控制台', insight: '洞察', brain: '记忆库',
    tune: '调教', advisor: '参谋与提案', memory: '人设与记忆',
  };

  function clickIf(id) {
    const el = $(id);
    if (el) { el.click(); return true; }
    return false;
  }

  function gotoPage(p) {
    const t = document.querySelector('.nav-tab[data-page="' + p + '"]');
    if (t) { t.click(); }
  }

  function buildCommands() {
    const cmds = [
      { name: '开启机器人', hint: '启动 bot', run: () => clickIf('btnStart') },
      { name: '关闭机器人', hint: '杀掉进程', run: () => clickIf('btnStop') },
      { name: '暂停自动回复', hint: '人工接管', run: () => clickIf('btnPause') },
      { name: '恢复自动回复', hint: '交还给 AI', run: () => clickIf('btnResume') },
      { name: '重启机器人', hint: '重建微信连接', run: () => clickIf('btnRestart') },
      { name: '一键自检', hint: '体检', run: () => { gotoPage('console'); clickIf('btnSelfTest'); } },
      { name: '切换深浅色', hint: '主题', run: () => clickIf('btnTheme') },
      { name: '沉淀记忆', hint: '补跑一天', run: () => { gotoPage('brain'); setTimeout(() => clickIf('btnMemRun1'), 120); } },
      { name: '全部补跑记忆', hint: '耗时较长', run: () => { gotoPage('brain'); setTimeout(() => clickIf('btnMemRunAll'), 120); } },
      { name: '生成回复预演', hint: '干跑', run: () => gotoPage('tune') },
    ];
    Object.keys(PAGE_LABEL).forEach(p => {
      cmds.push({ name: '跳到「' + PAGE_LABEL[p] + '」', hint: '页面', run: () => gotoPage(p) });
    });
    return cmds;
  }

  let cmds = [];
  let selIdx = 0;

  function ensureCmdk() {
    let mask = $('cmdkMask');
    if (!mask) {
      mask = document.createElement('div');
      mask.id = 'cmdkMask';
      mask.className = 'cmdk-mask';
      mask.innerHTML = '<div class="cmdk">'
        + '<input type="text" id="cmdkInput" placeholder="输入命令…（↑↓ 选择，Enter 执行，Esc 关闭）">'
        + '<div class="cmdk-list" id="cmdkList"></div></div>';
      document.body.appendChild(mask);
      const inp = $('cmdkInput');
      inp.oninput = () => { selIdx = 0; renderCmdk(); };
      inp.onkeydown = e => {
        if (e.key === 'ArrowDown') { selIdx = Math.min(selIdx + 1, cmds.length - 1); renderCmdk(); e.preventDefault(); }
        else if (e.key === 'ArrowUp') { selIdx = Math.max(selIdx - 1, 0); renderCmdk(); e.preventDefault(); }
        else if (e.key === 'Enter') { runSel(); e.preventDefault(); }
        else if (e.key === 'Escape') { closeCmdk(); }
      };
      mask.onclick = e => { if (e.target === mask) { closeCmdk(); } };
    }
    return mask;
  }

  function filtered() {
    const q = ($('cmdkInput') ? $('cmdkInput').value : '').trim().toLowerCase();
    if (!q) { return cmds; }
    return cmds.filter(c => (c.name + c.hint).toLowerCase().indexOf(q) >= 0);
  }

  function renderCmdk() {
    const list = $('cmdkList');
    if (!list) { return; }
    const rows = filtered();
    if (!rows.length) { list.innerHTML = '<div class="cmdk-empty">没有匹配的命令</div>'; return; }
    if (selIdx >= rows.length) { selIdx = 0; }
    list.innerHTML = rows.map((c, i) =>
      '<div class="cmdk-item' + (i === selIdx ? ' sel' : '') + '" data-i="' + i + '">'
      + '<span>' + esc(c.name) + '</span><span class="ck-hint">' + esc(c.hint) + '</span></div>').join('');
    list.querySelectorAll('.cmdk-item').forEach(el => {
      el.onclick = () => { selIdx = Number(el.dataset.i); runSel(); };
      el.onmouseenter = () => { selIdx = Number(el.dataset.i); };
    });
  }

  function runSel() {
    const rows = filtered();
    const c = rows[selIdx];
    closeCmdk();
    if (c) { c.run(); }
  }

  function openCmdk() {
    cmds = buildCommands();
    const mask = ensureCmdk();
    mask.classList.add('show');
    const inp = $('cmdkInput');
    inp.value = '';
    selIdx = 0;
    renderCmdk();
    inp.focus();
  }

  function closeCmdk() {
    const mask = $('cmdkMask');
    if (mask) { mask.classList.remove('show'); }
  }

  function initCmdk() {
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { openCmdk(); e.preventDefault(); return; }
      if (e.key === 'Escape') { closeCmdk(); }
      // 单键快捷：不在输入框里才生效
      const tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return; }
      if (e.key === 'p' || e.key === 'P') { clickIf('btnPause'); }
      else if (e.key === 'r' || e.key === 'R') { clickIf('btnResume'); }
      else if (e.key === '?') { openCmdk(); }
    });
  }

  // =========================================================================
  // C4 提案中心减负：合并重复 / 归档过期 / 批量通过拒绝
  // =========================================================================
  function refreshPropList() {
    const b = $('btnRefreshProps');
    if (b) { b.click(); }
  }

  async function loadPropStats() {
    const r = await api('/api/proposals/stats');
    const el = $('propStats');
    if (!el || !r) { return; }
    el.textContent = '待处理 ' + (r.pending || 0) + ' 条'
      + (r.dup ? ' · 重复 ' + r.dup + ' 条' : '')
      + (r.stale ? ' · 超 ' + r.archive_days + ' 天未处理 ' + r.stale + ' 条' : '');
  }

  function ensurePropTools() {
    const list = $('propsList');
    if (!list || !list.parentNode || $('propTools')) { return; }
    const box = document.createElement('div');
    box.className = 'logtools';
    box.id = 'propTools';
    box.innerHTML = '<span class="hint" id="propStats">—</span>'
      + '<button class="mini" id="propDedupe" type="button">合并重复</button>'
      + '<button class="mini" id="propArchive" type="button">归档过期</button>'
      + '<button class="mini" id="propApproveAll" type="button">全部通过</button>'
      + '<button class="mini danger" id="propRejectAll" type="button">全部拒绝</button>';
    list.parentNode.insertBefore(box, list);

    const act = (id, path, body, tip) => {
      const b = $(id);
      if (!b) { return; }
      b.onclick = async () => {
        if (tip && !window.confirm(tip)) { return; }
        const r = await api(path, 'POST', body);
        alert((r && r.msg) || '已处理');
        refreshPropList();
        loadPropStats();
      };
    };
    act('propDedupe', '/api/proposals/dedupe', { confirm: true });
    act('propArchive', '/api/proposals/archive_stale', { confirm: true, days: 30 });
    act('propApproveAll', '/api/proposals/batch',
      { all: true, status: 'approved', confirm: true },
      '确定要通过全部待处理提案吗？它们会被写进人设 / 关系档案。');
    act('propRejectAll', '/api/proposals/batch',
      { all: true, status: 'rejected', confirm: true },
      '确定要拒绝全部待处理提案吗？');
    loadPropStats();
  }

  // =========================================================================
  // C3 人设卡再蒸馏：只产草案，主人点头才覆盖
  // =========================================================================
  async function loadPersona() {
    const r = await api('/api/persona/status');
    if (!r || !r.ok) { return; }
    const box = $('personaBox2');
    if (!box) { return; }
    const enough = (r.samples || 0) >= (r.min_samples || 40);
    $('pnSamples').textContent = '真人样本 ' + (r.samples || 0) + ' 条'
      + (enough ? '' : '（不足 ' + (r.min_samples || 40) + ' 条，多聊几天再来）');
    const cur = (r.current || '').trim();
    $('pnCurrent').textContent = cur ? cur.slice(0, 220) + (cur.length > 220 ? '…' : '') : '（还没有人设卡）';
    if (r.has_proposal) {
      $('pnDraft').style.display = 'block';
      const d = (r.proposal || '').trim();
      $('pnDraftText').textContent = d;
      $('pnDraftTime').textContent = r.generated_at
        ? '草案生成于 ' + new Date(r.generated_at * 1000).toLocaleString('zh-CN') : '';
    } else {
      $('pnDraft').style.display = 'none';
    }
    const rb = $('btnPersonaRe');
    if (rb) { rb.disabled = !enough; }
    const ab = $('btnPersonaApply');
    if (ab) { ab.disabled = !r.has_proposal; }
  }

  function initPersona() {
    const page = $('page-tune');
    if (!page || $('personaBox2')) { return; }
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'personaBox2';
    card.style.marginTop = '12px';
    card.innerHTML = '<div class="card-head"><span class="k">人设卡再蒸馏（防回声室）</span>'
      + '<span class="hint" id="pnSamples">—</span></div>'
      + '<div class="hint">只用你自己手打的消息重新蒸馏，AI 代发的一条都不进样本。'
      + '生成的是草案，你点头才覆盖（旧卡自动备份）。</div>'
      + '<div class="prop" style="margin-top:10px;"><div class="p-content" '
      + 'style="white-space:pre-wrap;max-height:150px;overflow:auto;" id="pnCurrent">—</div>'
      + '<div class="f-meta">当前人设卡</div></div>'
      + '<div class="prop" id="pnDraft" style="display:none;border-color:rgba(79,156,255,.5);">'
      + '<div class="p-content" style="white-space:pre-wrap;max-height:190px;overflow:auto;" '
      + 'id="pnDraftText"></div><div class="f-meta" id="pnDraftTime"></div></div>'
      + '<div class="row" style="margin-top:10px;">'
      + '<button class="mini" id="btnPersonaRe" type="button">重新蒸馏</button>'
      + '<button class="mini" id="btnPersonaApply" type="button">采纳草案</button>'
      + '<span class="hint" id="pnMsg"></span></div>';
    page.appendChild(card);
    const on = (id, fn) => { const el = $(id); if (el) { el.onclick = fn; } };
    on('btnPersonaRe', async () => {
      const b = $('btnPersonaRe');
      b.disabled = true; b.textContent = '蒸馏中…';
      const r = await api('/api/persona/redistill', 'POST', { confirm: true });
      b.disabled = false; b.textContent = '重新蒸馏';
      const m = $('pnMsg');
      if (m) { m.textContent = (r && r.msg) || '完成'; }
      loadPersona();
    });
    on('btnPersonaApply', async () => {
      if (!window.confirm('确定用草案覆盖现有人设卡吗？旧卡会自动备份到 persona.md.bak。')) { return; }
      const r = await api('/api/persona/apply', 'POST', { confirm: true });
      const m = $('pnMsg');
      if (m) { m.textContent = (r && r.msg) || '完成'; }
      loadPersona();
    });
    loadPersona();
  }

  // =========================================================================
  // B8 多监听对象：一个人一套设置，早安别再发错人
  // =========================================================================
  async function loadChats() {
    const box = $('chBody');
    if (!box) { return; }
    const r = await api('/api/chats');
    if (!r || !r.ok) { return; }
    const d = r.defaults || {};
    const t = $('chTitle');
    if (t) { t.textContent = (r.count || 0) + ' 个，启用 ' + (r.enabled || 0) + ' 个'; }
    box.innerHTML = (r.chats || []).map(c =>
      '<div class="prop' + (c.enabled ? ' p-on' : '') + '">'
      + '<div class="p-content"><b>' + esc(c.name) + '</b> '
      + '<span class="hint">' + (c.customized ? '有独立配置' : '用全局配置') + '</span></div>'
      + '<div class="f-meta">冷却 ' + (c.cooldown == null ? d.cooldown : c.cooldown) + ' 秒'
      + (c.persona_file ? ' · 人设 ' + esc(c.persona_file) : '')
      + (c.relationship_file ? ' · 关系 ' + esc(c.relationship_file) : '')
      + '</div>'
      + '<div class="p-actions">'
      + '<button class="mini" data-chat="' + esc(c.name) + '" data-patch="'
      + esc(JSON.stringify({ enabled: !c.enabled })) + '" type="button">'
      + (c.enabled ? '停用' : '启用') + '</button>'
      + '<button class="mini" data-chat="' + esc(c.name) + '" data-prompt="cooldown" '
      + 'type="button">改冷却</button>'
      + '<button class="mini" data-chat="' + esc(c.name) + '" data-prompt="persona" '
      + 'type="button">独立人设</button>'
      + '</div></div>').join('')
      || '<div class="emptybox">还没有监听对象</div>';

    box.querySelectorAll('[data-patch]').forEach(b => {
      b.onclick = async () => {
        const res = await api('/api/chats/update', 'POST',
          { name: b.dataset.chat, patch: JSON.parse(b.dataset.patch), confirm: true });
        const m = $('chMsg');
        if (m) { m.textContent = (res && res.msg) || '完成'; }
        loadChats();
      };
    });
    box.querySelectorAll('[data-prompt]').forEach(b => {
      b.onclick = async () => {
        const name = b.dataset.chat;
        let patch = null;
        if (b.dataset.prompt === 'cooldown') {
          const v = window.prompt(name + ' 的冷却秒数（留空＝跟随全局）：');
          if (v === null) { return; }
          patch = { cooldown: v.trim() === '' ? null : Number(v) };
        } else {
          const v = window.prompt(name + ' 单独用的人设文件路径\n'
            + '（相对项目目录，如 data/persona_mafei.md；留空＝跟随全局）：');
          if (v === null) { return; }
          patch = { persona_file: v.trim() };
        }
        const res = await api('/api/chats/update', 'POST',
          { name: name, patch: patch, confirm: true });
        const m = $('chMsg');
        if (m) { m.textContent = (res && res.msg) || '完成'; }
        loadChats();
      };
    });
  }

  function initChats() {
    const page = $('page-tune');
    if (!page || $('chBox')) { return; }
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'chBox';
    card.style.marginTop = '12px';
    card.innerHTML = '<div class="card-head"><span class="k">监听对象</span>'
      + '<span class="hint" id="chTitle">—</span></div>'
      + '<div class="hint">每个对象可以有自己的冷却、人设卡和关系档案；'
      + '没单独设的就跟随全局。定时任务会发给所有启用的对象（以前只发给第一个）。</div>'
      + '<div id="chBody" style="margin-top:10px;"></div>'
      + '<div class="row" style="margin-top:8px;">'
      + '<input id="chNew" type="text" placeholder="新增对象昵称" style="width:160px;">'
      + '<button class="mini" id="btnChAdd" type="button">添加</button>'
      + '<span class="hint" id="chMsg"></span></div>';
    page.appendChild(card);
    const add = $('btnChAdd');
    if (add) {
      add.onclick = async () => {
        const inp = $('chNew');
        if (!inp || !inp.value.trim()) { return; }
        const res = await api('/api/chats/add', 'POST',
          { name: inp.value.trim(), confirm: true });
        const m = $('chMsg');
        if (m) { m.textContent = (res && res.msg) || '完成'; }
        inp.value = '';
        loadChats();
      };
    }
    loadChats();
  }

  // =========================================================================
  // E3 消息通道：现在走的是哪条路、它还好吗、要不要换一条
  // =========================================================================
  const RISK_NAME = { low: '低风险', mid: '中等', high: '有风险' };

  async function loadBackend() {
    const box = $('bkBody');
    if (!box) { return; }
    const r = await api('/api/backend/health');
    if (!r || !r.ok) { return; }
    const cur = r.current || '—';
    const t = $('bkTitle');
    if (t) { t.textContent = '当前 ' + cur + (r.failover_enabled ? ' · 自动降级已开' : ' · 自动降级已关'); }
    box.innerHTML = (r.backends || []).map(b =>
      '<div class="prop' + (b.current ? ' p-on' : '') + '">'
      + '<div class="p-content"><b>' + esc(b.name) + '</b> '
      + (b.current ? '<span class="kbd">使用中</span> ' : '')
      + '<span class="hint">' + esc(b.risk_note || RISK_NAME[b.risk] || '') + '</span></div>'
      + '<div class="f-meta">健康度 ' + b.score + ' 分 · 成功 ' + b.ok + ' / 失败 ' + b.fail
      + (b.consec_fail ? ' · 连续失败 ' + b.consec_fail : '')
      + (b.last_error ? '<br>最近错误：' + esc(b.last_error) : '') + '</div>'
      + (b.current ? '' : '<div class="p-actions"><button class="mini" data-bk="' + esc(b.name)
        + '" type="button">切到这条</button></div>')
      + '</div>').join('')
      + '<div class="hint" style="margin-top:8px;">连续失败 '
      + (r.fail_threshold || 3) + ' 次会自动往下一条通道降级；'
      + '不会自动切回来——要切回来在这里点按钮。</div>';
    box.querySelectorAll('[data-bk]').forEach(btn => {
      btn.onclick = async () => {
        if (!window.confirm('确定把消息通道切到 ' + btn.dataset.bk + ' 吗？'
          + '切换期间有几秒收不到消息。')) { return; }
        const res = await api('/api/backend/switch', 'POST',
          { name: btn.dataset.bk, confirm: true });
        const m = $('bkMsg');
        if (m) { m.textContent = (res && res.msg) || '完成'; }
        setTimeout(loadBackend, 2500);
      };
    });
  }

  function initBackend() {
    const page = $('page-insight');
    if (!page || $('bkBox')) { return; }
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'bkBox';
    card.style.marginTop = '12px';
    card.innerHTML = '<div class="card-head"><span class="k">消息通道（后端）</span>'
      + '<span class="hint" id="bkTitle">—</span></div>'
      + '<div id="bkBody"></div>'
      + '<div class="row" style="margin-top:8px;"><span class="hint" id="bkMsg"></span></div>';
    const anchor = $('remBox');
    if (anchor) { page.insertBefore(card, anchor); } else { page.appendChild(card); }
    loadBackend();
  }

  // =========================================================================
  // C5 每日回顾：一天结束，你只想知道四件事
  // =========================================================================
  const MOOD_COLOR = { high: '#f85149', mid: '#e3b341', calm: '#3fb950', none: 'transparent' };
  const MOOD_NAME = { high: '不太好', mid: '有波动', calm: '平稳', none: '没聊' };

  let dailyDate = '';

  async function loadDaily(dateStr) {
    const box = $('dayBody');
    if (!box) { return; }
    if (dateStr) { dailyDate = dateStr; }
    const q = '/api/daily_review' + (dailyDate ? '?date=' + encodeURIComponent(dailyDate) : '');
    const r = await api(q);
    if (!r || !r.ok) { return; }
    dailyDate = r.date;
    const t = $('dayTitle');
    if (t) { t.textContent = r.date + (r.date === new Date().toLocaleDateString('sv') ? '（今天）' : ''); }

    if (!r.has_data) {
      box.innerHTML = '<div class="emptybox">这天没有聊天记录</div>';
    } else {
      const s = r.stats || {};
      const curve = r.mood_curve || [];
      const bars = curve.map(c => {
        const h = c.n ? Math.max(3, Math.min(30, 3 + c.n * 2)) : 2;
        const col = MOOD_COLOR[c.level] || MOOD_COLOR.none;
        return '<div class="mbar" title="' + c.hour + '点 · ' + (MOOD_NAME[c.level] || '')
          + ' · ' + c.n + ' 条' + (c.tags && c.tags.length ? '（' + esc(c.tags.join('、')) + '）' : '')
          + '"><div class="mb-fill" style="height:' + h + 'px;background:' + col + '"></div>'
          + '<div class="mb-h">' + (c.hour % 3 === 0 ? c.hour : '') + '</div></div>';
      }).join('');

      box.innerHTML =
        '<div class="row" style="gap:14px;flex-wrap:wrap;margin-bottom:10px;">'
        + '<span>她说 <b>' + s.her_msgs + '</b> 条</span>'
        + '<span>AI 回 <b>' + s.ai_replies + '</b> 条</span>'
        + '<span>她均 <b>' + s.avg_her + '</b> 字</span>'
        + '<span>AI 均 <b>' + s.avg_ai + '</b> 字</span>'
        + '<span>花费 <b>' + (s.cost || 0).toFixed(4) + '</b> 元</span>'
        + '<span>新事实 <b>' + s.new_facts + '</b> 条</span></div>'
        + '<div class="hint" style="margin-bottom:6px;">' + esc(r.mood_text || '') + '</div>'
        + '<div class="mcurve">' + bars + '</div>'
        + '<div class="dsec"><div class="dsec-t">这天聊了什么</div>'
        + (r.summary
          ? '<div class="dsum">' + esc(r.summary) + '</div>'
          : '<div class="hint">还没有日总结，去「记忆库」补跑这一天就有了</div>') + '</div>'
        + '<div class="dsec"><div class="dsec-t">新记住的（' + (r.facts || []).length + '）</div>'
        + ((r.facts || []).length
          ? (r.facts || []).map(f =>
            '<div class="prop"><div class="p-content">' + esc(f.value) + '</div>'
            + '<div class="f-meta">' + esc(f.category || 'other') + ' · ' + esc(f.key || '') + '</div></div>').join('')
          : '<div class="hint">这天没抽出新事实</div>') + '</div>'
        + ((r.reminders_due || []).length
          ? '<div class="dsec"><div class="dsec-t">这天的约定</div>'
          + r.reminders_due.map(x =>
            '<div class="prop p-hot"><div class="p-content">' + esc(x.title) + '</div>'
            + '<div class="f-meta">' + esc(x.date) + (x.repeat === 'yearly' ? ' · 每年' : '') + '</div></div>').join('')
          + '</div>'
          : '');
    }

    const chips = $('dayChips');
    if (chips) {
      chips.innerHTML = (r.recent_dates || []).map(d =>
        '<button class="mini' + (d === r.date ? ' on' : '') + '" data-day="' + esc(d) + '" '
        + 'type="button" style="padding:2px 7px;">' + esc(d.slice(5)) + '</button>').join('');
      chips.querySelectorAll('[data-day]').forEach(b => {
        b.onclick = () => loadDaily(b.dataset.day);
      });
    }
    const pb = $('dayPrev'), nb = $('dayNext');
    if (pb) { pb.disabled = !r.prev_date; pb.dataset.day = r.prev_date || ''; }
    if (nb) { nb.disabled = !r.next_date; nb.dataset.day = r.next_date || ''; }
  }

  function initDaily() {
    const page = $('page-insight');
    if (!page || $('dayBox')) { return; }
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'dayBox';
    card.innerHTML = '<div class="card-head"><span class="k">每日回顾</span>'
      + '<span class="hint" id="dayTitle">—</span></div>'
      + '<div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:10px;">'
      + '<button class="mini" id="dayPrev" type="button">← 前一天</button>'
      + '<button class="mini" id="dayNext" type="button">后一天 →</button>'
      + '<button class="mini" id="dayToday" type="button">回到今天</button>'
      + '<span class="hint" id="dayChips" style="display:flex;gap:4px;flex-wrap:wrap;"></span></div>'
      + '<div id="dayBody"></div>';
    // 插到页面最前面：回顾是「一眼掌握今天」，不该排在后面要人翻
    page.insertBefore(card, page.firstChild);
    const on = (id, fn) => { const el = $(id); if (el) { el.onclick = fn; } };
    on('dayPrev', () => { const b = $('dayPrev'); if (b && b.dataset.day) { loadDaily(b.dataset.day); } });
    on('dayNext', () => { const b = $('dayNext'); if (b && b.dataset.day) { loadDaily(b.dataset.day); } });
    on('dayToday', () => loadDaily(''));
    loadDaily('');
  }

  // =========================================================================
  // C6 约定与纪念日：写在 relationship.md 里的日期，得有人替你记着
  // =========================================================================
  function rmDayText(d) {
    if (d === 0) { return '今天'; }
    if (d === 1) { return '明天'; }
    if (d < 0) { return Math.abs(d) + ' 天前'; }
    return d + ' 天后';
  }

  async function loadReminders() {
    const box = $('rmList');
    if (!box) { return; }
    const r = await api('/api/reminders?days=120');
    if (!r || !r.ok) { return; }
    const list = r.reminders || [];
    $('rmCount').textContent = list.length ? '最近 ' + list.length + ' 条' : '暂无';
    box.innerHTML = list.length ? list.map(x => {
      const urgent = x.days_left <= 3;
      return '<div class="prop' + (urgent ? ' p-hot' : '') + '">'
        + '<div class="p-content">' + esc(x.title) + '</div>'
        + '<div class="f-meta">' + esc(x.date)
        + ' · <b>' + rmDayText(x.days_left) + '</b>'
        + (x.repeat === 'yearly' ? ' · 每年' : '')
        + (x.source === 'manual' ? ' · 手填' : '')
        + ' <button class="mini" data-rmdel="' + esc(x.id) + '" type="button" '
        + 'style="margin-left:8px;padding:1px 6px;">删除</button></div></div>';
    }).join('') : '<div class="emptybox">关系档案里还没扫到带日期的约定。'
      + '在「人设与记忆 → 关系背景」写上「12.21 她考研」这类句子，'
      + '回来点重新扫描就有了；也可以直接在下面手填。</div>';
    box.querySelectorAll('[data-rmdel]').forEach(b => {
      b.onclick = async () => {
        const res = await api('/api/reminders/delete', 'POST',
          { id: b.dataset.rmdel, confirm: true });
        const m = $('rmMsg');
        if (m) { m.textContent = (res && res.msg) || '完成'; }
        loadReminders();
      };
    });
  }

  function initReminders() {
    const page = $('page-insight');
    if (!page || $('remBox')) { return; }
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'remBox';
    card.style.marginTop = '12px';
    card.innerHTML = '<div class="card-head"><span class="k">约定与纪念日</span>'
      + '<span class="hint" id="rmCount">—</span></div>'
      + '<div class="hint">从关系档案 / 长期事实里扫出来的日期，'
      + '到日子了会在面板上顶到最前面。手填的不会被「重新扫描」冲掉。</div>'
      + '<div id="rmList" style="margin-top:10px;max-height:280px;overflow:auto;"></div>'
      + '<div class="row" style="margin-top:10px;flex-wrap:wrap;">'
      + '<input id="rmTitle" type="text" placeholder="内容，如：她考研" style="flex:2;min-width:150px;">'
      + '<input id="rmDate" type="text" placeholder="2026-12-21 或 12-21" style="width:140px;">'
      + '<select id="rmRepeat" style="width:76px;">'
      + '<option value="none">一次性</option><option value="yearly">每年</option></select>'
      + '<button class="mini" id="btnRmAdd" type="button">添加</button>'
      + '<button class="mini" id="btnRmRefresh" type="button">重新扫描</button>'
      + '<span class="hint" id="rmMsg"></span></div>';
    page.appendChild(card);
    const on = (id, fn) => { const el = $(id); if (el) { el.onclick = fn; } };
    on('btnRmAdd', async () => {
      const t = $('rmTitle'), d = $('rmDate'), rp = $('rmRepeat');
      if (!t || !t.value.trim() || !d || !d.value.trim()) {
        const m = $('rmMsg');
        if (m) { m.textContent = '内容和日期都要填'; }
        return;
      }
      const res = await api('/api/reminders/add', 'POST',
        { title: t.value.trim(), date: d.value.trim(),
          repeat: rp ? rp.value : 'none', confirm: true });
      const m = $('rmMsg');
      if (m) { m.textContent = (res && res.msg) || '完成'; }
      if (res && res.ok) { t.value = ''; d.value = ''; }
      loadReminders();
    });
    on('btnRmRefresh', async () => {
      const res = await api('/api/reminders/refresh', 'POST', { confirm: true });
      const m = $('rmMsg');
      if (m) { m.textContent = (res && res.msg) || '完成'; }
      loadReminders();
    });
    loadReminders();
  }

  // =========================================================================
  // F4 新手指引（自检驱动，全部完成后自动隐藏）
  // =========================================================================
  async function loadGuide() {
    let hidden = false;
    try { hidden = localStorage.getItem('wb_guide_done') === '1'; } catch (e) { /* ignore */ }
    if (hidden) { return; }
    const r = await api('/api/onboarding');
    if (!r || !r.ok) { return; }
    if (r.finished) {
      try { localStorage.setItem('wb_guide_done', '1'); } catch (e) { /* ignore */ }
      const old = $('guideBox');
      if (old) { old.remove(); }
      return;
    }
    const wrap = document.querySelector('.wrap');
    const nav = document.querySelector('.nav-tabs');
    if (!wrap || !nav) { return; }
    let box = $('guideBox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'guideBox';
      box.className = 'guide';
      wrap.insertBefore(box, nav);
    }
    box.innerHTML = '<div class="g-title"><span>新手指引</span>'
      + '<span class="hint">' + (r.done || 0) + ' / ' + (r.total || 0) + ' 步完成</span></div>'
      + '<div class="g-step">换台电脑重装时，按这个顺序走一遍就够了</div>'
      + '<div class="g-list">' + (r.steps || []).map(s =>
        '<div class="g-item"><span class="' + (s.ok ? 'g-ok' : 'g-no') + '">'
        + (s.ok ? 'OK' : '!') + '</span>'
        + '<span class="g-txt">' + esc(s.title) + '<span class="hint"> · ' + esc(s.desc) + '</span></span>'
        + '<button class="mini" data-goto="' + esc(s.open) + '" type="button">去设置</button></div>'
      ).join('') + '</div>'
      + '<div class="row" style="margin-top:10px;"><button class="mini" id="guideHide" type="button">不再显示</button></div>';
    box.querySelectorAll('[data-goto]').forEach(b => {
      b.onclick = () => gotoPage(b.dataset.goto);
    });
    const hb = $('guideHide');
    if (hb) {
      hb.onclick = () => {
        try { localStorage.setItem('wb_guide_done', '1'); } catch (e) { /* ignore */ }
        box.remove();
      };
    }
  }

  // =========================================================================
  // D7 SSE：服务端主动推，前端不再空轮询
  // =========================================================================
  function initStream() {
    if (typeof EventSource === 'undefined') { return; }
    let es = null;
    const open = () => {
      try { es = new EventSource('/api/stream'); } catch (e) { return; }
      es.addEventListener('events', ev => {
        try {
          const d = JSON.parse(ev.data);
          const list = d.events || [];
          if (list.some(x => ['takeover', 'bot_start', 'bot_stop',
            'wechat_lost', 'wechat_back'].indexOf(x.kind) >= 0)) {
            refreshBar();
          }
          if (typeof window.__extraTick === 'function') { window.__extraTick(); }
        } catch (e) { /* 单条解析失败忽略 */ }
      });
      es.onerror = () => {
        try { if (es) { es.close(); } } catch (e) { /* ignore */ }
        setTimeout(open, 5000);   // 断线重连，退避 5 秒
      };
    };
    open();
  }

  function init() {
    initTheme();
    initLogTools();
    initCmdk();
    initStream();
    initPersona();
    initDaily();
    initReminders();
    initChats();
    initBackend();
    ensurePropTools();
    loadGuide();
    refreshBar();
    setInterval(refreshBar, 3000);
    setInterval(loadPropStats, 10000);
    // 跨零点后「还有几天」会变，定时刷一次，别让你看着昨天的数字
    setInterval(loadReminders, 300000);
    const h1 = document.querySelector('h1');
    if (h1 && !$('cmdkTip')) {
      const tip = document.createElement('span');
      tip.id = 'cmdkTip';
      tip.className = 'hint';
      tip.style.marginLeft = 'auto';
      tip.innerHTML = '按 <span class="kbd">Ctrl</span>+<span class="kbd">K</span> 打开命令面板';
      h1.appendChild(tip);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
