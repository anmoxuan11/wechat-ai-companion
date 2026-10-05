// ---------------------------------------------------------------------------
// 2.0 安全：面板令牌。服务端每次启动随机生成并注入 <meta name="panel-token">，
// 所有写请求必须带回它，否则 403。这样即使别的本地程序 / 恶意网页
// POST 到 127.0.0.1:8765 也操作不了机器人。
// ---------------------------------------------------------------------------
const TOKEN = (document.querySelector('meta[name="panel-token"]')||{}).content || '';

const $ = id => document.getElementById(id);
let timer = null;

function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2200);
}
async function api(path, method='GET', body){
  try{
    const opt = {method, headers:{'Content-Type':'application/json'}};
    if(TOKEN) opt.headers['X-Panel-Token'] = TOKEN;
    if(body) opt.body = JSON.stringify(body);
    const r = await fetch(path, opt);
    if(r.status === 403){
      const j = await r.json().catch(()=>({}));
      toast(j.msg || '请求被拒绝（令牌无效），请刷新页面重试');
      return {ok:false, msg:j.msg||'请求被拒绝'};
    }
    return await r.json();
  }catch(e){ return {ok:false, msg:'请求失败: '+e.message}; }
}

document.querySelectorAll('.nav-tab').forEach(t=>{
  t.onclick = ()=>{
    document.querySelectorAll('.nav-tab').forEach(x=>x.classList.toggle('active', x===t));
    document.querySelectorAll('.tab-page').forEach(p=>p.style.display='none');
    document.getElementById('page-'+t.dataset.page).style.display='block';
  };
});

function setRunning(s){
  const dot=$('statusDot'), rb=$('runningBadge');
  dot.className = 'dot ' + (s.running?'run':(s.paused?'pause':'stop'));
  if(s.running){
    rb.className='badge b-run'; rb.textContent='运行中';
  }else{
    rb.className='badge b-stop'; rb.textContent='未运行';
  }
  const pb=$('pauseBadge');
  if(!s.running){ pb.className='badge b-stop'; pb.textContent='已停止'; }
  else if(s.paused){ pb.className='badge b-pause'; pb.textContent='已暂停'; }
  else { pb.className='badge b-ok'; pb.textContent='自动回复中'; }

  $('listenVal').textContent = (s.listen||[]).join('、') || '—';
  $('uptimeVal').textContent = s.uptime || '—';
  $('pidVal').textContent = s.pid || '—';
  const bm = s.bot_mem_mb, pm = s.panel_mem_mb;
  $('memVal').textContent = (bm!=null?('bot '+bm+' MB'):'—') + ' · 面板 ' + (pm!=null?(pm+' MB'):'—');

  const wb=$('wechatBadge');
  if(s.wechat==='已连接'){ wb.className='badge b-ok'; wb.textContent='已连接'; }
  else if(s.wechat==='异常'){ wb.className='badge b-stop'; wb.textContent='异常'; }
  else { wb.className='badge b-warn'; wb.textContent=s.wechat; }

  // 按钮可用性
  $('btnStart').disabled = s.running;
  $('btnStop').disabled = !s.running;
  $('btnPause').disabled = !s.running || s.paused;
  $('btnResume').disabled = !s.running || !s.paused;
  $('btnRestart').disabled = !s.running;
  // 监听下拉同步
  const sel=$('listenSelect');
  const cur=(s.listen||[])[0];
  if(cur && sel.value!==cur) sel.value=cur;
}
// 2.0：页面不可见（切到别的标签页 / 最小化）时不轮询，回来立刻补一次。
// 1.0 里 3 秒一次无条件轮询，挂着一天就是 3 万次请求 + 3 万次日志读取。
const POLL_MS = 3000;
const IDLE_MS = 30000;   // 不可见时降到 30 秒一次，保持"回来就能看到最新"
let busy = false;

async function refresh(){
  if(busy) return;
  busy = true;
  try{
    const s = await api('/api/status');
    if(s.error) return;
    setRunning(s);
    const log = await api('/api/log?lines=200');
    if(log && log.log!==undefined){
      const box=$('logbox');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.textContent = log.log || '(暂无日志)';
      if(atBottom) box.scrollTop = box.scrollHeight;
    }
  } finally { busy = false; }
}

function setPoll(ms){
  if(timer) clearInterval(timer);
  timer = setInterval(()=>{ if(document.visibilityState === 'visible') refresh(); }, ms);
}
document.addEventListener('visibilitychange', ()=>{
  if(document.visibilityState === 'visible'){ refresh(); setPoll(POLL_MS); }
  else { setPoll(IDLE_MS); }
});
setPoll(POLL_MS);

async function act(btn, path, body, okMsg){
  btn.disabled=true;
  // 2.0：服务端对 /api/stop、/api/clear_memory、/api/shutdown 要求显式 confirm
  const payload = body ? Object.assign({}, body) : {};
  if(String(path).indexOf('/stop')>=0 || String(path).indexOf('/shutdown')>=0
     || String(path).indexOf('/clear_memory')>=0){
    payload.confirm = true;
  }
  const r = await api(path,'POST',Object.keys(payload).length?payload:null);
  toast(r.msg || okMsg || (r.ok?'完成':'操作失败'));
  await refresh();
  if(r.ok === false && String(r.msg||'').indexOf('令牌')>=0){ /* 已 toast */ }
  btn.disabled=false;
}
$('btnStart').onclick = e=>act(e.currentTarget,'/api/start',null,'已开启');
$('btnStop').onclick  = e=>act(e.currentTarget,'/api/stop',null,'已关闭');
$('btnResume').onclick= e=>act(e.currentTarget,'/api/resume',null,'已继续');
$('btnPause').onclick = e=>act(e.currentTarget,'/api/pause',null,'已暂停');
$('btnRestart').onclick=e=>act(e.currentTarget,'/api/restart',null,'已重启');
function renderSelfTest(r){
  $('selfTestBox').style.display='block';
  $('selfTestList').innerHTML = r.items.map(i=>
    '<div class="prop"><div class="p-head"><span class="p-badge '+(i.ok?'p-mem':'p-per')+'">'+(i.ok?'✓ 正常':'✗ 异常')+'</span>'+
    '<span style="font-size:13px;font-weight:600;color:var(--txt)">'+esc(i.name)+'</span></div>'+
    '<div class="p-reason">'+esc(i.detail)+'</div>'+
    (i.fix ? '<div class="p-actions"><button class="mini" data-fix="'+esc(i.fix)+'">🔧 一键修复</button></div>' : '')+
    '</div>').join('');
  $('selfTestList').querySelectorAll('button[data-fix]').forEach(b=>{
    b.onclick = async ()=>{
      b.disabled=true; b.textContent='修复中…';
      const rr = await api('/api/fix','POST',{action:b.dataset.fix});
      toast(rr.msg||(rr.ok?'已修复':'修复失败'));
      await refresh();
      await runSelfTest();
    };
  });
}
async function runSelfTest(){
  const b=$('btnSelfTest'); b.disabled=true; b.textContent='自检中…';
  const r = await api('/api/self_test','POST');
  b.disabled=false; b.textContent='🧪 一键自检';
  renderSelfTest(r);
  toast('自检完成：'+r.passed+'/'+r.total+' 项通过');
}
$('btnSelfTest').onclick = runSelfTest;
$('btnSwitch').onclick = async ()=>{
  const target=$('listenSelect').value;
  const b=$('btnSwitch'); b.disabled=true;
  const r=await api('/api/set_listen','POST',{target});
  toast(r.msg||'已切换');
  await refresh();
};
$('btnClear').onclick = async ()=>{
  if(!confirm('确定清空成长记忆？每日对话日志与摘要都会被删除，不可恢复。')) return;
  const b=$('btnClear'); b.disabled=true;
  const r=await api('/api/clear_memory','POST',{confirm:true});
  toast(r.msg||'已清空');
  await refresh();
  $('btnClear').disabled=false;
};
// ---- 人设 & 记忆 ----
let personaLoaded=false, memoryLoaded=false, relLoaded=false;
async function loadPanel(){
  const p = await api('/api/panel');
  if(p.error) return;
  if(!personaLoaded){ $('personaBox').value = p.persona||''; personaLoaded=true; }
  if(!memoryLoaded){ $('memoryBox').value = p.memory||''; memoryLoaded=true; }
  if(!relLoaded){ $('relBox').value = p.relationship||''; relLoaded=true; }
  const st=p.profile||{};
  const em = (st.emojis||[]).join(' ');
  $('profileStats').innerHTML =
    '来源统计：<b>'+ (st.total_messages||'?') +'</b> 条消息 · 平均 <b>'+ (st.avg_length||'?') +'</b> 字 · ' +
    '示范 <b>'+ (st.fewshot_pairs||0) +'</b> 组<br>口头禅：<b>'+ ((st.catchphrases||[]).join('、')||'无') +'</b> · 常用表情：<b>'+ (em||'无') +'</b>';
  $('promptPreview').textContent = p.prompt_preview || '(预览失败)';
}
async function loadDaily(){
  const r = await api('/api/daily');
  $('dailyBox').textContent = r.log || '(今日暂无记录)';
}
$('btnSavePersona').onclick = async e=>{
  const b=e.currentTarget; b.disabled=true;
  const r = await api('/api/save_persona','POST',{content:$('personaBox').value});
  toast(r.msg||'已保存'); personaLoaded=true; b.disabled=false;
};
$('btnSaveMemory').onclick = async e=>{
  const b=e.currentTarget; b.disabled=true;
  const r = await api('/api/save_memory','POST',{content:$('memoryBox').value});
  toast(r.msg||'已保存'); memoryLoaded=true; b.disabled=false;
};
$('btnSaveRel').onclick = async e=>{
  const b=e.currentTarget; b.disabled=true;
  const r = await api('/api/save_relationship','POST',{content:$('relBox').value});
  toast(r.msg||'已保存'); relLoaded=true; b.disabled=false;
};
$('btnRefreshPreview').onclick = async ()=>{ const p=await api('/api/panel'); $('promptPreview').textContent=p.prompt_preview||'(预览失败)'; };
$('btnRefreshDaily').onclick = loadDaily;
// ---- AI 参谋 & 提案中心 ----
const TYPE_LABEL = {relationship:'关系档案', persona:'人设卡', memory:'成长记忆', advice:'改进建议'};
const TYPE_CLS = {relationship:'p-rel', persona:'p-per', memory:'p-mem', advice:'p-adv'};
function addChatMsg(role, text){
  const box=$('chatbox');
  const div=document.createElement('div');
  div.className='chat-msg '+role;
  div.textContent=text;
  box.appendChild(div);
  box.scrollTop=box.scrollHeight;
}
$('btnSend').onclick = async ()=>{
  const input=$('chatInput');
  const msg=input.value.trim();
  if(!msg) return;
  input.value='';
  addChatMsg('user', msg);
  addChatMsg('ai', '（思考中…）');
  const r = await api('/api/advisor_chat','POST',{msg});
  const box=$('chatbox');
  const thinking=[...box.querySelectorAll('.chat-msg.ai')].pop();
  if(thinking && thinking.textContent==='（思考中…）') thinking.remove();
  addChatMsg('ai', r.reply || '(无回复)');
  if(r.proposals && r.proposals.length) toast('参谋生成了 '+r.proposals.length+' 条提案，请到下方审批');
  loadProposals();
};
$('chatInput').addEventListener('keydown', e=>{ if(e.key==='Enter') $('btnSend').click(); });
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
let propTab = 'pending';
function propCard(p){
  const done = p.status!=='pending';
  const time = new Date(p.created_at*1000).toLocaleString('zh-CN',{hour12:false});
  const isQ = p.kind==='question';
  let badge, actions='';
  if(done){
    const lbl = p.status==='approved' ? '✓ 已批准' : (p.status==='resolved' ? '已解决' : '✗ 已拒绝');
    badge = '<span class="p-badge p-done">'+lbl+'</span>';
  } else if(isQ){
    badge = '<span class="p-badge p-q">问询 · '+(TYPE_LABEL[p.type]||p.type)+'</span>';
    actions = '<div class="p-actions">'+
      '<button class="mini" data-act="chat" data-id="'+p.id+'">💬 去聊天回答</button>'+
      '<button class="mini" data-act="resolve" data-id="'+p.id+'">✓ 已解决</button></div>';
  } else {
    badge = '<span class="p-badge '+(TYPE_CLS[p.type]||'p-adv')+'">提议 · '+(TYPE_LABEL[p.type]||p.type)+'</span>';
    actions = '<div class="p-actions">'+
      '<button class="mini" data-act="approve" data-id="'+p.id+'">✓ 直接批准</button>'+
      '<button class="mini" data-act="edit" data-id="'+p.id+'">✎ 修改后批准</button>'+
      '<button class="mini" data-act="reject" data-id="'+p.id+'">✗ 拒绝</button></div>';
  }
  return '<div class="prop" id="prop-'+p.id+'">'+
    '<div class="p-head">'+badge+'<span style="font-size:11px;color:var(--sub)">'+time+' · 来源:'+(p.source==='selfcheck'?'每日体检':'参谋对话')+'</span></div>'+
    '<div class="p-content" id="prop-content-'+p.id+'">'+esc(p.content)+'</div>'+
    (p.reason?'<div class="p-reason">原因：'+esc(p.reason)+'</div>':'')+
    actions+'</div>';
}
async function loadProposals(){
  const r = await api('/api/proposals');
  const props = r.proposals||[];
  const pending = props.filter(p=>p.status==='pending');
  const done = props.filter(p=>p.status!=='pending');
  $('pendingCount').textContent = pending.length;
  $('doneCount').textContent = done.length;
  const items = propTab==='pending' ? pending : done;
  const list=$('propsList');
  if(!items.length){
    list.innerHTML = propTab==='pending'
      ? '<div class="chat-tip" style="padding:14px 4px;">🎉 待处理提案已清零。点「立即体检」或跟 AI 参谋聊聊，它会帮你发现新问题。</div>'
      : '<div class="chat-tip" style="padding:14px 4px;">还没有已处理的提案记录。</div>';
    return;
  }
  list.innerHTML = items.map(propCard).join('');
  list.querySelectorAll('button[data-act]').forEach(btn=>{
    btn.onclick = ()=>handlePropAction(btn.dataset.act, btn.dataset.id, btn);
  });
}
document.querySelectorAll('.prop-tab').forEach(t=>{
  t.onclick = ()=>{
    propTab = t.dataset.tab;
    document.querySelectorAll('.prop-tab').forEach(x=>x.classList.toggle('active', x===t));
    loadProposals();
  };
});
async function handlePropAction(act, id, btn){
  if(act==='approve'){
    btn.disabled=true;
    const r = await api('/api/proposal/approve','POST',{id});
    toast(r.msg||'已处理'); loadProposals(); loadPanel();
  } else if(act==='reject'){
    btn.disabled=true;
    const r = await api('/api/proposal/reject','POST',{id});
    toast(r.msg||'已拒绝'); loadProposals();
  } else if(act==='resolve'){
    btn.disabled=true;
    const r = await api('/api/proposal/resolve','POST',{id});
    toast(r.msg||'已解决'); loadProposals();
  } else if(act==='chat'){
    const p = document.getElementById('prop-content-'+id);
    $('chatInput').value = '关于「'+(p?p.textContent:'这条问询')+'」，我来补充：';
    $('chatInput').focus();
    document.getElementById('chatbox').scrollIntoView({behavior:'smooth'});
    toast('在下方聊天里告诉参谋，看它怎么追问');
  } else if(act==='edit'){
    const box = document.getElementById('prop-content-'+id);
    const orig = box.textContent;
    box.innerHTML = '<textarea class="editor" id="edit-ta-'+id+'" style="min-height:60px;">'+esc(orig)+'</textarea>'+
      '<div class="p-actions"><button class="mini" data-confirm="'+id+'">✓ 确认写入</button>'+
      '<button class="mini" data-cancel="'+id+'">取消</button></div>';
    box.querySelector('[data-confirm="'+id+'"]').onclick = async ()=>{
      const content = document.getElementById('edit-ta-'+id).value;
      const r = await api('/api/proposal/approve','POST',{id, content});
      toast(r.msg||'已写入'); loadProposals(); loadPanel();
    };
    box.querySelector('[data-cancel="'+id+'"]').onclick = ()=>loadProposals();
  }
}
$('btnSelfCheck').onclick = async ()=>{
  const b=$('btnSelfCheck'); b.disabled=true; b.textContent='体检中（约10秒）…';
  const r = await api('/api/self_check','POST');
  toast(r.msg||'体检完成');
  b.disabled=false; b.textContent='🩺 立即体检';
  loadProposals();
};
$('btnRefreshProps').onclick = loadProposals;
// ---- 定时任务 ----
const S_ACT = {message:'💬 消息', pause:'⏸ 暂停', resume:'▶ 继续'};
async function loadSched(){
  const r = await api('/api/schedule');
  const list=$('schedList');
  if(!r.jobs || !r.jobs.length){ list.innerHTML='<div class="chat-tip">暂无定时任务。加一个早安消息，或晚上定时暂停防止聊脱。</div>'; return; }
  list.innerHTML = r.jobs.map(j=>{
    const on = j.enabled;
    const actCls = j.action==='pause' ? 'p-q' : (j.action==='message' ? 'p-mem' : 'p-rel');
    return '<div class="prop">'+
      '<div class="p-head"><span class="p-badge '+(on?'p-adv':'p-done')+'">'+(on?'● 启用':'○ 停用')+'</span>'+
      '<span class="p-badge p-rel" style="font-size:13px;">'+esc(j.time)+'</span>'+
      '<span class="p-badge '+actCls+'">'+(S_ACT[j.action]||esc(j.action))+'</span>'+
      '<span style="font-size:11px;color:var(--sub)">'+(j.last_run?'今天已执行':'今天未执行')+'</span></div>'+
      (j.text?'<div class="p-content">'+esc(j.text)+'</div>':'')+
      '<div class="p-actions"><button class="mini" data-sact="toggle" data-id="'+j.id+'">'+(on?'停用':'启用')+'</button>'+
      '<button class="mini" data-sact="del" data-id="'+j.id+'">删除</button></div></div>';
  }).join('');
  list.querySelectorAll('button[data-sact]').forEach(b=>{
    b.onclick = async ()=>{
      const p = b.dataset.sact==='toggle' ? '/api/schedule/toggle' : '/api/schedule/delete';
      const r = await api(p,'POST',{id:b.dataset.id});
      toast(r.msg||'已处理'); loadSched();
    };
  });
}
$('btnSchedAdd').onclick = async ()=>{
  const r = await api('/api/schedule/add','POST',{time:$('schedTime').value, action:$('schedAction').value, text:$('schedText').value});
  toast(r.msg||'已添加');
  if(r.ok){ $('schedText').value=''; loadSched(); }
};
loadSched();
loadProposals();
loadPanel();
loadDaily();
refresh();
// 轮询由 setPoll() 统一管理（见上方），这里不再重复 setInterval

// ---- 2.0：退出面板（服务端会优雅关闭 HTTP 服务并清理实例锁）----
if($('btnShutdown')){
  $('btnShutdown').onclick = async ()=>{
    if(!confirm('退出控制面板？机器人进程不受影响，可随时用桌面快捷方式重新打开。')) return;
    const b=$('btnShutdown'); b.disabled=true; b.textContent='正在退出…';
    await api('/api/shutdown','POST',{confirm:true});
    document.body.innerHTML =
      '<div style="padding:60px;text-align:center;color:#8b9bb4;font-family:sans-serif">'+
      '控制面板已退出。<br><br>机器人仍在后台运行。<br>'+
      '重新打开：双击桌面「微信AI陪聊」快捷方式。</div>';
  };
}
// ---- 2.0：显示服务端版本号 ----
(async ()=>{
  try{
    const r = await fetch('/api/identity');
    const j = await r.json();
    if(j && j.version && $('verBadge')) $('verBadge').textContent = 'v' + j.version;
  }catch(e){ /* 忽略 */ }
})();
