const $ = (id) => document.getElementById(id);
const {node, money, renderMoney} = window.NexusUI;
const states = {ACTIVE:'正常',TEMP_LOCKED:'已锁定',LOST:'已挂失',CLOSED:'已关闭',CANCELLED:'已取消',REVOKED:'已撤销',TERMINATED:'已终止',COMPLETED:'已完成',REVERSED:'已撤销',CONFIRMED:'已确认',DRAFT:'草稿',PAUSED:'已暂停',SUPERSEDED:'已替代',SIMULATED_ORDERED:'模拟预订完成',NEEDS_ATTENTION:'需处理'};
const auditLabels = {CREATE_TRANSFER_DRAFT:'生成转账草稿',CONFIRM_TRANSFER:'用户确认转账',DEBIT_BALANCE:'付款账户扣款',CREDIT_BALANCE:'收款账户入账',SUBMIT_TRANSFER:'提交模拟转账',COMPLETE_TRANSFER:'转账清算完成',AUTHORIZE_SCHEDULED_EXECUTION:'授权定时执行',SCHEDULED_TRANSFER_COMPLETED:'定时转账完成',SCHEDULED_TRANSFER_FAILED:'定时转账暂停',PAUSE_SCHEDULED_TRANSFER:'暂停定时转账',SUPERSEDE_CONFIRMATION:'旧确认已失效',BIRTHDAY_ORDER_SIMULATED:'生日模拟预订完成',AUTHORIZE_BIRTHDAY_SIMULATION:'授权生日模拟预订',CREATE_SCHEDULED_TRANSFER:'创建定时转账计划',CREATE_AA_COLLECTION:'创建AA收款',CREATE_BIRTHDAY_PLAN:'创建生日计划',APPLY_CARD:'提交办卡申请',LOCK_CARD:'卡片临时锁定',UNLOCK_CARD:'卡片解锁',REPORT_LOST:'卡片挂失登记',SET_CARD_LIMIT:'卡片限额调整',SUBSCRIBE_PRODUCT:'模拟产品申购',REDEEM_PRODUCT:'模拟产品赎回',CANCEL_SUBSCRIPTION:'订阅合同终止',REVOKE_MANDATE:'代扣授权撤销',CONFIRM_SUBSCRIPTION:'订阅初始化',REVERSE_TRANSFER:'转账撤销'};
let busy = false;
let retryMessage = null;
const seenActions = new Map();
const actionVersions = new Map();
// Action IDs contain no account details. Persist dismissal across page reloads.
const CLEARED_CONVERSATION_KEY = 'nexus.cleared-conversation.v1';
function loadClearedActionIds() {
  try {
    const ids = JSON.parse(localStorage.getItem(CLEARED_CONVERSATION_KEY) || '[]');
    return new Set(Array.isArray(ids) ? ids.filter(id => typeof id === 'string') : []);
  } catch { return new Set(); }
}
const clearedActionIds = loadClearedActionIds();
function persistClearedActionIds() {
  localStorage.setItem(CLEARED_CONVERSATION_KEY, JSON.stringify([...clearedActionIds]));
}

function setAgentState(label, mode='ready') {
  const state=$('agent-state'); if (state) state.textContent=label;
  document.body.classList.remove('agent-thinking','agent-confirm','agent-executing');
  if (mode !== 'ready') document.body.classList.add(`agent-${mode}`);
  const phases=document.querySelectorAll('.agent-pipeline li');
  phases.forEach((item,index)=>item.classList.toggle('ready',mode==='thinking'?index<2:mode==='confirm'?index<4:mode==='executing'?true:index===0));
}

// Thinking bubble rendered inside the conversation stream.
// Rotates through three stage labels so the user sees the agent "working".
const THINKING_STAGES = [
  '正在理解你的请求',
  '正在选择所需工具',
  '正在核验数据并组织结果',
];
let thinkingBubble = null;
let thinkingTimer = null;
let thinkingStageIndex = 0;

function showThinkingBubble() {
  if (thinkingBubble) return;
  thinkingBubble = node('article', 'assistant-message thinking-message');
  const avatar = node('span', 'agent-avatar thinking-avatar', 'N');
  const content = node('div', 'thinking-content');
  const dots = node('div', 'thinking-dots');
  dots.append(node('i'), node('i'), node('i'));
  content.append(
    node('span', 'speaker', 'NEXUS · AI 银行管家'),
    node('div', 'thinking-text', THINKING_STAGES[0]),
    dots,
  );
  thinkingBubble.append(avatar, content);
  $('messages').append(thinkingBubble);
  scrollMessages();
  thinkingStageIndex = 0;
  thinkingTimer = setInterval(() => {
    if (!thinkingBubble) return;
    thinkingStageIndex = (thinkingStageIndex + 1) % THINKING_STAGES.length;
    const text = thinkingBubble.querySelector('.thinking-text');
    if (!text) return;
    // 直接改 textContent 不会触发 opacity 过渡（同一帧内改完就没得过渡了），
    // 所以先淡出、下一帧再换字淡入，头像的脉动因此和文案变化读起来是同一个动作。
    text.classList.add('swapping');
    setTimeout(() => {
      if (!text.isConnected) return;
      text.textContent = THINKING_STAGES[thinkingStageIndex];
      text.classList.remove('swapping');
    }, 180);
  }, 1800);
}

function hideThinkingBubble() {
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
  if (thinkingBubble && thinkingBubble.parentNode) {
    thinkingBubble.parentNode.removeChild(thinkingBubble);
  }
  thinkingBubble = null;
}

// Confirmation and receipt payloads arrive as "label: value" lines so the user
// can verify each field separately. A run-on sentence would defeat the point of
// asking for confirmation, so multi-field details render as a field table.
function renderFieldList(detail) {
  const rows = String(detail||'').split('\n').map(r=>r.trim()).filter(Boolean);
  const pairs = rows
    .map(row=>{const i=row.indexOf('：');return i<0?null:{key:row.slice(0,i).trim(),value:row.slice(i+1).trim()};})
    .filter(Boolean);
  if (pairs.length < 2) return node('p','',detail);
  const list = node('dl','confirm-fields');
  pairs.forEach(({key,value})=>list.append(node('dt','',key),node('dd','',value)));
  return list;
}
const RECURRENCE_LABELS=[['once','仅此一次'],['weekly','每周'],['monthly','每月'],['quarterly','每季'],['yearly','每年']];
// 确认卡在钱动之前必须是可以改的表。日期被读错、金额多打一个零，客户在
// 确认卡上改掉，比事后申诉便宜一百倍。收款人不给改——那是从原话核出来的，
// 改收款人等于把「我确认」变成「我确认了另一笔」。
function actionEditor(answer, onSaved) {
  const terms = answer.terms || {};
  const fields = answer.editable || [];
  const form = node('form','action-edit');
  const row = (label, control, hint) => {
    const line = node('label','action-edit-row');
    line.append(node('span','action-edit-label',label), control);
    if (hint) line.append(node('small','action-edit-hint',hint));
    form.append(line); return control;
  };
  const amount = node('input','action-edit-input');
  amount.type = 'text'; amount.inputMode = 'decimal';
  amount.value = money(terms.amount || '');
  amount.dataset.editField = 'amount';
  row('金额', amount, '可改成你能承受的数；差额我会在确认前再核一次');

  const purpose = node('input','action-edit-input');
  purpose.type = 'text'; purpose.maxLength = 100;
  purpose.value = terms.purpose || '';
  purpose.dataset.editField = 'purpose';
  row('用途', purpose, '会打印在回执上');

  if (fields.includes('run_date')) {
    const date = node('input','action-edit-input');
    date.type = 'date';
    date.value = terms.first_run_on || '';
    date.dataset.editField = 'run_date';
    row('执行日期', date, '这一笔什么时候扣');

    const cadence = node('select','action-edit-input');
    RECURRENCE_LABELS.forEach(([value,label])=>{
      const option = node('option','',label); option.value = value;
      if ((terms.frequency || 'once').toLowerCase() === value) option.selected = true;
      cadence.append(option);
    });
    cadence.dataset.editField = 'recurrence';
    const times = node('input','action-edit-input action-edit-narrow');
    times.type = 'number'; times.min = '1'; times.max = '60';
    times.placeholder = '长期';
    if (terms.occurrences) times.value = String(terms.occurrences);
    times.dataset.editField = 'occurrences';
    row('怎么重复', cadence, '只有你确实要长期扣的时候才选「每月」等；选「仅此一次」就只扣这一笔');
    row('共几期', times, '留空表示长期有效、随时可暂停；填了数字就会在第 N 期后自动结束');
  }

  const actions = node('div','action-edit-actions');
  const error = node('p','action-edit-error');
  const save = button('保存修改', async () => {
    if (busy) return;
    const body = {amount: amount.value.trim()};
    if (purpose.value.trim()) body.purpose = purpose.value.trim();
    if (form.querySelector('[data-edit-field="run_date"]')) {
      const runDate = form.querySelector('[data-edit-field="run_date"]').value;
      if (runDate) body.run_date = runDate;
      body.recurrence = cadence.value;
      const raw = times.value.trim();
      if (raw) body.occurrences = Number(raw);
      else if (cadence.value === 'once') body.occurrences = 1;
    }
    setBusy(true); error.textContent = '';
    try {
      onSaved(await api(`/actions/${answer.action_id}`, body, 'PATCH'));
    } catch (problem) {
      // Stay open on purpose. The customer just told us what they want; closing
      // the editor on a validation error throws that answer away and makes them
      // retype it.
      error.textContent = problem.message;
    } finally { setBusy(false); }
  }, 'primary');
  actions.append(save, button('不改了', () => form.closest('.action-edit-wrap')?.remove()));
  form.append(error, actions);
  const wrap = node('div','action-edit-wrap');
  wrap.append(form);
  return wrap;
}
function moneyFieldList(answer) {
  const list = renderFieldList(answer.detail);
  if (!(answer.editable || []).length) return list;
  list.append(node('p','confirm-edit-hint', answer.edit_notice || ''));
  return list;
}
const busyButtons=new Map();
function setBusy(value) { busy=value; if(value){document.querySelectorAll('button').forEach(b=>{busyButtons.set(b,b.disabled);b.disabled=true;});}else{busyButtons.forEach((disabled,b)=>{if(b.isConnected)b.disabled=disabled;});busyButtons.clear();} }
function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
// 倒计时挂在确认卡上，而不是写一句"5 分钟内有效"。写操作有真实的时间窗，
// 看不见的窗口只会让人在过期后才发现自己白等。
const ACTION_TTL_SECONDS = 300;
function countdownBar(onExpire, expiresAt) {
  const wrap = node('div','action-ttl');
  const track = node('div','action-ttl-track');
  const fill = node('i','action-ttl-fill');
  // Count down to the moment the *server* will stop honouring this action, not
  // to a fresh five minutes from whenever the card happened to be drawn. A card
  // re-rendered after a failed step-up used to restart the clock and promise
  // time the server was not going to give.
  const total = expiresAt
    ? Math.max(1, Math.round((new Date(expiresAt).getTime() - Date.now()) / 1000))
    : ACTION_TTL_SECONDS;
  const text = node('span','action-ttl-text', `${Math.round(total/60)} 分钟内有效`);
  track.append(fill); wrap.append(track, text);
  const started = Date.now();
  const timer = setInterval(() => {
    const left = Math.max(0, total - Math.floor((Date.now()-started)/1000));
    fill.style.width = `${(left/total)*100}%`;
    text.textContent = left > 0 ? `剩余 ${Math.floor(left/60)} 分 ${left%60} 秒 · 过期后需重新发起` : '已过期，请重新发起这笔操作';
    wrap.classList.toggle('urgent', left > 0 && left <= 60);
    if (left === 0) { clearInterval(timer); onExpire(); }
  }, 1000);
  return {wrap, stop: () => clearInterval(timer)};
}
function failureCard(message, onRetry) {
  // 失败不该只留一行红字。给出发生了什么、钱有没有动、以及下一步。
  const block = node('article','failure-card');
  block.append(node('span','speaker','NEXUS · 请求未完成'), node('h3','',message));
  block.append(node('p','failure-body','这次请求没有完成。没有创建任何操作，也没有移动任何资金。'));
  if (onRetry) {
    block.append(button('重试一次', onRetry, 'primary'), button('换个问法', () => { showError(''); $('message').focus(); }));
  } else {
    block.append(button('重新输入', () => { showError(''); $('message').focus(); }));
  }
  return block;
}
async function api(path, body, method) {
  // An omitted body used to mean GET, which silently turned the step-up card's
  // 取消 into a GET against a POST-only route: 405, a one-line red error at the
  // top of a scrolled page, and no way for the customer to tell that their click
  // had done nothing. The method is now explicit at every call site.
  const verb = method || (body === undefined ? 'GET' : 'POST');
  let response;
  try {
    response = await fetch(`/api${path}`, {method:verb, headers:{'Content-Type':'application/json','X-Nexus-Demo':'1'}, body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(path==='/messages'||path==='/financial-profile'?120000:15000)});
  } catch(error) {
    throw new Error(error.name==='TimeoutError'?'服务响应超时，请检查本地服务并重试。':'无法连接本地服务，请确认 Nexus 已启动。');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('本地服务响应异常，请确认服务仍在运行。'); }
  if (!response.ok) {
    const failure = new Error(data.message || (typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入后重试。'));
    // Carry the business code and its structured detail. "余额不足，还差 ¥2,950.00"
    // and a bare "余额不足" are the same event, but only one of them lets the
    // card offer the number that would actually work.
    failure.code = data.code || '';
    failure.extra = data.extra && typeof data.extra === 'object' ? data.extra : {};
    // 会话在服务端没了（过期、被登出、服务重启后轮换）。任何一次 401 都要把
    // 用户送回登录，而不是让每个卡片各自弹一次"会话已过期"。
    if (response.status === 401 && data.code === 'UNAUTHENTICATED') requireLogin();
    throw failure;
  }
  return data;
}
let loginGateShown=false;
function requireLogin() {
  if (loginGateShown) return;
  loginGateShown=true;
  accountReady=false;
  showError('');
  const badge=$('model-badge'); if(badge) badge.textContent='需要登录';
  setAgentState('需要登录');
  // 整页跳走，不是弹个浮层盖住正在看的界面。
  goToLogin();
}
function button(text, callback, cls='') {
  const b = node('button', cls, text); b.type='button'; b.addEventListener('click',event=>{if(!busy)callback(event);}); return b;
}
function scrollMessages() { $('messages').scrollTop = $('messages').scrollHeight; }
function analysisSection(title) {
  const section=node('section','analysis-section'); section.append(node('h4','',title)); return section;
}
// 卡片也必须署名。这些卡片左边本来就留了 44px 的头像位，只是没画头像，
// 结果确认卡和边界卡看上去像系统弹窗而不是 AI 说的话。
function agentAvatar() { return node('span','agent-avatar card-avatar','N'); }
// 纯拒绝类回答：正文已经把"为什么不行"讲完了，再挂一个安全检查标签和一串
// 依据，只是重复用户已经读过的话。真正做了事或需要用户接手的（转人工、
// 中断、风险隔离）不在此列——那些的依据必须留着。
const QUIET_REFUSALS = new Set(['out_of_scope','unsupported_financial','text_confirmation']);
function svgNode(tag, attrs={}, textValue) {
  const el=document.createElementNS('http://www.w3.org/2000/svg',tag);
  Object.entries(attrs).forEach(([key,value])=>el.setAttribute(key,String(value)));
  if (textValue !== undefined) el.textContent=textValue;
  return el;
}
function lineChart(series, labels, ariaLabel, unit='', partialIndex=null) {
  const width=620,height=190,padX=56,padY=22,plotW=width-padX*2,plotH=height-padY*2;
  const values=series.flatMap(item=>item.values.filter(value=>value!=null).map(Number).filter(Number.isFinite));
  const min=Math.min(0,...values),max=Math.max(1,...values),span=max-min || 1;
  const figure=node('figure','data-chart');
  const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':ariaLabel});
  for(let i=0;i<4;i++){const y=padY+(plotH/3)*i;svg.append(svgNode('line',{x1:padX,y1:y,x2:width-padX,y2:y,class:'chart-grid'}));}
  svg.append(svgNode('text',{x:padX-6,y:padY+3,class:'chart-axis','text-anchor':'end'},axisValue(max,unit)));
  svg.append(svgNode('text',{x:padX-6,y:padY+plotH+3,class:'chart-axis','text-anchor':'end'},axisValue(min,unit)));
  const at=(index)=>padX+(plotW*(index/(Math.max(labels.length-1,1))));
  series.forEach((item,sIndex)=>{
    const points=item.values.map((value,index)=>{
      if(value==null||!Number.isFinite(Number(value)))return null;
      const y=padY+plotH-((Number(value)-min)/span*plotH);return `${at(index)},${y}`;
    });
    // A still-running period is drawn as a dashed tail with an open marker. A
    // solid segment there would claim the month finished, and the eye would
    // read three days of spending as a collapse.
    const cut=Number.isInteger(partialIndex)&&partialIndex>0&&partialIndex<points.length?partialIndex:points.length;
    let run=[];const flush=()=>{if(run.length)svg.append(svgNode('polyline',{points:run.join(' '),class:`chart-line series-${sIndex}`}));run=[];};
    points.slice(0,cut).forEach(point=>{if(point)run.push(point);else flush();});flush();
    if(cut<points.length&&points[cut-1]&&points[cut]) svg.append(svgNode('polyline',{points:points.slice(cut-1).join(' '),class:`chart-line chart-partial series-${sIndex}`}));
    points.forEach((point,index)=>{
      if(!point)return;
      const [cx,cy]=point.split(',');
      const dot=svgNode('circle',{cx,cy,r:4,class:`chart-dot series-${sIndex}${index===points.length-1&&cut<points.length?' chart-partial':''}`});dot.append(svgNode('title',{},`${labels[index]} · ${item.name} ${formatValue(item.values[index],unit)}`));svg.append(dot);
    });
  });
  const every=labels.length>7?2:1;
  labels.forEach((label,index)=>{if(index%every===0||index===labels.length-1){svg.append(svgNode('text',{x:at(index),y:height-4,class:'chart-label','text-anchor':'middle'},`${label}${index===partialIndex?'*':''}`));}});
  const legend=node('figcaption','chart-legend');series.forEach((item,index)=>{const key=node('span',`series-${index}`);key.append(node('i'),document.createTextNode(item.name));legend.append(key);});
  figure.append(svg,legend,chartDataTable(series,labels,unit,partialIndex));return figure;
}
const CATEGORY_COLORS=['#35d8d0','#ffb454','#8eacff','#cba4ff','#84e59a','#ff99ac','#94a9bd'];
function categoryDonut(categories,totalLabel) {
  const wrap=node('div','donut-wrap'),svg=svgNode('svg',{viewBox:'0 0 180 180',role:'img','aria-label':'消费分类占比，完整金额见右侧分类列表'});
  svg.append(svgNode('circle',{cx:90,cy:90,r:68,class:'donut-track'}));
  const total=categories.reduce((sum,item)=>sum+Math.max(0,Number(item.amount_value)||0),0);let offset=0;
  categories.forEach((item,index)=>{
    const share=total>0?Math.max(0,Number(item.amount_value)||0)/total*100:0;
    const segment=svgNode('circle',{cx:90,cy:90,r:68,pathLength:100,'stroke-dasharray':`${share} ${100-share}`,'stroke-dashoffset':-offset,class:`donut-segment segment-${index%CATEGORY_COLORS.length}`});
    segment.append(svgNode('title',{},`${item.name} ${item.amount} · ${share.toFixed(1)}%`));svg.append(segment);offset+=share;
  });
  const center=node('div','donut-center');center.append(node('small','','本期支出'),node('strong','',totalLabel));wrap.append(svg,center);return wrap;
}
function chartDataTable(series,labels,unit,partialIndex) {
  const detail=node('details','chart-values');detail.append(node('summary','','查看图表数据'));
  const table=node('table','data-table'),caption=node('caption','','图表完整数值');table.append(caption);
  const head=node('tr');['期间',...series.map(item=>item.name)].forEach(text=>{const cell=node('th','',text);cell.scope='col';head.append(cell);});
  const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
  labels.forEach((label,index)=>{const row=node('tr');const key=node('th','',`${label}${index===partialIndex?'（未结束）':''}`);key.scope='row';row.append(key);series.forEach(item=>row.append(node('td','',item.values[index]==null?'暂无数据':formatValue(item.values[index],unit))));body.append(row);});
  table.append(body);detail.append(table);return detail;
}
function transactionExplorer(answer) {
  const section=analysisSection('消费明细'),filters=node('div','ledger-filters'),list=node('div','ledger-list'),status=node('p','ledger-status');status.setAttribute('role','status');
  let category='全部',limit=8;
  const rows=answer.transactions||[];
  const draw=()=>{
    const selected=rows.filter(item=>category==='全部'||item.category===category);list.replaceChildren();
    status.textContent=`${category==='全部'?'全部消费':category} · ${selected.length} 笔 · 合计 ¥${money(selected.reduce((sum,item)=>sum+item.amount_value,0))}`;
    filters.querySelectorAll('button').forEach(btn=>btn.setAttribute('aria-pressed',String(btn.textContent===category)));
    selected.slice(0,limit).forEach(item=>{const row=node('div','ledger-row'),copy=node('div');copy.append(node('strong','',item.merchant),node('small','',`${item.date} · ${item.category}${item.note?' · '+item.note:''}`));row.append(copy,node('b','',`−${item.amount}`));list.append(row);});
    if(!selected.length)list.append(node('p','analysis-method','这个分类暂无消费，选择其他分类查看。'));
    more.hidden=selected.length<=limit;more.textContent=`再显示 ${Math.min(20,selected.length-limit)} 笔`;
  };
  ['全部',...answer.categories.map(item=>item.name)].forEach(name=>{const btn=button(name,()=>{category=name;limit=8;draw();},'filter-chip');filters.append(btn);});
  const more=button('显示更多消费',()=>{limit+=20;draw();},'ledger-more');section.append(filters,status,list,more);draw();return section;
}
const formatValue = (value, unit='') => unit==='%'?`${Number(value).toFixed(2)}%`:unit==='¥'?`¥${money(value)}`:String(value);
// Axis ticks stay short: a curve is read by its shape, and a full currency
// string on the left margin would eat the plot it is labelling.
const axisValue = (value, unit='') => {
  const number=Number(value);
  if(unit==='%') return `${number.toFixed(number%1?2:0)}%`;
  if(unit==='¥') return `¥${Math.abs(number)>=10000?`${(number/10000).toFixed(1)}万`:Math.round(number).toLocaleString('zh-CN')}`;
  return String(Math.round(number*100)/100);
};
function barChart(series, labels, ariaLabel, unit='', partialIndex=null) {
  const width=620,height=200,padX=56,padY=26,padB=22,plotW=width-padX*2,plotH=height-padY-padB;
  const values=series.flatMap(item=>item.values.map(Number).filter(Number.isFinite));
  if(!values.length) return null;
  // A comparison bar is allowed to go below zero: that is exactly how a net
  // position shows that the month came out negative.
  const min=Math.min(0,...values),max=Math.max(0,...values),span=(max-min)||1;
  const zeroY=padY+plotH-((0-min)/span*plotH);
  const figure=node('figure','data-chart');
  const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':ariaLabel});
  for(let i=0;i<4;i++){const y=padY+(plotH/3)*i;svg.append(svgNode('line',{x1:padX,y1:y,x2:width-padX,y2:y,class:'chart-grid'}));}
  svg.append(svgNode('text',{x:padX-6,y:padY+3,class:'chart-axis','text-anchor':'end'},axisValue(max,unit)));
  svg.append(svgNode('text',{x:padX-6,y:padY+plotH+3,class:'chart-axis','text-anchor':'end'},axisValue(min,unit)));
  if(min<0) svg.append(svgNode('line',{x1:padX,y1:zeroY,x2:width-padX,y2:zeroY,class:'chart-zero'}));
  const slot=plotW/labels.length,barW=Math.max(Math.min(slot*0.68,54)/series.length,4);
  series.forEach((item,sIndex)=>{
    item.values.forEach((value,index)=>{
      if(value===null||value===undefined||!Number.isFinite(Number(value))) return;
      const x=padX+slot*index+(slot-barW*series.length)/2+barW*sIndex;
      const y=padY+plotH-((Number(value)-min)/span*plotH);
      const rect=svgNode('rect',{x,y:Math.min(y,zeroY),width:barW,height:Math.max(Math.abs(zeroY-y),1.5),rx:2,class:`chart-bar series-${sIndex}${Number(value)<0?' negative':''}${index===partialIndex?' chart-partial':''}`});
      rect.append(svgNode('title',{},`${labels[index]} · ${item.name} ${formatValue(value, unit||'')}`));
      svg.append(rect);
    });
  });
  labels.forEach((label,index)=>{const x=padX+slot*(index+0.5);svg.append(svgNode('text',{x,y:height-4,class:'chart-label','text-anchor':'middle'},`${label}${index===partialIndex?'*':''}`));});
  const legend=node('figcaption','chart-legend');series.forEach((item,index)=>{const key=node('span',`series-${index}`);key.append(node('i'),document.createTextNode(item.name));legend.append(key);});
  figure.append(svg,legend,chartDataTable(series,labels,unit,partialIndex));return figure;
}
// Single entry point for every chart the backend sends. The backend already
// decided line-versus-bar from the shape of the data, so this only has to draw.
function renderChart(chart) {
  if(!chart||!chart.labels?.length||!chart.series?.length) return null;
  const series=chart.series.filter(item=>item.values?.some(value=>value!==null&&value!==undefined));
  if(!series.length) return null;
  const unit=chart.unit||'';
  const partial=Number.isInteger(chart.partial_index)?chart.partial_index:null;
  const plot=chart.kind==='compare'
    ? barChart(series,chart.labels,chart.title||'数据对比图',unit,partial)
    : lineChart(series,chart.labels,chart.title||'数据走势图',unit,partial);
  if(!plot) return null;
  const wrap=node('figure','chart-block');
  wrap.append(node('figcaption','chart-caption',chart.title||''));
  wrap.append(plot);
  if(chart.insight) wrap.append(node('p','chart-insight',chart.insight));
  if(chart.note) wrap.append(node('p','chart-note',chart.note));
  if(unit) wrap.dataset.unit=unit;
  return wrap;
}
// 五星评分：这条回答的"落地感"由你判定。高分会被存下来，后面打磨时当基准。
function ratingRow(requestId) {
  if (!requestId) return null;
  const row = node('div','rating-row');
  row.append(node('span','rating-label','评价回答'));
  const stars = node('div','rating-stars');
  const saved = readRating(requestId);
  for (let value = 1; value <= 5; value++) {
    const star = node('button', `rating-star${saved >= value ? ' on' : ''}`, value <= saved ? '★' : '☆');
    star.type = 'button';
    star.title = `${value} 星`;
    star.addEventListener('click', async () => {
      setBusy(true); showError('');
      try {
        await api('/ratings', {request_id: requestId, stars: value});
        storeRating(requestId, value);
        stars.querySelectorAll('.rating-star').forEach((el, index) => {
          el.textContent = index + 1 <= value ? '★' : '☆';
          el.classList.toggle('on', index + 1 <= value);
        });
        note.textContent = '已记录';
      } catch (error) { showError(error.message); } finally { setBusy(false); }
    });
    stars.append(star);
  }
  const note = node('small','rating-note', saved ? `已记录 ${saved} 星` : '');
  row.append(stars, note);
  return row;
}
const RATING_KEY = 'nexus.ratings';
function readRatings() { try { return JSON.parse(localStorage.getItem(RATING_KEY)) || {}; } catch { return {}; } }
function readRating(id) { return readRatings()[id] || 0; }
function storeRating(id, value) {
  const all = readRatings(); all[id] = value;
  try { localStorage.setItem(RATING_KEY, JSON.stringify(all)); } catch { /* private mode: the server still has it */ }
}
// 每一步的调用过程不再挂在回答下方。"依据 · N 步"是把自己怎么干活摊开给客户看，
// 真实银行不会这么讲话。同一份可追溯性没有丢：它落在右栏的「操作记录」里，
// 那里是本来就应该查账的地方；每张回执和确认卡也各自写明了对象、金额与影响。
function renderExternalData(answer) {
  const block=node('article','external-card');
  block.append(agentAvatar());
  const head=node('div','external-head'), source=node('span','live-source','● LIVE EXTERNAL DATA');
  head.append(source,node('span','analysis-date',`数据日期 ${answer.as_of}`));
  const rate=node('div','fx-rate');rate.append(node('span','',`${answer.base} → ${answer.quote}`),node('strong','',answer.rate));
  const conversion=node('div','fx-conversion');conversion.append(node('small','','本地换算结果'),node('h3','',answer.summary));
  const provenance=node('p','external-provenance',`来源：${answer.source.name} · 查询仅包含 ${answer.base}/${answer.quote} 币种对`);
  const link=node('a','source-link','查看数据源 ↗'); link.href=answer.source.url;link.target='_blank';link.rel='noopener noreferrer';
  block.append(head,node('h2','',answer.title),rate,conversion,provenance,link);
  return block;
}
function cacheLabel(value) { return value==='live'?'实时获取':value==='fresh'?'本地缓存':value==='stale'?'过期缓存':'已核验'; }
function renderMacroData(answer) {
  const block=node('article','external-card macro-card');
  block.append(agentAvatar());
  const head=node('div','external-head');head.append(node('span','live-source','● OFFICIAL MACRO DATA'),node('span','analysis-date',`${cacheLabel(answer.cache)} · 更新 ${answer.updated}`));
  const metric=node('div','macro-metric');metric.append(node('strong','',answer.value),node('span','',answer.period));
  const provenance=node('p','external-provenance',`指标：${answer.indicator_code} · 来源：${answer.source.name}`);
  const link=node('a','source-link','查看世界银行来源 ↗');link.href=answer.source.url;link.target='_blank';link.rel='noopener noreferrer';
  block.append(head,node('h2','',answer.title),metric,provenance,link);
  return block;
}
function renderSecFilings(answer) {
  const block=node('article','external-card sec-card');
  block.append(agentAvatar());
  const head=node('div','external-head');head.append(node('span','live-source','● OFFICIAL COMPANY FILINGS'),node('span','analysis-date',`${cacheLabel(answer.cache)} · ${answer.ticker}`));
  block.append(head,node('h2','',answer.title));
  const list=node('div','filing-list');
  answer.filings.forEach(item=>{
    const link=node('a','filing-row');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';
    const left=node('div');left.append(node('b','',item.form),node('small','',`报告期 ${item.report_date || '未标注'} · ${item.accession}`));
    link.append(left,node('span','',`${item.filed}  ↗`));list.append(link);
  });
  block.append(list,node('p','external-provenance',`来源：${answer.source.name} · 核验时间 ${answer.checked_at.replace('T',' ').replace('+00:00',' UTC')}`));
  return block;
}
function renderAccountSnapshot(answer) {
  const block=node('article','snapshot-card');
  block.append(agentAvatar());
  block.append(node('span','speaker','NEXUS / VERIFIED ACCOUNT TOOLS'));
  // The answer first, at a size you can read across the room. Everything the
  // customer did not ask about is still there — just folded underneath.
  const hero=answer.hero||{label:answer.title,value:'',note:answer.summary,aside:[]};
  const head=node('div','snapshot-hero');
  head.append(node('span','snapshot-hero-label',hero.label),node('strong','snapshot-hero-value',hero.value||'—'));
  if(hero.note) head.append(node('small','snapshot-hero-note',hero.note));
  if((hero.aside||[]).length){
    const aside=node('div','snapshot-aside');
    hero.aside.forEach(item=>aside.append(node('span','',item.label),node('b',item.tone?`stat-${item.tone}`:'',item.value)));
    head.append(aside);
  }
  block.append(head);

  const sections=[];
  // A comparison the model asked for sits right under the headline: it is the
  // answer to "够不够", not a footnote under a balance.
  if(answer.comparison){
    const cmp=node('div',`action-compare ${answer.comparison.affordable?'ok':'short'}`);
    cmp.append(node('span','action-compare-label',answer.comparison.label),
      node('p','action-compare-verdict',answer.comparison.verdict),
      node('small','action-compare-rate',`参考汇率 ${answer.comparison.rate}${answer.comparison.as_of?' · 汇率日期 '+answer.comparison.as_of:''} · ${(answer.comparison.source||{}).name||'公开数据'}`));
    sections.push(['comparison','换汇测算',cmp]);
  }
  if(answer.cards.length){
    const rows=answer.cards.map(item=>{const row=node('div','snapshot-row');
      row.append(node('span','',`${item.name} · •••• ${item.last4}`),
        node('b','snapshot-row-status',states[item.status]||item.status),
        node('small','',`单笔 ${item.single_limit} / 每日 ${item.daily_limit}`));
      return row;});
    sections.push(['cards','卡片',rows]);
  }
  if(answer.subscriptions.length){
    const rows=answer.subscriptions.map(item=>{const row=node('div','snapshot-row');
      row.append(node('span','',item.merchant),
        node('b','snapshot-row-status',item.amount),
        node('small','',`合同${states[item.contract]||item.contract} · 代扣${states[item.mandate]||item.mandate}`));
      return row;});
    sections.push(['subscriptions','订阅与代扣',rows]);
  }
  if(answer.transactions.length){
    const rows=answer.transactions.map(item=>{const row=node('div','snapshot-row');
      row.append(node('span','',item.direction),node('b','snapshot-row-status',item.amount),node('small','',item.status));
      return row;});
    sections.push(['transactions','最近流水',rows]);
  }
  const slots=[{name:'hero',label:'概览',el:head}];
  sections.forEach(([key,title,rows])=>{
    const section=analysisSection(title); rows.forEach(row=>section.append(row));
    slots.push({name:key,label:title,el:section});
  });
  return compose(block, answer, slots);
}
function renderCardBalances(answer) {
  const block=node('article','analysis-card card-balances-card');
  block.append(agentAvatar(),node('span','speaker','NEXUS · AI 银行管家'),node('h3','',answer.title));
  const cards=node('div','card-balance-grid');
  for(const item of answer.cards){
    const card=node('section','card-balance-item');
    card.append(node('h4','',item.name),node('span','card-balance-tail',`尾号 ${item.last4} · ${states[item.status]||item.status}`),node('small','card-balance-label','关联账户可用余额'),node('strong','card-balance-amount',item.available));
    if(item.shared_account)card.append(node('span','card-balance-shared','与其他卡共用账户'));
    card.append(node('p','card-balance-limits',`单笔限额 ${item.single_limit} · 每日限额 ${item.daily_limit}`));cards.append(card);
  }
  block.append(cards,node('p','card-balance-note',answer.summary));return block;
}
function renderProductCatalog(answer) {
  const block=node('article','product-card');
  block.append(agentAvatar());
  const head=node('div','product-head');head.append(node('span','speaker','NEXUS / LOCAL PRODUCT TOOL'),node('b','risk-pill',`风险等级 ${answer.risk_score}`));
  block.append(head,node('h3','',answer.title));
  const chart=renderChart(answer.chart); if(chart) block.append(chart);
  if (answer.products.length) {
    const grid=node('div','product-grid');
    answer.products.forEach(item=>{
      const card=node('section','product-item');
      const top=node('div','product-item-head');top.append(node('b','',item.code),node('span','',item.risk));
      // The badge answers the question the chart cannot: is it up or down *right
      // now*. Colour follows direction, and the number itself is always signed
      // so the colour is never the only signal.
      if(item.recent_change_pct){
        const arrow=item.direction==='up'?'▲':item.direction==='down'?'▼':'—';
        top.append(node('b',`move-pill move-${item.direction||'flat'}`,`${arrow} 近 7 日 ${item.recent_change_pct}`));
      }
      card.append(top,node('h4','',item.name));
      const stats=node('div','product-stats');
      stats.append(node('div','','参考年化'),node('strong','',item.reference_rate));
      if(item.window_change_pct) stats.append(node('div','','区间涨跌'),node('strong',`stat-${item.direction||'flat'}`,item.window_change_pct));
      stats.append(node('div','','锁定期'),node('strong','',`${item.lock_days} 天`),node('div','','起购'),node('strong','',item.minimum));
      card.append(stats,button('生成申购计划',()=>send(`申购 ${item.code} ${String(item.minimum).replace(/[^0-9.]/g,'')}元`),'product-action'));
      grid.append(card);
    });
    block.append(grid);
  }
  const orders=node('section','order-list');orders.append(node('h4','',`我的持仓 · ${answer.orders.length}`));
  if (!answer.orders.length) orders.append(node('p','muted','暂无投资订单。'));
  answer.orders.forEach(item=>{
    const row=node('div','order-row'), copy=node('div');
    copy.append(node('b','',`${item.product_code} · ${item.order_type==='SUBSCRIBE'?'申购':'赎回'} #${item.order_id}`),node('small','',`${states[item.status]||item.status} · 金额 ¥${money(item.amount)} · 剩余 ${item.remaining_shares ?? '—'} 份`));
    row.append(copy);
    if (item.order_type==='SUBSCRIBE' && Number(item.remaining_shares)>0) row.append(button('全部赎回',()=>send(`赎回订单${item.order_id}`)));
    orders.append(row);
  });
  block.append(orders);
  return block;
}
function renderRecurringDetection(answer) {
  const block=node('article','recurring-card');
  block.append(agentAvatar());
  const head=node('div');
  head.append(node('span','speaker','NEXUS / RECURRING DETECTOR'),node('h3','',answer.title),node('p','snapshot-summary',answer.summary));
  const slots=[{name:'head',label:'概览',el:head}];
  const list=node('div','recurring-list');
  if (!answer.items.length) list.append(node('p','muted','当前账单中没有达到识别阈值的规律扣费。'));
  answer.items.forEach(item=>{
    const row=node('section','recurring-row'), left=node('div'), right=node('div');
    left.append(node('b','',item.merchant_name),node('small','',`${item.occurrence_count} 次 · ${item.period==='MONTHLY'?'每月':item.period==='QUARTERLY'?'每季':'每年'} · 置信度 ${Math.round(item.confidence*100)}%`));
    right.append(node('strong','',`¥${money(item.amount)}`),node('small','',`预计下次 ${item.next_charge_estimate}`));
    row.append(left,right);list.append(row);
  });
  slots.push({name:'items',label:'识别到的扣费',el:list});
  return compose(block, answer, slots);
}
function renderScheduledTransfers(answer) {
  const block=node('article','analysis-card schedule-card');
  block.append(agentAvatar());
  block.append(node('span','speaker','NEXUS / SCHEDULE ORCHESTRATOR'),node('h3','',answer.title),node('p','analysis-summary',answer.summary));
  const timeline=node('div','schedule-timeline');
  if (!answer.plans.length) timeline.append(node('p','muted','还没有计划。可以说"每月5号给张三转账1000元备注房租"。'));
  answer.plans.forEach(item=>{const row=node('section','schedule-row');const date=node('time','');date.append(node('b','',String(item.day_of_month).padStart(2,'0')),node('small','','每月'));const copy=node('div');copy.append(node('strong','',`${item.recipient} · ¥${money(item.amount)}`),node('p','',item.purpose),node('small','',`下次模拟执行 ${item.next_run_at.slice(0,10)} 09:00`));row.append(date,copy,node('span',`badge ${item.status==='ACTIVE'?'':'inactive'}`,item.status==='ACTIVE'?'运行中':'已暂停'));timeline.append(row);});
  block.append(timeline);
  return block;
}
function renderBirthdayIntake(answer) {
  const block=node('article','analysis-card birthday-card');block.append(agentAvatar(),node('span','speaker','NEXUS / CROSS-SCENE PLANNER'),node('h3','',answer.title),node('p','analysis-summary',answer.message));
  const form=node('form','birthday-form'),dateInput=node('input'),budget=node('input');dateInput.type='date';dateInput.min=answer.min_date;dateInput.required=true;budget.type='number';budget.min='1';budget.step='0.01';budget.value=answer.budget;budget.required=true;
  const dateLabel=node('label');dateLabel.append(node('span','','生日日期'),dateInput);const budgetLabel=node('label');budgetLabel.append(node('span','','预算金额'),budget);const submit=node('button','primary','继续生成跨场景计划');submit.type='submit';form.append(dateLabel,budgetLabel,submit);
  form.addEventListener('submit',event=>{event.preventDefault();send(`请规划生日惊喜：生日日期${dateInput.value}，预算${budget.value}元`);block.remove();});block.append(form);return block;
}
function renderCrossScenePlan(answer) {
  const block=node('article','analysis-card cross-scene-card');const head=node('div','analysis-head'),title=node('div');title.append(node('span','speaker','NEXUS / MULTI-TOOL PLAN'),node('h3','',answer.title));head.append(title,node('span','analysis-date',`生日 ${answer.event_date}`));block.append(agentAvatar(),head);
  const summary=node('div','cross-summary');[['预算',answer.budget],['可用余额',answer.available],['最大支出类别',`${answer.top_category} ${answer.top_share_pct.toFixed(1)}%`],['需关注交易',`${answer.anomaly_count} 笔`]].forEach(([label,value])=>{const item=node('div','metric');item.append(node('small','',label),node('strong','',value));summary.append(item);});block.append(summary);
  const planner=analysisSection('Agent 可审计规划');const steps=node('ol','planner-steps');answer.plan_steps.forEach((item,index)=>{const row=node('li');row.append(node('i','',String(index+1).padStart(2,'0')));const copy=node('div');copy.append(node('b','',`${item.tool} · ${item.action}`),node('p','',item.observation));row.append(copy);steps.append(row);});planner.append(steps);block.append(planner);
  const options=analysisSection('选择一个执行方案'),grid=node('div','gift-grid');answer.options.forEach(item=>{const card=node('article','gift-option');const choose=button(item.within_budget === false ? '超出预算' : '选择并生成确认卡',()=>send(item.command),'primary');choose.disabled=item.within_budget === false || answer.affordable === false;card.append(node('span','gift-code',item.code),node('h4','',item.name),node('strong','',item.amount),node('p','',item.description),choose);grid.append(card);});options.append(grid);block.append(options);
  return block;
}
function renderUniversalPlan(answer) {
  const block=node('article',`analysis-card universal-plan urgency-${answer.urgency||'normal'}`);
  block.append(agentAvatar());
  const head=node('div','universal-head'),copy=node('div');copy.append(node('span','speaker','NEXUS / DYNAMIC TOOL PLANNER'),node('h3','',answer.title),node('p','analysis-summary',answer.subtitle));head.append(copy,node('span','plan-id',`FLOW ${String(answer.plan_id).padStart(4,'0')}`));block.append(head,node('p','analysis-method',answer.summary));
  const metrics=node('div','universal-metrics');(answer.metrics||[]).forEach(item=>{const m=node('div',`metric ${item.tone||''}`);m.append(node('small','',item.label),node('strong','',item.value));metrics.append(m);});block.append(metrics);
  const chart=renderChart(answer.chart); if(chart) block.append(chart);
  const pipeline=analysisSection('执行过程');const steps=node('ol','planner-steps universal-steps');(answer.steps||[]).forEach((item,index)=>{const row=node('li');row.append(node('i','',String(index+1).padStart(2,'0')));const c=node('div');c.append(node('b','',`${item.tool} · ${item.action}`),node('p','',item.observation));row.append(c,node('span',`step-state ${item.status}`,item.status==='done'?'已核验':'待执行'));steps.append(row);});pipeline.append(steps);block.append(pipeline);
  const advice=analysisSection('专业建议');const adviceGrid=node('div','advice-grid');(answer.recommendations||[]).forEach(item=>{const card=node('article','advice-item');card.append(node('span','advice-badge',item.badge),node('h4','',item.title),node('p','',item.detail));adviceGrid.append(card);});advice.append(adviceGrid);block.append(advice);
  const evidence=analysisSection('数据依据');const list=node('div','evidence-grid');(answer.evidence||[]).forEach(item=>{const e=node('article','evidence-item');e.append(node('small','',item.source),node('b','',item.label),node('p','',item.value));list.append(e);});evidence.append(list);block.append(evidence);
  const actions=node('div','plan-actions');(answer.actions||[]).forEach(item=>actions.append(button(item.label,()=>send(item.command),item.tone==='primary'?'primary':item.tone==='danger'?'danger':'')));block.append(actions);return block;
}
function renderBoundaryFlow(answer) {
  const block=node('article',`analysis-card boundary-card boundary-${answer.category||'notice'} ${answer.urgency==='urgent'?'urgency-urgent':''}`);
  const head=node('div','boundary-head');const copy=node('div');copy.append(node('span','speaker','NEXUS / BOUNDARY & FALLBACK'),node('h3','',answer.title||'需要进一步确认'));head.append(copy,node('span','boundary-code',(answer.category||'boundary').replaceAll('_',' ').toUpperCase()));block.append(agentAvatar(),head,node('p','boundary-message',answer.message||''));
  if(answer.summary){const summary=node('section','handoff-summary');summary.append(node('small','','我已经记下的情况'),node('p','',answer.summary));block.append(summary);}
  if(answer.metrics?.length){const metrics=node('div','universal-metrics');answer.metrics.forEach(item=>{const card=node('div','metric');card.append(node('small','',item.label),node('strong','',item.value));metrics.append(card);});block.append(metrics);}
  if(answer.paused_task){const paused=node('section','paused-task');paused.append(node('small','','已暂停的任务'),node('b','',answer.paused_task.intent==='transfer'?'转账任务':'待处理任务'),node('p','',`收款人：${answer.paused_task.recipient||'待补充'} · 金额：${answer.paused_task.amount?`¥${answer.paused_task.amount}`:'待补充'}`));block.append(paused);}
  const actions=node('div','plan-actions');(answer.actions||[]).forEach(item=>actions.append(button(item.label,()=>send(item.command),item.tone==='primary'?'primary':item.tone==='danger'?'danger':'')));if(answer.actions?.length)block.append(actions);
  return block;
}
function renderSplitChart(chart) {
  if (!chart) return null;
  const figure=node('figure','split-chart');
  if (chart.type === 'split_progress') {
    const total=Math.max(1,Number(chart.values?.[0]||0)+Number(chart.values?.[1]||0));
    const bar=node('div','split-progress-bar');
    const done=node('i','split-progress-done');done.style.width=`${Number(chart.values?.[0]||0)/total*100}%`;
    bar.append(done); figure.append(node('figcaption','',`已收 ${chart.values?.[0]||0} 人 · 待收 ${chart.values?.[1]||0} 人`),bar);
    const stats=node('div','split-chart-stats');stats.append(node('span','',`总额 ¥${money(chart.total)}`),node('b','',`每人 ¥${money(chart.per_person)}`));figure.append(stats);
  } else if (chart.labels?.length) {
    figure.append(node('figcaption','', 'AA 任务金额分布'));
    const chartFigure=lineChart([{name:'任务总额',values:chart.values||[]}], chart.labels||[], 'AA任务金额分布');
    figure.append(chartFigure);
  }
  return figure;
}
function renderAACollections(answer) {
  const block=node('article','analysis-card schedule-card');block.append(agentAvatar(),node('span','speaker','NEXUS / SPLIT COLLECTION'),node('h3','',answer.title),node('p','analysis-summary',answer.summary));
  const chart=renderSplitChart(answer.chart); if(chart) block.append(chart);
  const list=node('div','aa-list');
  if(!answer.items.length)list.append(node('p','muted','暂无 AA 收款任务。'));
  answer.items.forEach(item=>{const row=node('div','aa-row');const copy=node('div');copy.append(node('strong','',item.purpose),node('small','',`${item.participant_count} 人 · 总额 ¥${money(item.total)}`));const progress=node('div','aa-row-progress');const collected=Number(item.collected_count||0);progress.append(node('span','',`${collected}/${item.participant_count} 已收`));const bar=node('i');bar.style.width=`${Number(item.progress_pct||0)}%`;progress.append(bar);row.append(copy,node('b','',`每人 ¥${money(item.per_person)}`),progress,node('span','badge',item.status==='COLLECTING'?'收款中':item.status));list.append(row);});block.append(list);return block;
}
function renderToolStatus(catalog, health) {
  const list=$('tool-status'); if (!list) return; list.replaceChildren();
  catalog.tools.forEach(tool=>{const item=node('li',tool.status==='connected'?'connected':'optional');item.append(node('i'));const copy=node('div');copy.append(node('b','',tool.name),node('small','',tool.data));item.append(copy,node('em','',tool.status==='connected'?'已连接':'可选'));list.append(item);});
  // 不报"N 项能力"：那是给评审看的功能计数，客户要看到的是自己的账户已经接上了。
  const badge=$('model-badge');if(badge){badge.replaceChildren(node('i'),document.createTextNode('账户已接入'));}
}
function renderFinancialIntake(answer) {
  const block=node('article','analysis-card intake-card');
  block.append(agentAvatar());
  block.append(node('span','speaker','NEXUS / YOUR CONSTRAINTS'),node('h3','',answer.title),node('p','analysis-method',answer.message));
  const form=node('form','intake-form'), grid=node('div','intake-grid');
  let subscriptionList=null;
  const addSubscription=(value={})=>{
    if (!subscriptionList) return;
    const row=node('div','subscription-input-row');
    const merchant=node('input');merchant.type='text';merchant.placeholder='订阅名称';merchant.value=value.merchant_name||'';merchant.dataset.subMerchant='1';merchant.required=true;
    const amount=node('input');amount.type='number';amount.min='0.01';amount.step='0.01';amount.placeholder='金额';amount.value=value.amount||'';amount.dataset.subAmount='1';amount.required=true;
    const period=node('select');period.dataset.subPeriod='1';[['MONTHLY','每月'],['QUARTERLY','每季'],['YEARLY','每年']].forEach(([v,l])=>{const option=node('option','',l);option.value=v;if((value.period||'MONTHLY')===v)option.selected=true;period.append(option);});
    const day=node('input');day.type='number';day.min='1';day.max='28';day.step='1';day.value=value.next_charge_day||1;day.dataset.subDay='1';day.setAttribute('aria-label','预计扣费日');day.required=true;
    const essential=node('label','subscription-essential');const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=Boolean(value.essential);checkbox.dataset.subEssential='1';essential.append(checkbox,document.createTextNode('必要'));
    row.append(merchant,amount,period,day,essential,button('移除',()=>row.remove(),'text-button'));subscriptionList.append(row);
  };
  if (answer.templates?.length) {
    const presets=node('section','profile-presets');presets.append(node('h4','','先选一个典型画像，也可以从空白开始'));
    const presetGrid=node('div','preset-grid');
    answer.templates.forEach(template=>{const card=button('',()=>{
      Object.entries(template.values).forEach(([name,value])=>{if(Array.isArray(value)||name==='declared_subscriptions')return;const input=form.elements.namedItem(name);if(input)input.value=value;});
      template.values.seasonal_monthly_income.forEach((value,index)=>{form.querySelector(`[data-month-income="${index}"]`).value=value;});
      template.values.seasonal_monthly_expenses.forEach((value,index)=>{form.querySelector(`[data-month-expense="${index}"]`).value=value;});
      subscriptionList.replaceChildren();template.values.declared_subscriptions.forEach(addSubscription);
      presetGrid.querySelectorAll('button').forEach(item=>item.classList.toggle('selected',item===card));
    },'preset-card');card.append(node('strong','',template.name),node('span','',template.description));presetGrid.append(card);});
    presets.append(presetGrid,node('p','analysis-method','画像仅用于快速预填；每个字段都能继续修改，最终评分不会使用固定结论。'));form.append(presets);
  }
  for (const field of answer.fields) {
    if (field.type === 'monthly_grid') {
      const panel=node('fieldset','seasonal-input');
      const legend=node('legend','',field.label);
      const note=node('p','',field.hint || '');
      const header=node('div','seasonal-row seasonal-head');header.append(node('b','','月份'),node('b','','收入'),node('b','','生活支出'));
      panel.append(legend,note,header);
      for (let index=0; index<12; index++) {
        const row=node('label','seasonal-row');row.append(node('span','',`${index+1} 月`));
        const income=node('input');income.type='number';income.min='0';income.step='0.01';income.required=true;income.dataset.monthIncome=String(index);income.value=field.income_values?.[index] ?? answer.values.monthly_income ?? '0';income.setAttribute('aria-label',`${index+1}月收入`);
        const expense=node('input');expense.type='number';expense.min='0';expense.step='0.01';expense.required=true;expense.dataset.monthExpense=String(index);expense.value=field.expense_values?.[index] ?? answer.values.essential_expenses ?? '0';expense.setAttribute('aria-label',`${index+1}月生活支出`);
        row.append(income,expense);panel.append(row);
      }
      grid.append(panel); continue;
    }
    if (field.type === 'subscription_list') {
      const panel=node('fieldset','subscription-input');panel.append(node('legend','',field.label),node('p','',field.hint||''));
      subscriptionList=node('div','subscription-input-list');panel.append(subscriptionList,button('+ 添加一项订阅',()=>addSubscription(), 'subscription-add'));
      grid.append(panel);(field.values||[]).forEach(addSubscription);continue;
    }
    const label=node('label','intake-field'), title=node('span','',field.label); let input;
    if (field.type === 'choice') {
      input=node('select');
      for (const option of field.options) { const item=node('option','',option.label); item.value=option.value; input.append(item); }
    } else {
      input=node('input'); input.type=field.type === 'text' ? 'text' : 'number';
      if (input.type === 'number') { input.min=field.name === 'goal_amount' || field.name === 'horizon_months' ? '1' : '0'; input.step=field.type === 'integer' ? '1' : '0.01'; }
    }
    input.name=field.name; input.required=true; input.value=answer.values[field.name] ?? (field.name === 'income_stability' ? 'STABLE' : ['annual_income','annual_expenses','liquid_savings','investment_assets','debt_interest_rate'].includes(field.name) ? '0' : '');
    label.append(title,input,node('small','',field.hint || '')); grid.append(label);
  }
  const submit=node('button','primary','保存资料并生成方案'); submit.type='submit';
  form.append(grid,submit);
  form.addEventListener('submit',async event=>{
    event.preventDefault(); if (busy) return; setBusy(true); showError(''); setAgentState('正在生成个性化方案','thinking');
    showThinkingBubble();
    const data=Object.fromEntries(new FormData(form).entries());
    data.seasonal_monthly_income=[...form.querySelectorAll('[data-month-income]')].map(input=>input.value);
    data.seasonal_monthly_expenses=[...form.querySelectorAll('[data-month-expense]')].map(input=>input.value);
    data.declared_subscriptions=[...form.querySelectorAll('.subscription-input-row')].map(row=>({merchant_name:row.querySelector('[data-sub-merchant]').value,amount:row.querySelector('[data-sub-amount]').value,period:row.querySelector('[data-sub-period]').value,next_charge_day:Number(row.querySelector('[data-sub-day]').value),essential:row.querySelector('[data-sub-essential]').checked}));
    // 保存资料会改掉侧栏依赖的目标进度和现金流，方案重算的同时侧栏也要跟着重取。
    try { const result=await api('/financial-profile',data); hideThinkingBubble(); renderAnswer(result); block.remove(); setAgentState('方案已生成'); await refresh(); }
    catch(error){showError(error.message); hideThinkingBubble();} finally {setBusy(false);}
  });
  block.append(form);
  return block;
}
function renderAnalysis(answer) {
  const block=node('article','analysis-card');
  const head=node('div','analysis-head');
  const title=node('div'); title.append(node('span','speaker','NEXUS / PERSONAL PLAN'),node('h3','',answer.title));
  head.append(title,node('span','analysis-date',`数据截至 ${answer.as_of}`)); block.append(agentAvatar(),head);
  if(answer.scenario&&answer.summary){block.append(node('p','advice-verdict',answer.summary));const grid=node('div','finance-grid');(answer.metrics||[]).forEach(item=>{const cell=node('div','finance-stat');cell.append(node('small','',item.label),node('strong','',item.value));grid.append(cell);});block.append(grid);}
  const badges=node('div','analysis-badges');
  badges.append(node('span','',`${answer.profile.risk_score} · ${answer.profile.style}`),node('span','',`数据质量：${answer.profile.data_quality}`)); block.append(badges);

  // 建议放在最前面。用户问的是“我该买什么”，不是“请看我的数据”。其余 section
  // 都是这句话的依据，因此一律排在建议之后。
  if (answer.recommendation) {
    const rec=answer.recommendation, section=analysisSection('给你的建议');
    section.append(node('p','advice-verdict',rec.verdict));
    const invest=node('div','advice-amount');
    const investBox=node('div'), capBox=node('div');
    investBox.append(node('small','','这次可以投'),node('strong','',rec.investable.amount),node('em','',rec.investable.basis));
    capBox.append(node('small','','产品风险上限'),node('strong','',rec.risk_cap.level),node('em','',`受限于${rec.risk_cap.reason}`));
    invest.append(investBox,capBox);
    section.append(invest);
    if (rec.actions.length) {
      const steps=node('ol','advice-actions');
      rec.actions.forEach(item=>steps.append(node('li','',item)));
      section.append(steps);
    }
    if (rec.debt_comparison) {
      const cmp=node('div','debt-compare');
      cmp.append(node('strong','',`负债 ${rec.debt_comparison.debt_rate} vs 产品最高参考值 ${rec.debt_comparison.best_reference_yield} → ${rec.debt_comparison.conclusion}`),node('small','',rec.debt_comparison.note));
      section.append(cmp);
    }
    block.append(section);
  }

  if(answer.saving_options?.length){const section=analysisSection('可以先核实的节省机会');answer.saving_options.forEach(item=>{const row=node('div','allocation-row');row.append(node('strong','',`${item.merchant} → ${item.alternative}`),node('p','',`每月可省 ${item.monthly_saving}，全年 ${item.annual_saving}。${item.note}`));section.append(row);});block.append(section);}
  if (answer.health) {
    const dashboard=analysisSection(answer.scenario?'当前资料的财务健康诊断':'财务健康诊断');
    const overview=node('div','health-overview');
    const gauge=node('div','score-gauge');
    gauge.style.setProperty('--score',Math.max(0,Math.min(100,answer.health.score)));
    const gaugeCore=node('div','score-core');
    gaugeCore.append(node('strong','',String(answer.health.score)),node('small','','/ 100'),node('span','',answer.health.label));
    gauge.append(gaugeCore);
    const scoreCopy=node('div','score-copy');
    scoreCopy.append(node('p','score-kicker',`基础分 ${answer.health.base_score} · 波动后得分 ${answer.health.score}`));
    // 五个分项是分数的推导过程，不是结论本身。折起来，让大字分数当主角。
    const detail=node('details','score-detail');
    detail.append(node('summary','','这个分数怎么算出来的'));
    const components=node('div','score-components');
    answer.health.components.forEach(item=>{const row=node('div','score-component');const top=node('div');top.append(node('span','',item.name),node('b','',`${item.score} / ${item.max}`));row.append(top,node('small','',item.basis));components.append(row);});
    detail.append(components,node('p','analysis-method',answer.health.method));
    scoreCopy.append(detail);
    overview.append(gauge,scoreCopy);dashboard.append(overview);
    block.append(dashboard);
  }

  if (answer.recommendation?.buy.length) {
    const products=analysisSection('可以买');
    for (const item of answer.recommendation.buy) {
      const card=node('div','product-card');
      const top=node('div','product-card-top');
      const name=node('div'); name.append(node('strong','',item.name),node('small','',`${item.code} · ${item.risk_level} · ${item.lock_days?`锁定 ${item.lock_days} 天`:'无锁定'}`));
      const yieldBox=node('div','product-yield'); yieldBox.append(node('strong','',item.reference_yield),node('small','','参考值'));
      top.append(name,yieldBox);
      card.append(top,node('small','product-floor',`起购 ${item.min_purchase}`));
      const why=node('ul','product-why'); (item.why||[]).forEach(line=>why.append(node('li','',line)));
      card.append(why); products.append(card);
    }
    block.append(products);
  }

  if (answer.recommendation?.avoid.length) {
    const avoid=analysisSection('先别买');
    const list=node('div','avoid-list');
    answer.recommendation.avoid.forEach(item=>{const row=node('div','avoid-row');row.append(node('strong','',item.subject),node('p','',item.reason));list.append(row);});
    avoid.append(list); block.append(avoid);
  }

  if (answer.allocation) {
    const allocation=analysisSection('钱怎么分');
    allocation.append(node('p','analysis-method',answer.allocation.method));
    const bars=node('div','allocation-list');
    for (const item of answer.allocation.buckets) {
      const row=node('div','allocation-row'), label=node('div','allocation-label');
      label.append(node('strong','',item.name),node('span','',`${item.amount} · ${item.target}`));
      row.append(label,node('small','allocation-status',({READY:'已备足',GAP:'有缺口',ON_TRACK:'按计划推进',AVAILABLE:'可配置',WAIT:'暂缓配置'})[item.status]||item.status),node('p','',item.rationale)); bars.append(row);
    }
    allocation.append(bars); block.append(allocation);
  }

  if(answer.cashflow) {
    const section=analysisSection(answer.scenario?'原有资料的月均现金流（情景调整前）':'每月资金怎么流动'),flow=answer.cashflow;
    const grid=node('div','finance-grid');
    [['月均收入',flow.income],['必要生活支出',flow.essential],['每月还款',flow.debt_payment],['订阅扣费',flow.subscriptions],['月均结余',flow.surplus]].forEach(([label,value])=>{const cell=node('div','finance-stat');cell.append(node('small','',label),node('strong','',value));grid.append(cell);});section.append(grid);
    if(answer.seasonality?.months?.length)section.append(lineChart([{name:'收入',values:answer.seasonality.months.map(item=>item.income)},{name:'流出',values:answer.seasonality.months.map(item=>item.expenses)},{name:'结余',values:answer.seasonality.months.map(item=>item.net)}],answer.seasonality.months.map(item=>`${item.month}月`),'全年月度现金流','¥'));
    section.append(node('p','chart-note','依据财务资料与季节性假设测算，不等同于已发生的银行流水。'));block.append(section);
  }
  // 支撑数据：它解释结论，但本身不是结论。折起来，让人先看完上面再看依据。
  const detail=node('details','evidence-more');
  detail.append(node('summary','','查看全部计算依据'));
  const body=node('div','evidence-body');
  if (answer.balance_sheet && answer.cashflow) {
    const finances=analysisSection('资产负债与年度现金流');
    const sheet=node('div','finance-grid');
    [['总资产',answer.balance_sheet.assets],['负债',answer.balance_sheet.liabilities],['净资产',answer.balance_sheet.net_worth],['年度收入',answer.cashflow.annual_income],['年度总流出',answer.cashflow.annual_outflow],['年度结余',answer.cashflow.annual_surplus]].forEach(([label,value],index)=>{const card=node('div',index===2||index===5?'finance-stat accent':'finance-stat');card.append(node('small','',label),node('strong','',value));sheet.append(card);});
    finances.append(sheet);
    const flow=node('div','cashflow-breakdown');
    [['年均月收入',answer.cashflow.income],['生活支出',answer.cashflow.essential],['每月还款',answer.cashflow.debt_payment],['订阅代扣',answer.cashflow.subscriptions],['月均结余',answer.cashflow.surplus]].forEach(([label,value])=>{const item=node('div');item.append(node('span','',label),node('b','',value));flow.append(item);});
    finances.append(flow); body.append(finances);
  }
  if (answer.seasonality) {
    const seasonal=analysisSection('季节性现金流与压力月份');
    const kpis=node('div','seasonal-kpis');
    [['年度储蓄率',`${answer.seasonality.annual_savings_rate_pct.toFixed(1)}%`],['最差月份',`${answer.seasonality.worst_month} 月`],['最差月收支比 M',answer.seasonality.worst_month_ratio.toFixed(2)],['收支波动率 σ',answer.seasonality.volatility.toFixed(3)],['波动扣分',`-${answer.seasonality.penalty_pct.toFixed(1)}%`]].forEach(([label,value])=>{const item=node('div');item.append(node('small','',label),node('strong','',value));kpis.append(item);});
    seasonal.append(kpis,lineChart([{name:'月收入',values:answer.seasonality.months.map(item=>item.income)},{name:'总流出',values:answer.seasonality.months.map(item=>item.expenses)},{name:'净现金流',values:answer.seasonality.months.map(item=>item.net)}],answer.seasonality.months.map(item=>`${item.month}月`),'十二个月收入、流出与净现金流趋势'));
    seasonal.append(node('p','analysis-method',`模型按全年总额计算储蓄率，并用最差月份评估现金流安全垫。最差月净现金流为 ${answer.seasonality.worst_month_net}。`));
    body.append(seasonal);
  }
  if (answer.stress_tests?.length) {
    const stress=analysisSection('目标压力测试');
    const grid=node('div','stress-grid');
    answer.stress_tests.forEach(item=>{const card=node('div',`stress-card ${item.status==='可达'?'pass':'gap'}`);const h=node('div');h.append(node('strong','',item.name),node('span','',item.status));const progress=node('progress');progress.max=100;progress.value=Math.min(item.attainment_pct,100);card.append(h,node('p','',`月结余 ${item.monthly_surplus}`),node('p','',`期限末预计 ${item.projected_goal}`),progress,node('small','',`目标达成度 ${item.attainment_pct.toFixed(1)}%`));grid.append(card);});
    stress.append(grid); body.append(stress);
  }
  if (answer.metrics?.length) {
    const metrics=analysisSection('数据依据');
    const metricGrid=node('div','metric-grid');
    for (const item of answer.metrics) { const card=node('div','metric'); card.append(node('small','',item.label),node('strong','',item.value),node('p','',item.basis)); metricGrid.append(card); }
    metrics.append(metricGrid); body.append(metrics);
  }
  if (answer.narrative) {
    // 模型散文没有数字，也不参与决策，所以放进依据区而不是答案区。
    // 上面的建议和行动项已经用核对过的数回答了问题，这里只是补充视角。
    const narrative=analysisSection('模型补充解读');
    narrative.append(node('p','narrative-title',answer.narrative.headline),node('p','analysis-method',answer.narrative.assessment));
    const priorities=node('ul','analysis-list'); answer.narrative.priorities.forEach(item=>priorities.append(node('li','',item))); narrative.append(priorities);
    narrative.append(node('p','analysis-method',`需要权衡：${answer.narrative.tradeoffs.join('；')}`));
    body.append(narrative);
  }
  if (answer.observations?.length) {
    const evidence=analysisSection('关键观察');
    const observations=node('ul','analysis-list'); answer.observations.forEach(item=>observations.append(node('li','',item))); evidence.append(observations); body.append(evidence);
  }
  if (answer.decision) {
    const decision=analysisSection('可核验结论');
    decision.append(node('p','analysis-method',`目标可行性：${answer.decision.goal_feasibility} · 每月需要：${answer.decision.monthly_required} · 每月结余：${answer.decision.monthly_surplus} · 产品风险上限：${answer.decision.max_product_risk}`)); body.append(decision);
  }
  detail.append(body); block.append(detail);

  if(answer.supporting_bill&&!answer.supporting_bill.empty){const bill=answer.supporting_bill;block.append(renderAgentEvidence([{tool:'get_bills',data:{total:bill.summary.total,categories:bill.categories,chart:bill.chart,period:bill.period}}]));if(bill.transactions?.length)block.append(transactionExplorer(bill));}
  const edit=node('button','analysis-edit-link','这些数据算错了？改我的资料');
  edit.addEventListener('click',async()=>{ if(busy) return; setBusy(true); showError(''); try { renderAnswer(await api('/financial-profile')); } catch(error){showError(error.message);} finally {setBusy(false);} });
  block.append(edit);
  return block;
}
// 按 AI 的排版决定拼装区块。它只重排已经渲染好的节点——数据早就在节点里绑定了，
// 排版层动不了内容，只能决定顺序和谁先折叠。
function compose(block, answer, slots) {
  const plan = answer.presentation || {};
  const byName = new Map(slots.map(slot => [slot.name, slot]));
  const fold = new Set(plan.fold || []);
  const order = (plan.order || []).filter(name => byName.has(name));
  order.forEach((name, index) => {
    const slot = byName.get(name);
    // 排在第一位的永远展开：折叠用户最想看的东西只是把问题藏起来。
    if (fold.has(name) && index > 0) {
      const group = node('details', 'snapshot-more');
      group.append(node('summary', '', slot.label), slot.el);
      block.append(group);
    } else {
      block.append(slot.el);
    }
  });
  return block;
}
// 区块代号是给排版器用的，不是给客户看的。模型偶尔会把 hero 之类写进理由里，
// 那句话就变成了黑话，所以在这里换成客户看得懂的说法。
const BLOCK_WORDS = {
  hero: '主数字', transactions: '流水', cards: '卡片', subscriptions: '订阅', accounts: '账户',
  summary: '关键数字', chart: '趋势图', categories: '消费结构', anomalies: '需关注交易',
  insights: '观察', ranking: '排行', comparison: '环比', prudence: '审慎原则',
  checklist: '分项体检', warnings: '风险提示', allocation: '资产配置', advice: '配置建议',
};
function humanise(text) {
  return String(text || '').replace(/\b([a-z_]+)\b/gi, (word) => {
    const key = word.toLowerCase();
    return BLOCK_WORDS[key] || word;
  });
}
// 排版理由随答案一起显示。它是这个回答"为什么长这样"的唯一解释。
// 一句话的答复没有"排版"可言——在那里显示排版决策只是噪音。
const LAYOUT_EXPLAINED = new Set([
  'account_snapshot','bill_analysis','risk_report','recurring_detection',
  'product_catalog','risk_intake','agent_response',
]);
function layoutNote(answer) {
  const plan = answer.presentation || {};
  if (!plan.rationale || !LAYOUT_EXPLAINED.has(answer.type)) return null;
  const note = node('p', 'layout-note');
  note.append(node('span', 'layout-note-tag', plan.source === 'model' ? 'AI 排版' : '默认排版'),
    node('span', '', humanise(plan.rationale)));
  if (plan.emphasis) note.append(node('em', '', `重点：${humanise(plan.emphasis)}`));
  return note;
}
function renderBillAnalysis(answer) {
  const block=node('article','analysis-card bill-card');
  block.append(agentAvatar());
  const head=node('div','analysis-head'), title=node('div');
  title.append(node('span','speaker','NEXUS / BILL INTELLIGENCE'),node('h3','',answer.title));
  head.append(title,node('span','analysis-date',answer.period.label)); block.append(head);
  if (answer.empty) {
    block.append(node('p','analysis-summary',answer.message));
    return block;
  }
  const slots=[];
  // 先给问的那个数，再给结构。收入问题不该先读到"本期支出"的四个格子。
  if (answer.hero) {
    const hero=node('div','snapshot-hero');
    hero.append(node('span','snapshot-hero-label',answer.hero.label),node('strong','snapshot-hero-value',answer.hero.value));
    if(answer.hero.note) hero.append(node('small','snapshot-hero-note',answer.hero.note));
    if((answer.hero.aside||[]).length){
      const aside=node('div','snapshot-aside');
      answer.hero.aside.forEach(item=>aside.append(node('span','',item.label),node('b',item.tone?`stat-${item.tone}`:'',item.value)));
      hero.append(aside);
    }
    slots.push({name:'hero',label:'概览',el:hero});
  }
  const summary=node('div','bill-summary');
  const summaryItems=[[answer.headline_label||'本期支出',answer.summary.total],['交易笔数',`${answer.summary.transaction_count} 笔`],['日均支出',answer.summary.daily_average],['周期扣费',answer.summary.recurring]];
  summaryItems.forEach(([label,value])=>{const item=node('div','metric');item.append(node('small','',label),node('strong','',value));summary.append(item);});
  slots.push({name:'summary',label:'关键数字',el:summary});
  const dashboard=analysisSection('消费结构');
  const categoryLayout=node('div','category-layout');
  categoryLayout.append(categoryDonut(answer.categories,answer.summary.total));
  const categories=node('div','category-list');
  for (const [index,item] of answer.categories.entries()) {
    const row=node('div','bill-category'), copy=node('div');
    row.style.setProperty('--category-color',CATEGORY_COLORS[index%CATEGORY_COLORS.length]);
    copy.append(node('strong','',item.name),node('small','',`${item.count} 笔 · ${item.share_pct.toFixed(1)}%`));
    const amount=node('b','',item.amount), progress=node('progress'); progress.max=100; progress.value=item.share_pct; progress.setAttribute('aria-label',`${item.name}占比 ${item.share_pct}%`);
    row.append(copy,amount,progress); categories.append(row);
  }
  categoryLayout.append(categories);dashboard.append(categoryLayout);slots.push({name:'categories',label:'消费结构',el:dashboard});
  if(answer.transactions?.length)block.dataset.hasLedger='true';
  const anomaly=analysisSection(`需关注交易 · ${answer.anomalies.length}`);
  if (answer.anomalies.length) {
    answer.anomalies.forEach(item=>{const row=node('div','bill-anomaly');const copy=node('div');copy.append(node('strong','',item.merchant),node('small','',`${item.date} · ${item.category}`),node('p','',item.reason));row.append(copy,node('b',`severity ${item.severity.toLowerCase()}`,item.amount));anomaly.append(row);});
  } else anomaly.append(node('p','analysis-method','本期未发现明显偏离同类金额基线的交易。'));
  slots.push({name:'anomalies',label:'需关注交易',el:anomaly});
  const trend=analysisSection(answer.period.kind==='year'?'年度月度趋势':'收支与净结余');
  const trendKpis=node('div','trend-kpis');
  const cmp=answer.period_comparison;
  const change=answer.trend_stats.change_pct===null?'无上月基线':`${answer.trend_stats.change_pct>=0?'+':''}${answer.trend_stats.change_pct.toFixed(1)}%`;
  [['环比变化',change],['月均支出',answer.trend_stats.average],['月度波动率',`${answer.trend_stats.volatility_pct.toFixed(1)}%`],['支出峰值',`${answer.trend_stats.highest_month} · ${answer.trend_stats.highest_amount}`]].forEach(([label,value])=>{const item=node('div','metric');item.append(node('small','',label),node('strong','',value));trendKpis.append(item);});
  trend.append(trendKpis);
  // The backend sends one chart covering income, spending and net position.
  // Falling back to the raw monthly totals keeps this section populated if an
  // older cached answer arrives without a chart.
  const trendChart=renderChart(answer.chart)
    || lineChart([{name:'月度支出',values:answer.monthly_trend.map(item=>item.amount_value)}],answer.monthly_trend.map(item=>item.label.slice(2)),'近月支出趋势');
  trend.append(trendChart);
  if(cmp) trend.append(node('p','chart-note',`${cmp.from} → ${cmp.to} 支出 ${cmp.expense_delta>=0?'+':''}¥${Number(cmp.expense_delta).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}${cmp.expense_pct===null?'':`（${cmp.expense_pct>=0?'+':''}${cmp.expense_pct}%）`}${cmp.net_delta===undefined?'':` · 净结余 ${cmp.net_delta>=0?'+':''}¥${Number(cmp.net_delta).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`}`));
  slots.push({name:'chart',label:'收支趋势',el:trend});

  const patterns=analysisSection('商户与消费时段');
  const patternGrid=node('div','pattern-grid'), merchants=node('div','rank-panel'), weekdays=node('div','rank-panel');
  merchants.append(node('h5','','商户支出排行'));
  answer.merchant_ranking.forEach((item,index)=>{const row=node('div','rank-row');const label=node('div');label.append(node('i','',String(index+1)),node('span','',item.name));const data=node('div');data.append(node('b','',item.amount),node('small','',`${item.count} 笔 · ${item.share_pct.toFixed(1)}%`));row.append(label,data);merchants.append(row);});
  weekdays.append(node('h5','','星期支出分布'));
  const maxWeek=Math.max(1,...answer.weekday_pattern.map(item=>item.amount_value));
  answer.weekday_pattern.forEach(item=>{const row=node('div','weekday-row');row.append(node('span','',item.label));const bar=node('i');bar.style.setProperty('--bar',`${item.amount_value/maxWeek*100}%`);row.append(bar,node('b','',item.amount),node('small','',`${item.count} 笔`));weekdays.append(row);});
  patternGrid.append(merchants,weekdays);patterns.append(patternGrid);
  const concentration=node('p','analysis-method',`前三大商户占本期支出 ${answer.concentration.top_three_merchant_pct.toFixed(1)}%，周期性支出占 ${answer.concentration.recurring_pct.toFixed(1)}%。`);patterns.append(concentration);slots.push({name:'ranking',label:'商户与时段',el:patterns});
  const insights=analysisSection('Agent 观察'), list=node('ul','analysis-list');
  answer.insights.forEach(item=>list.append(node('li','',item))); insights.append(list); slots.push({name:'insights',label:'Agent 观察',el:insights});
  compose(block, answer, slots);
  if(answer.daily_spending?.length>1&&answer.daily_spending.length<=31){const daily=analysisSection('每天花了多少');daily.append(lineChart([{name:'日支出',values:answer.daily_spending.map(item=>item.amount_value)}],answer.daily_spending.map(item=>item.date.slice(5)),'本期每日消费金额','¥'));block.append(daily);}
  if(answer.transactions?.length)block.append(transactionExplorer(answer));
  block.append(node('p','chart-note',answer.method||'仅统计已导入的消费账单。'));return block;
}
// 二次核验卡：确认卡下方紧接身份核验。用户回答的是"这笔是否仍由我授权"，
// 所以确认摘要保持可见，不做成一个凭空出现的独立弹窗。
let demoPasscode='';
function renderAgentEvidence(evidence) {
  const section=analysisSection('本次核验的数据'),seen=new Set();
  evidence.forEach(item=>{
    if(seen.has(item.tool))return;seen.add(item.tool);const data=item.data||{};
    if(item.tool==='get_bills'&&!data.empty&&data.total){
      const box=analysisSection(data.period?.label?`${data.period.label}消费结构`:'消费结构');box.append(categoryDonut(data.categories||[],data.total));
      const list=node('div','evidence-category-list');(data.categories||[]).forEach(row=>{const line=node('div','evidence-value');line.append(node('span','',row.name),node('b','',`${row.amount} · ${row.share_pct}%`));list.append(line);});box.append(list);const chart=renderChart(data.chart);if(chart)box.append(chart);section.append(box);
    }else if(item.tool==='get_balance'||item.tool==='get_financial_profile'||item.tool==='get_subscriptions'){
      const labels=item.tool==='get_balance'?[['余额','balance'],['可用余额','available'],['预占','reserved']]:item.tool==='get_financial_profile'?[['月收入','monthly_income'],['基础结余（未扣订阅）','monthly_surplus'],['目标','goal'],['期限（月）','horizon_months']]:[['月度订阅扣费','monthly_active']];
      const grid=node('div','finance-grid');labels.forEach(([label,key])=>{if(data[key]==null)return;const cell=node('div','finance-stat');cell.append(node('small','',label),node('strong','',String(data[key])));grid.append(cell);});if(grid.children.length)section.append(grid);
    }
  });
  const detail=node('details','evidence-more');detail.append(node('summary','',`查看 ${seen.size} 项数据来源`));const names={get_balance:'账户余额',get_bills:'消费账单',get_financial_profile:'财务资料',get_products:'在售产品',get_subscriptions:'订阅与代扣',get_cards:'银行卡',get_events:'已授权事件',get_recipients:'已登记收款人',get_fx:'参考汇率'};evidence.forEach(item=>{const row=node('div','tool-evidence');row.append(node('strong','',names[item.tool]||'授权数据'),node('p','analysis-method',item.data?.empty?'该期间暂无记录':'已读取当前账户授权数据；以上结论引用本次读取结果。'));detail.append(row);});section.append(detail);return section;
}
function renderStepUp(answer) {
  const block=node('article','analysis-card confirm-card step-up-card');
  block.append(agentAvatar(),node('span','speaker','NEXUS / STEP-UP VERIFICATION'),node('h3','',answer.title),node('p','analysis-method',answer.detail));
  const challenge=answer.challenge||{};
  const form=node('form','step-up-form');
  form.append(node('p','step-up-msg',challenge.message||'请完成二次核验以继续。'));
  (challenge.fields||[]).forEach(field=>{
    const row=node('label','step-up-field');
    row.append(node('span','',field.label));
    const input=node('input');
    input.type=field.format==='digits'?'text':'text';
    input.inputMode=field.format==='digits'?'numeric':'decimal';
    input.autocomplete='off';
    if(field.length) input.maxLength=field.length;
    if(field.format==='digits') input.pattern='\\d{4}';
    input.dataset.stepField=field.name;
    input.required=true;
    row.append(input);
    form.append(row);
  });
  if(challenge.passcode_required){
    if (demoPasscode) {
      // 沙箱里没有真实持卡人，口令在进入页面时就发给你了。把它摆在卡片上，
      // 否则用户只能猜——而输错三次就锁死，整条链路看起来是坏的。
      const note=node('p','step-up-demo-code');
      note.append(document.createTextNode('本次演示口令：'),node('b','',demoPasscode));
      form.append(note);
    }
    const row=node('label','step-up-field');
    row.append(node('span','',challenge.passcode_hint||''));
    const input=node('input');
    input.type='password';input.inputMode='numeric';input.autocomplete='off';
    input.maxLength=challenge.passcode_length||4;input.pattern='\\d{4}';
    input.dataset.stepField='passcode';input.required=true;
    row.append(input);
    form.append(row);
    // 没有设置入口的话，卡片会把用户停在"请先设置本机验证密码"上。验证密码只
    // 活在本次会话的内存里，所以它必须能在核验卡片里就地设置或更换。
    form.append(passcodeSetup(challenge));
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const body={echoes:{},passcode:null};
    (challenge.fields||[]).forEach(field=>{const input=form.querySelector(`[data-step-field="${field.name}"]`);if(input)body.echoes[field.name]=input.value;});
    const code=form.querySelector('[data-step-field="passcode"]');
    if(code) body.passcode=code.value;
    setBusy(true);
    showStepUpProblem(null);
    // 核验通过后钱才真的动。侧栏那五块（订阅、定时转账、AA 收款、流水、审计）
    // 必须在这里重取，否则计划已经在库里、卡片还停在执行前的样子——用户会以为
    // 没生效，甚至重复点一次。
    try{ renderAnswer(await api(`/actions/${answer.action_id}/step-up`,body,'POST')); setAgentState('已执行'); await refresh(); }
    catch(problem){ showStepUpProblem(problem); } finally{setBusy(false);}
  });

  // 执行失败必须留在原地，并说清三件事：钱动没动、差在哪、现在能做什么。
  // 之前只有一个页面顶部的红字——在长对话里它早就滚出屏幕了，客户看到的是
  // 一张没反应的卡，只能猜，然后回头去别的卡上乱点。
  const problem = node('div','step-up-problem');
  function showStepUpProblem(failure) {
    problem.replaceChildren();
    if (!failure) return;
    const gap = failure.extra && failure.extra.shortfall;
    problem.append(node('strong','', failure.message || '这笔没有执行。'));
    const lines = ['这笔操作没有执行，你的钱没有动，也没有产生任何计划。'];
    if (gap) {
      lines.push(`差额 ¥${money(gap)}。你可以把金额改成账户里的可用数、换一天执行，或者直接取消。`);
    }
    problem.append(node('p','', lines.join('')));
    const actionsRow = node('div','step-up-problem-actions');
    if ((answer.editable || []).length) {
      actionsRow.append(button('改金额后重试', () => {
        problem.replaceChildren();
        const editor = actionEditor(answer, (updated) => { renderAnswer(updated); setAgentState('已更新 · 请重新确认'); });
        block.insertBefore(editor, form);
      }, 'primary'));
    }
    actionsRow.append(button('取消这笔', async () => {
      setBusy(true);
      try {
        renderAnswer(await api(`/actions/${answer.action_id}/cancel`, {}, 'POST'));
        setAgentState('已取消 · 钱没有动'); await refresh();
      } catch(failure2){ showStepUpProblem(failure2); } finally{ setBusy(false); }
    }));
    problem.append(actionsRow);
    block.append(problem);
    problem.scrollIntoView({block:'nearest'});
  }
  block.append(problem);

  const actions=node('div','plan-actions');
  actions.append(button('完成核验并执行',()=>form.requestSubmit(),'primary'));
  actions.append(button('取消',async()=>{
    setBusy(true);
    // 传 {} 才能发 POST。之前这里是 api(url, undefined)，而 api 把"没有 body"
    // 当成 GET，于是取消按钮打在一个只接受 POST 的路由上：405、一行滚出屏幕的
    // 红字，客户唯一能真正取消的地方是更早那张确认卡——两张卡讲同一笔钱，
    // 却只有一个是活的。
    try{ renderAnswer(await api(`/actions/${answer.action_id}/cancel`,{},'POST')); setAgentState('已取消 · 钱没有动'); await refresh(); }
    catch(error){ showStepUpProblem(error); } finally{setBusy(false);}
  },''));
  form.append(actions);
  block.append(form);
  return block;
}
// 就地设置/更换本机验证密码。密码只在本次会话的内存里，服务端只留哈希，
// 所以这里不提供"找回"，只能重新设置——这正是它应该在卡片上就地完成的原因。
function passcodeSetup(challenge) {
  const wrap=node('div','step-up-setup');
  const length=challenge.passcode_length||4;
  const form=node('form','step-up-setup-form');
  const input=node('input');
  input.type='password';input.inputMode='numeric';input.autocomplete='off';
  input.maxLength=length;input.pattern=`\\d{${length}}`;input.required=true;
  input.placeholder=`设置 ${length} 位验证密码`;
  input.dataset.setupPasscode='1';
  form.append(input);
  wrap.append(node('span','step-up-setup-label','首次使用或需要更换？'),form);
  async function save(){
    if(busy) return;
    setBusy(true); showError('');
    try{
      await api('/step-up/passcode',{passcode:input.value});
      input.value='';
      wrap.replaceChildren(node('span','step-up-setup-ok','验证密码已在本机生效，请在上方输入它完成核验。'));
    }catch(error){showError(error.message);} finally{setBusy(false);}
  }
  form.addEventListener('submit',event=>{event.preventDefault(); save();});
  form.append(button('设置验证密码',()=>save(),'ghost'));
  return wrap;
}
function scoreBar(score, max) {
  const wrap=node('div','risk-bar');
  const track=node('i');track.style.width=`${Math.min(100,Number(score)/Number(max)*100)}%`;
  wrap.append(track,node('b','',`${Number(score).toFixed(1)} / ${max}`));
  return wrap;
}
function renderRiskIntake(answer) {
  const block=node('article','analysis-card risk-card');
  block.append(agentAvatar());
  block.append(node('span','speaker','NEXUS / RISK ASSESSMENT'),node('h3','',answer.title),node('p','analysis-method',answer.message));
  const prev=answer.objective_preview;
  if(prev){
    const box=node('section','risk-objective');
    box.append(node('small','','客观承受能力（已从你的财务档案读取）'));
    box.append(node('strong','',`${prev.grade} ${prev.grade_label}`),node('span','',`财务 BMI ${Number(prev.score).toFixed(1)} 分`));
    const m=prev.metrics||{};
    const facts=node('div','risk-facts');
    [['结余率',`${m.savings_rate_pct}%`],['安全垫',`${m.safety_months} 个月`],['负债率',`${m.debt_ratio_pct}%`],['净资产',`¥${money(m.net_worth||0)}`]]
      .forEach(([k,v])=>{const item=node('div');item.append(node('small','',k),node('b','',String(v)));facts.append(item);});
    box.append(facts);
    box.append(node('p','analysis-method','这一半不需要你填写，下面四题只用来确认你自己的风险意愿。'));
    block.append(box);
  }
  const form=node('form','risk-form');
  answer.questions.forEach((question,index)=>{
    const field=node('fieldset','risk-question');
    field.append(node('legend','',`${index+1}. ${question.prompt}`),node('p','risk-hint',question.hint));
    const row=node('div','risk-options');
    question.options.forEach(option=>{
      const label=node('label','risk-option');
      const radio=node('input');radio.type='radio';radio.name=question.name;radio.value=option.label;radio.required=true;
      label.append(radio,node('span','',option.label),node('small','',option.hint));
      row.append(label);
    });
    field.append(row);
    form.append(field);
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const payload={};
    answer.questions.forEach(q=>{const checked=form.querySelector(`input[name="${q.name}"]:checked`);if(checked)payload[q.name]=checked.value;});
    setBusy(true);
    // 测评等级会改变产品匹配和可投额度，侧栏的账户摘要同源，一并重取。
    try{ renderAnswer(await api('/risk-assessment',payload)); block.remove(); setAgentState('测评完成'); await refresh(); }
    catch(error){showError(error.message);} finally{setBusy(false);}
  });
  const actions=node('div','plan-actions');
  actions.append(button('提交并生成报告',()=>form.requestSubmit(),'primary'));
  form.append(actions);
  block.append(form);
  return block;
}
function renderRiskReport(answer) {
  const block=node('article','analysis-card risk-card risk-report');
  block.append(agentAvatar());
  block.append(node('span','speaker','NEXUS / RISK ASSESSMENT'),node('h3','',answer.title));
  // Below the grade the body is slotted so the layout pass can reorder it.
  const hero=node('section','risk-hero');
  hero.append(node('div','risk-grade',answer.grade),node('div','risk-grade-label',`${answer.grade_label} · ${Number(answer.score).toFixed(1)} 分`));
  hero.append(node('p','risk-verdict',answer.verdict));
  const stamp=node('div','risk-stamp');
  stamp.append(node('span','',`最高可适配 ${answer.max_product_risk}`));
  if(answer.valid_until) stamp.append(node('span','',`有效期至 ${answer.valid_until}`));
  hero.append(stamp);
  const prudence=node('section','risk-prudence');
  prudence.append(node('small','','审慎原则'),node('p','',answer.prudence.binding_reason));
  const halves=node('div','risk-halves');
  [['客观承受能力',answer.objective.score,answer.prudence.objective_grade],['主观风险偏好',answer.subjective.score,answer.prudence.subjective_grade]]
    .forEach(([label,score,grade])=>{const item=node('div','risk-half');item.append(node('small','',label),node('strong','',grade),node('span','',`${Number(score).toFixed(1)} 分`));halves.append(item);});
  prudence.append(halves);
  const check=analysisSection('分项体检报告');
  const list=node('div','risk-checklist');
  answer.checklist.forEach(item=>{
    const row=node('div','risk-check');
    const head=node('div','risk-check-head');
    head.append(node('b','',item.name),node('strong','',item.value),node('span',`risk-rating ${item.rating}`,item.rating));
    row.append(head,scoreBar(item.score,item.max),node('p','risk-basis',item.basis));
    list.append(row);
  });
  check.append(list);
  if((answer.warnings||[]).length){
    const warn=analysisSection('风险提示');
    const wrap=node('div','risk-warnings');
    answer.warnings.forEach(item=>{const row=node('div',`risk-warning ${item.level}`);row.append(node('b','',item.title),node('p','',item.detail));wrap.append(row);});
    warn.append(wrap);
  }
  const plan=analysisSection('资产配置建议');
  const buckets=node('div','risk-allocation');
  answer.allocation.forEach(item=>{
    const row=node('div','risk-bucket');
    const head=node('div');head.append(node('b','',item.name),node('span',`badge ${item.max_risk}`,`最高 ${item.max_risk}`));
    const bar=node('div','risk-bar');const track=node('i');track.style.width=`${item.weight}%`;bar.append(track,node('b','',`${item.weight}%`));
    row.append(head,bar);buckets.append(row);
  });
  plan.append(buckets);
  const advice=node('section','risk-advice');
  advice.append(node('small','',answer.advice.label),node('p','',answer.advice.detail));
  const forbid=node('div','risk-forbid');forbid.append(node('b','','理财禁忌'),node('p','',`不建议配置：${answer.advice.forbid}`));
  advice.append(forbid);
  plan.append(advice);
  const slots=[
    {name:'hero',label:'评级',el:hero},
    {name:'prudence',label:'审慎原则',el:prudence},
    {name:'checklist',label:'分项体检',el:check},
  ];
  if((answer.warnings||[]).length) slots.push({name:'warnings',label:'风险提示',el:warn});
  slots.push({name:'allocation',label:'资产配置',el:plan},{name:'advice',label:'配置建议与禁忌',el:advice});
  return compose(block, answer, slots);
}
// Keep customer feedback beneath the answer; presentation and evidence stay internal.
const RATED_TYPES = new Set([
  'account_snapshot','bill_analysis','risk_report','recurring_detection',
  'product_catalog','risk_intake','agent_response','financial_analysis','card_balances',
]);
function emit(answer, block, opts) {
  opts = opts || {};
  const rating = RATED_TYPES.has(answer.type) ? ratingRow(answer.request_id) : null;
  if (rating) {
    const footer = node('footer','answer-feedback');
    footer.append(rating);
    const content = block.querySelector('.agent-response-content');
    (content || block).append(footer);
  }
  if (opts.old) opts.old.replaceWith(block); else $('messages').append(block);
  if (answer.action_id) { seenActions.set(answer.action_id, block); actionVersions.set(answer.action_id, JSON.stringify(answer)); }
  const viewport=$('messages');
  viewport.scrollTop+=block.getBoundingClientRect().top-viewport.getBoundingClientRect().top-16;
  return block;
}
function renderAnswer(answer) {
  if(answer.action_id && clearedActionIds.has(answer.action_id))return;
  const version = JSON.stringify(answer);
  // The dedupe has to run BEFORE the step-up render. Clicking 确认执行 asks the
  // server for the same action again, so without this every click appended
  // another identical 核验 card and the user chased a stack of them.
  if (answer.action_id && actionVersions.get(answer.action_id) === version) return;
  // Resolved before any branch, because the step-up render replaces the block
  // it is superseding rather than appending a second one.
  const old = answer.action_id && seenActions.get(answer.action_id);
  if (answer.type === 'step_up') {
    // Replace, don't append. 确认执行 turns the same action into its second
    // factor; without {old} the verification card landed *below* the summary
    // card, leaving two live blocks for one action. The customer cancelled on
    // the one that was no longer wired up and concluded the app was broken.
    return emit(answer, renderStepUp(answer), {old});
  }
  if (answer.type === 'financial_intake') {
    return emit(answer, renderFinancialIntake(answer));
  }
  if (answer.type === 'risk_intake') {
    return emit(answer, renderRiskIntake(answer));
  }
  if (answer.type === 'risk_report') {
    return emit(answer, renderRiskReport(answer));
  }
  if (answer.type === 'financial_analysis') {
    return emit(answer, renderAnalysis(answer));
  }
  if (answer.type === 'bill_analysis') {
    return emit(answer, renderBillAnalysis(answer));
  }
  if (answer.type === 'external_data') {
    return emit(answer, renderExternalData(answer));
  }
  if (answer.type === 'external_macro') {
    return emit(answer, renderMacroData(answer));
  }
  if (answer.type === 'sec_filings') {
    return emit(answer, renderSecFilings(answer));
  }
  if (answer.type === 'card_balances') return emit(answer, renderCardBalances(answer));
  if (answer.type === 'account_snapshot') {
    return emit(answer, renderAccountSnapshot(answer));
  }
  if (answer.type === 'product_catalog') {
    return emit(answer, renderProductCatalog(answer));
  }
  if (answer.type === 'recurring_detection') {
    return emit(answer, renderRecurringDetection(answer));
  }
  if (answer.type === 'scheduled_transfer_list') {
    return emit(answer, renderScheduledTransfers(answer));
  }
  if (answer.type === 'birthday_intake') {
    return emit(answer, renderBirthdayIntake(answer));
  }
  if (answer.type === 'cross_scene_plan') {
    return emit(answer, renderCrossScenePlan(answer));
  }
  if (answer.type === 'universal_plan') {
    return emit(answer, renderUniversalPlan(answer));
  }
  if (answer.type === 'chat') {
    // A greeting is an ordinary reply, not a boundary event: it must not wear
    // the BOUNDARY badge, and it must not claim a capability check ran.
    const block=node('article','assistant-message');
    const content=node('div');
    content.append(node('span','speaker','NEXUS · AI 银行管家'),node('p','',answer.message||''));
    block.append(node('span','agent-avatar','N'),content);
    if (answer.actions?.length) {
      const actions=node('div','plan-actions');
      answer.actions.forEach(item=>actions.append(button(item.label,()=>send(item.command),item.tone==='primary'?'primary':'')));
      block.append(actions);
    }
    return emit(answer, block);
  }
  if (['boundary','support_handoff','interruption','risk_assistance','slot_request'].includes(answer.type)) {
    return emit(answer, renderBoundaryFlow(answer));
  }
  if (answer.type === 'aa_collection_list') {
    return emit(answer, renderAACollections(answer));
  }
  if (answer.type === 'agent_response') {
    const block=node('article','assistant-message agent-response-card');
    const content=node('div','agent-response-content');
    content.append(node('span','speaker','NEXUS · AI 银行管家'),node('h3','',answer.title || 'Nexus 智能分析'));
    content.append(window.NexusRichText.render(answer.message));
    block.append(node('span','agent-avatar','N'),content); return emit(answer, block);
  }
  const block = node('article', answer.type === 'confirmation' ? 'confirmation' : answer.type === 'receipt' ? 'receipt' : 'assistant-message');
  let messageContent=null;
  if (answer.action_id) block.dataset.actionId = answer.action_id;
  if (answer.type === 'confirmation') {
    const label = node('div','eyebrow'); label.append(node('span','','等待你的确认'));
    block.append(agentAvatar(),label,node('h3','',answer.title));
    const fields = moneyFieldList(answer);
    block.append(fields);
    const actions = node('div','confirm-actions');
    const ttl = countdownBar(() => {
      // 过期后按钮留着但不可点：让客户看到"我刚才那笔要重来"，
      // 而不是对着一个点了没反应的按钮猜原因。
      actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
      const editToggle = block.querySelector('[data-act="edit"]');
      if (editToggle) editToggle.disabled = true;
      setAgentState('确认已过期 · 未执行');
      // 过期不是死路。给一条能走的路，否则客户只能刷新页面重说一遍。
      if (!block.querySelector('.expired-note')) {
        block.append(node('p','expired-note','这笔没有执行，钱没有动。修正信息后可以重新发起。'));
      }
    }, answer.expires_at);
    block.append(ttl.wrap);
    const decide = async (decision) => {
      if (busy) return;
      setBusy(true); showError(''); setAgentState(decision==='confirm'?'正在执行并记录':'正在取消计划','executing');
      try {
        const result = await api(`/actions/${answer.action_id}/${decision}`, {}, 'POST');
        ttl.stop();
        renderAnswer(result);
        setAgentState(decision==='confirm'?'执行完成':'计划已取消');
        await refresh();
      } catch(error) {showError(error.message);setAgentState('执行失败');}
      finally {setBusy(false);}
    };
    const canEdit = (answer.editable || []).length > 0;
    if (canEdit) {
      const toggle = button('修改这笔', () => {
        const existing = block.querySelector('.action-edit-wrap');
        if (existing) { existing.remove(); toggle.textContent = '修改这笔'; return; }
        // 编辑和确认/取消互斥：两个按钮同时可点，等于给了一个"边改边确认"的口子。
        actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
        ttl.stop();
        const editor = actionEditor(answer, (updated) => {
          renderAnswer(updated);
          setAgentState('已按你的修改更新 · 等你确认');
        });
        block.insertBefore(editor, actions);
        toggle.textContent = '收起修改';
      });
      toggle.dataset.act = 'edit';
      actions.append(toggle);
    }
    actions.append(button('确认执行',()=>decide('confirm'),'primary'),button('取消',()=>decide('cancel')));
    block.append(actions);
  } else if (answer.type === 'receipt') {
     if (answer.chart) { const chart=renderSplitChart(answer.chart); if(chart) block.append(chart); }
    block.append(agentAvatar(),node('div','eyebrow','✓  执行回执'),node('h3','',answer.title));
    block.append(renderFieldList(answer.detail));
    block.append(node('div','reference',`回执 ${answer.reference || answer.action_id.slice(0,8).toUpperCase()}`));
  } else {
    messageContent=node('div');
    messageContent.append(node('span','speaker','NEXUS · AI 银行管家'),node('p','',answer.message));
    block.append(node('span','agent-avatar','N'),messageContent);
  }
  // 拒绝类回答的正文已经说清了"为什么不行"，再补一个安全检查标签只是复述。
  // 其余场景的标签仍然如实标注实际发生的处理——确认卡、拦截、模型降级都需要。
  const quiet = QUIET_REFUSALS.has(answer.category) || (answer.type === 'message' && answer.engine === 'policy');
  if (answer.engine && !quiet) {
    // clarify 的标签只说"确实还差东西"，不替任何核验背书。说"已核验你给的信息"
    // 等于在还没确认缺什么之前先宣称核过了；一旦这张卡问错，标签会把错话坐实。
    const labels = {memory:'明确偏好 · 可以查看、更正和清除', model:'当前模型 理解 · 后端校验', rules:'明确指令 · 本地工具', policy:'服务范围与安全检查', write:'已核验收款人 · 金额 · 限额 · 等你确认', clarify:'还差一点信息 · 我没有替你猜', 'external-tool':'公开外部数据 · 本地换算', compliance:'未通过发送前合规检查 · 已拦截', fallback:'模型暂不可用 · 未创建操作'};
    (messageContent||block).append(node('p','demo-note',labels[answer.engine] || '后端校验'));
  }
  return emit(answer, block, {old});
}
async function send(text) {
  text = text.trim(); if (!text || busy) return;
  setBusy(true); showError('');
  // A network retry reuses its request ID, so it cannot create another plan.
  const previous = retryMessage && retryMessage.message === text;
  const payload = previous ? retryMessage : {message:text,request_id:crypto.randomUUID()};
  retryMessage=payload;
  if (!previous) $('messages').append(node('article','user-message',text));
  $('message').value=''; scrollMessages();
  setAgentState('正在理解与核验','thinking');
  showThinkingBubble();
  try {
    const answer=await api('/messages',payload);
    retryMessage=null; hideThinkingBubble(); renderAnswer(answer); setAgentState(answer.type==='confirmation'?'等待你的确认':'准备就绪',answer.type==='confirmation'?'confirm':'ready'); await refresh();
  } catch(error) {
    // Retry reuses the same request_id, so a network failure can never turn
    // into a second plan. What the customer needs to hear is that nothing
    // happened to their money, not just that a request failed.
    hideThinkingBubble();
    $('message').value=text;
    const failed = failureCard(error.message, () => { showError(''); send(text); });
    $('messages').append(failed); scrollMessages();
    showError(error.message);
  }
  finally {setBusy(false); $('message').focus(); if(document.body.classList.contains('agent-thinking')) setAgentState('需要你重试');}
}
function renderOverview(data) {
  const totalAvailable = data.accounts.reduce((sum,a)=>sum+Number(a.available),0);
  const totalReserved = data.accounts.reduce((sum,a)=>sum+Number(a.reserved),0);
  const bal=$('balance'); if (bal) bal.textContent = money(totalAvailable);
  const res=$('reserved'); if (res) res.textContent = `¥ ${money(totalReserved)}`;
  const navBal=$('nav-balance'); renderMoney(navBal,totalAvailable);
  $('cards').replaceChildren();
  for (const c of data.cards) {
    const card=node('article','bank-card');
    const top=node('div','bank-card-top');top.append(node('b','',c.name),node('span','chip'));
    const bottom=node('div','bank-card-bottom');bottom.append(node('span','',`•••• ${c.last4}`),node('span',`badge ${c.status==='ACTIVE'?'':'inactive'}`,states[c.status]||c.status));
    card.append(top,bottom);
    if(c.available!=null){const balance=node('div','side-card-balance');balance.append(node('small','','关联账户可用余额'),node('strong','',`¥${money(c.available)}`));card.append(balance);if(c.shared_account)card.append(node('p','side-card-shared','与其他卡共用账户 · 不重复计入总额'));}
    card.append(node('p','card-limits',`单笔 ¥${money(c.single_limit)} · 每日 ¥${money(c.daily_limit)}`));
    const controls=node('div','mini-actions');
    if (c.status==='ACTIVE') controls.append(button('临时锁定',()=>send(`锁定尾号${c.last4}`),'action-warning'));
    if (c.status==='TEMP_LOCKED') controls.append(button('解锁卡片',()=>send(`解锁尾号${c.last4}`),'action-positive'));
    if (['ACTIVE','TEMP_LOCKED'].includes(c.status)) controls.append(button('挂失',()=>send(`挂失尾号${c.last4}`),'action-danger'));
    card.append(controls);$('cards').append(card);
  }
  $('subscriptions').replaceChildren();
  // A cancelled contract leaves the active list — keeping it there would make
  // the side panel disagree with what the user just confirmed in chat. Anything
  // still holding a mandate stays visible, because money can still move.
  const subs = data.subscriptions.filter(s=>s.contract_status!=='TERMINATED'&&s.contract_status!=='CANCELLED'||s.mandate_status==='ACTIVE');
  for (const s of subs) {
    const closed = s.contract_status==='TERMINATED'||s.contract_status==='CANCELLED';
    const item=node('article',`subscription${closed?' sub-closed':''}`), top=node('div','sub-top');
    const price=node('span','sub-price',money(s.amount));price.append(node('small','',' / 月'));
    top.append(node('span','sub-icon',s.merchant_name.slice(0,1)),node('span','sub-name',s.merchant_name),price);
    item.append(top,node('p','sub-state',`合同 ${states[s.contract_status]||s.contract_status||'无'} · 代扣 ${states[s.mandate_status]||s.mandate_status||'无'}${s.next_charge_at?` · 预计续费 ${s.next_charge_at.slice(0,10)}`:''}`));
    const controls=node('div','mini-actions');
    if (s.status==='ACTIVE'&&!closed) controls.append(button('取消订阅',()=>send(`取消${s.merchant_name}订阅`),'action-warning'));
    if (s.mandate_status==='ACTIVE') controls.append(button('撤销代扣',()=>send(`撤销${s.merchant_name}代扣`),'action-danger'));
    item.append(controls);$('subscriptions').append(item);
  }
  const scheduleList=$('scheduled-transfers');
  if (scheduleList) {
    scheduleList.replaceChildren();
    if (!data.scheduled_transfers.length) scheduleList.append(node('p','muted','暂无定时转账计划。'));
    data.scheduled_transfers.forEach(item=>{const row=node('article','subscription schedule-mini');const top=node('div','sub-top');top.append(node('span','sub-icon','↻'),node('span','sub-name',`${item.recipient} · 每月${item.day_of_month}日`),node('span','sub-price',`¥${money(item.amount)}`));row.append(top,node('p','sub-state',`${states[item.status]||item.status} · ${item.purpose} · 下次 ${item.next_run_at.slice(0,10)}`));if(item.status==='ACTIVE'){const controls=node('div','mini-actions');controls.append(button('暂停计划',async()=>{setBusy(true);showError('');try{await api(`/scheduled-transfers/${item.id}/pause`,{});await refresh();}catch(error){showError(error.message);}finally{setBusy(false);}},'action-warning'));row.append(controls);}scheduleList.append(row);});
  }
  const aaList=$('aa-collections');
  if (aaList) {
    aaList.replaceChildren();if(!data.aa_collections.length)aaList.append(node('p','muted','暂无 AA 收款任务。'));
    data.aa_collections.forEach(item=>{const row=node('article','subscription');const top=node('div','sub-top');top.append(node('span','sub-icon','A'),node('span','sub-name',`AA #${item.id}`),node('span','sub-price',`¥${money(item.total)}`));row.append(top,node('p','sub-state',`${item.purpose} · ${item.participant_count} 人 · 每人 ¥${money(item.per_person)}`));aaList.append(row);});
  }
  if (data.transactions.length) {
    $('transactions').replaceChildren();
    for (const t of data.transactions) {
      const item=node('div','transaction'),top=node('div','transaction-top');
      top.append(node('span','',`转账 #${t.id}`),node('strong','',`${t.status==='REVERSED'?'↩':t.direction==='out'?'−':'+'} ${money(t.amount)}`));
      item.append(top,node('small','',`${states[t.status]||t.status} · ${t.time.replace('T',' ').slice(5,16)}`));$('transactions').append(item);
    }
  }
  $('audit').replaceChildren();
  for (const a of data.audit) {
    const item=node('li');item.append(node('p','',auditLabels[a.action]||a.action),node('small','',`#${a.id} · ${a.time.replace('T',' ').slice(5,19)}`));$('audit').append(item);
  }
  const railActivity = $('rail-activity');
  if (railActivity) {
    railActivity.replaceChildren();
    const recent = data.transactions.slice(0, 3);
    if (!recent.length) {
      railActivity.append(node('li', 'muted', '近 7 天暂无新交易'));
    } else {
      for (const t of recent) {
        const li = node('li', t.direction === 'out' ? 'out' : 'in');
        if (t.status === 'REVERSED') li.className = 'reversed';
        const main = node('div', 'act-main');
        main.append(
          node('span', 'act-name', t.remark || `转账 #${t.id}`),
          node('small', 'act-time', `${t.time.replace('T', ' ').slice(5, 16)}`)
        );
        const sign = t.status === 'REVERSED' ? '↩' : t.direction === 'out' ? '−' : '+';
        const amt = node('span', 'act-amount', `${sign} ¥${money(t.amount)}`);
        li.append(main, amt);
        railActivity.append(li);
      }
    }
  }
  if (data.goal) {
    const navGoalValues=$('nav-goal-values'); if (navGoalValues) navGoalValues.textContent=`¥${money(data.goal.saved)} / ¥${money(data.goal.amount)}`;
    const navGoalPercent=$('nav-goal-percent'); if (navGoalPercent) navGoalPercent.textContent=`${data.goal.progress_pct}%`;
    const navGoalBar=$('rail-goal-bar'); if (navGoalBar) navGoalBar.value=data.goal.progress_pct;
    const oldGoalName=$('goal-name'); if (oldGoalName) oldGoalName.textContent=data.goal.name;
    const oldGoalValues=$('goal-values'); if (oldGoalValues) oldGoalValues.textContent=`¥${money(data.goal.saved)} / ¥${money(data.goal.amount)}`;
    const oldGoalPercent=$('goal-percent'); if (oldGoalPercent) oldGoalPercent.textContent=`${data.goal.progress_pct}%`;
    const oldGoalProgress=$('goal-progress'); if (oldGoalProgress) oldGoalProgress.value=data.goal.progress_pct;
    const oldGoalHorizon=$('goal-horizon'); if (oldGoalHorizon) oldGoalHorizon.textContent=`计划期限 ${data.goal.horizon_months} 个月 · 根据你的理财资料计算`;
  } else {
    const oldGoalName=$('goal-name'); if (oldGoalName) oldGoalName.textContent='尚未设置目标';
    const navGoalValues=$('nav-goal-values'); if (navGoalValues) navGoalValues.textContent='完成理财资料后显示';
    const navGoalPercent=$('nav-goal-percent'); if (navGoalPercent) navGoalPercent.textContent='—';
    const navGoalBar=$('rail-goal-bar'); if (navGoalBar) navGoalBar.value=0;
  }
  // Expired plans remain in the audit log, but repeating one message per old
  // confirmation makes a returning user think the current session is broken.
  for (const action of data.actions) {
    if (action.type === 'message' && action.message === '确认已过期，请重新发起。') continue;
    renderAnswer(action);
  }
}
function renderRailSummary(data) {
  if (!data || data.empty) return;
  const navSpend = $('nav-spend');
  const navSpendCount = $('nav-spend-count');
  renderMoney(navSpend,data.summary.total.replace(/[¥,]/g, ''));
  if (navSpendCount) {
    const top = data.categories[0];
    navSpendCount.textContent = top
      ? `${data.summary.transaction_count} 笔 · ${top.name} ${top.share_pct.toFixed(1)}%`
      : `${data.summary.transaction_count} 笔`;
  }
  const navAlertAmount = $('nav-alert-amount');
  const navAlertMerchant = $('nav-alert-merchant');
  if (navAlertAmount && data.anomalies.length) {
    const item = data.anomalies[0];
    renderMoney(navAlertAmount,item.amount.replace(/[¥,]/g, ''));
    if (navAlertMerchant) navAlertMerchant.textContent = `${item.merchant} · 需核对`;
  } else if (navAlertMerchant) {
    renderMoney(navAlertAmount,0);
    navAlertMerchant.textContent = '本月暂无异常';
  }
}
function renderMemory(data) {
  const target=$('user-memory'); if(!target)return;
  target.replaceChildren();
  if(data.status==='untrusted'){target.append(node('p','muted','记忆签名校验未通过，已停止使用。'));return;}
  target.append(node('p','side-note',data.summary||'还没有记录明确的偏好。你可以告诉我你的目标、资金使用习惯和希望的回答方式。'));
  for(const fact of data.facts||[]) {
    const item=node('article','subscription-item');
    item.append(node('b','',`${fact.label} · ${fact.value}`),node('p','side-note',`你说：${fact.evidence}`),node('small','muted',`有效至 ${fact.valid_until.slice(0,10)}`));target.append(item);
  }
  target.append(node('p','side-note',`记忆版本 ${data.version} · 已压缩 ${data.compression_count} 次 · 近期 ${data.recent_count} 轮`));
}
async function refresh() {
  const [overview,bills,memory]=await Promise.allSettled([api('/overview'),api('/bill-analysis?period=month'),api('/memory')]);
  if(overview.status==='rejected')throw overview.reason;
  renderOverview(overview.value);
  const warnings=[];
  if(bills.status==='fulfilled')renderRailSummary(bills.value);
  else {warnings.push('消费分析暂未连接');$('nav-spend-count').textContent='暂时无法读取，点击刷新重试';}
  if(memory.status==='fulfilled')renderMemory(memory.value);
  else {warnings.push('记忆功能暂未连接');$('user-memory').replaceChildren(node('p','muted','记忆暂未连接。其他账户功能可继续使用；若刚更新程序，请重启 Nexus 服务。'));}
  if(warnings.length)showError(warnings.join('；')+'。其他账户功能可继续使用。');
  return warnings;
}
$('clear-memory').addEventListener('click',async()=>{if(busy)return;try{await api('/memory/reset',{});await refresh();}catch(error){showError(error.message);}});
$('verify-audit').addEventListener('click',async()=>{if(busy)return;try{const result=await api('/audit/verify');$('audit-verification').textContent=result.ok?`验证通过：${result.checked} 条记录；独立锚点覆盖至 ${result.anchored_sequence||0}。`:`验证未通过（${result.status}），请检查记录与独立锚点。`;}catch(error){showError(error.message);}});
$('composer').addEventListener('submit',e=>{e.preventDefault();send($('message').value);});
$('clear-conversation').addEventListener('click',()=>{
  if(busy)return;
  hideThinkingBubble();
  for(const id of seenActions.keys())clearedActionIds.add(id);
  let clearWarning = '';
  try { persistClearedActionIds(); }
  catch { clearWarning = '当前浏览器无法保存清空记录；请允许本地存储，否则刷新后旧卡片可能再次出现。'; }
  seenActions.clear();actionVersions.clear();retryMessage=null;
  const welcome=node('article','assistant-message welcome-message');
  const content=node('div');content.append(node('span','speaker','NEXUS · AI 银行管家'),node('p','','对话已清空，可以重新输入问题。已保存的偏好仍保留。'));
  welcome.append(node('span','agent-avatar','N'),content);
  $('messages').replaceChildren(welcome);$('messages').scrollTop=0;
  $('message').value='';showError(clearWarning);setAgentState('可以开始新对话');$('message').focus();
});
document.querySelectorAll('[data-prompt]').forEach(b=>b.addEventListener('click',()=>send(b.dataset.prompt)));
document.querySelectorAll('[data-focus]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.focus).focus()));
const capabilityToggle=$('capability-toggle');
if (capabilityToggle) capabilityToggle.addEventListener('click',()=>{
  const board=capabilityToggle.closest('.capability-board');
  const expanded=board.classList.toggle('expanded');
  capabilityToggle.setAttribute('aria-expanded',String(expanded));
  capabilityToggle.textContent=expanded?'收起 ↑':'查看全部服务 ↓';
});
$('refresh').addEventListener('click',async()=>{
  if (busy) return;
  setBusy(true); showError('');
  try {if(accountReady)await refresh();else await start();} catch(error) {showError(error.message);} finally {setBusy(false);}
});
let accountReady=false;
async function start() {
  setBusy(true);
  try {
    const user=await api('/session',{});
    demoPasscode=user.demo_passcode||'';
    const [health,catalog,account]=await Promise.allSettled([api('/health'),api('/capabilities'),refresh()]);
    if(account.status==='rejected')throw account.reason;
    accountReady=true;setAgentState('账户已就绪');
    if(catalog.status==='fulfilled')renderToolStatus(catalog.value,health.status==='fulfilled'?health.value:{});
    else {const badge=$('model-badge');if(badge)badge.textContent='账户已接入';$('tool-status').replaceChildren(node('li','muted','工具状态暂时无法读取，请重新加载页面。'));}
    const welcome=$('welcome-copy'); if (welcome) welcome.textContent='账户已经接上了。说说你想办什么——查一查、算一算，或者要动钱，我都会先跟你确认。';
  }
  catch(error){accountReady=false;showError(`Agent 启动失败：${error.message}`);setAgentState('连接失败');const welcome=$('welcome-copy');if(welcome)welcome.textContent=`账户连接未完成：${error.message} 请启动服务后点击刷新，或重新加载页面。`;const badge=$('model-badge');if(badge)badge.textContent='连接未完成';}
  finally {setBusy(false);}
}

// ---------------------------------------------------------------- 登录闸门
// 本地开发不需要登录，服务会直接说 required=false。公网上多了一道门：没有
// 有效会话，服务端对所有 /api/* 直接 401。
//
// 登录是**独立页面** /login，不是盖在主界面上的浮层。旧浮层有两个实打实的
// 问题：登录成功后 gate.remove() 删了浮层，却没摘 body 上的 login-locked，
// style.css 给 .app-shell 的 blur(6px) 于是永远生效——登录进去整个界面是虚的；
// 而且退出登录只能 reload 回同一个浮层。改成整页跳转后，"浮层盖住主界面"
// 这个状态根本不存在。
let signedInName='';
function goToLogin() {
  // replace 而不是赋值：登录页不该留在"后退"历史里。
  location.replace('/login');
}
async function authGate() {
  try {
    const state=await api('/auth/state');
    // 只有需要登录的环境才显示"退出"——本地沙箱没有会话可言，挂了反而误导。
    const exit=$('logout'); if(exit) exit.hidden=!state.required;
    if(!state.required){return start();}
    // 公网模式下默认走 /login：浏览器里残留的旧 Cookie 不该让人"直接进来"。
    // 例外：URL 带 ?via=login 跳过来时，这一次放行进 app。
    // 这个标记只由登录页的"继续"按钮和表单提交使用；authGate 立刻把它
    // 清掉，避免循环 / 留痕。
    const params=new URLSearchParams(location.search);
    if(state.authenticated && params.get('via')==='login'){
      signedInName=state.name||'';
      history.replaceState(null,'','/');
      return start();
    }
    goToLogin();
  } catch(error) {
    showError(`无法连接服务：${error.message}`);
  }
}
authGate();

// A native dialog gives long financial reports room and manages keyboard focus.
const reportDialog=node('dialog','report-dialog');reportDialog.setAttribute('aria-label','金融报告阅读');document.body.append(reportDialog);
let reportPlaceholder=null;
$('report-view').addEventListener('click',()=>{
  if(reportDialog.open){reportDialog.close();return;}
  const zone=document.querySelector('.conversation-zone');reportPlaceholder=document.createComment('conversation');zone.before(reportPlaceholder);reportDialog.append(zone);$('report-view').textContent='退出阅读';reportDialog.showModal();const viewport=$('messages'),latest=viewport.lastElementChild;if(latest)viewport.scrollTop+=latest.getBoundingClientRect().top-viewport.getBoundingClientRect().top-12;
});
reportDialog.addEventListener('close',()=>{const zone=reportDialog.querySelector('.conversation-zone');if(zone&&reportPlaceholder){reportPlaceholder.replaceWith(zone);reportPlaceholder=null;}$('report-view').textContent='展开阅读';$('report-view').focus({preventScroll:true});});

// 退出：服务端删会话，浏览器清 Cookie。只清 Cookie 是不够的——令牌还在数据库里，
// 换个浏览器照样能用。
$('logout').addEventListener('click',async()=>{
  if(busy)return;
  setBusy(true);
  try{
    await api('/auth/logout',{},'POST');
  }catch(error){
    // 令牌本来就没了也算退出成功——目标状态已经达成。
  }finally{
    setBusy(false);
    // 回登录页而不是 reload：reload 只会把主界面再画一遍，然后被 authGate
    // 弹回 /login，白闪一次。
    location.replace('/login');
  }
});
