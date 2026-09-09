'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const number = (n) => Number(n || 0).toLocaleString('zh-CN');
const state = { dataset: '', files: [], view: 'search', page: 1, tracePage: 1, lastSearch: null, lastTrace: null, rows: new Map(), searchSerial: 0, traceSerial: 0, refreshSerial: 0, correlation: null };
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4500); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function paramsURL(params) { return new URLSearchParams(Object.entries(params).filter(([,v]) => v !== '' && v != null)).toString(); }
function needDataset() { if (!state.dataset) { toast('请先导入并选择一个日志包'); return false; } return true; }
function highlight(raw, keyword) {
  if (!keyword) return escapeHTML(raw);
  const sensitive = state.lastSearch?.case === '1';
  const text = sensitive ? raw : raw.toLowerCase();
  const query = sensitive ? keyword : keyword.toLowerCase();
  let out = '', offset = 0, match;
  while ((match = text.indexOf(query, offset)) >= 0) {
    out += escapeHTML(raw.slice(offset, match)) + '<mark>' + escapeHTML(raw.slice(match, match + keyword.length)) + '</mark>';
    offset = match + keyword.length;
  }
  return out + escapeHTML(raw.slice(offset));
}
const viewMeta = {
  search:['全局搜索','每一条日志，都有迹可循。','跨节点搜索，从接口请求一路定位到异常现场。'],
  trace:['流水号追踪','把一次请求，完整串起来。','跨 Pod 汇集同一流水号，按时间还原请求过程。'],
  files:['日志文件','每个节点，每份日志。','查看解析到的原始文件及其完整压缩包来源。'],
  ai:['AI 问诊','从日志线索，到问题原因。','基于实际检索证据分析；由你决定何时连接模型。']
};
function setView(view) {
  state.view = view;
  $$('.nav').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  Object.keys(viewMeta).forEach(v => $('#' + v + 'View').hidden = v !== view);
  const [label,title,description] = viewMeta[view];
  $('#viewLabel').textContent=label; $('#viewTitle').textContent=title; $('#viewDescription').textContent=description;
  if (view === 'files') renderFiles();
}
function empty(container, title, message, importButton=false) {
  container.innerHTML = `<div class="empty"><span class="empty-icon">⌕</span><h2>${escapeHTML(title)}</h2><p>${escapeHTML(message)}</p>${importButton ? '<button class="primary" data-action="upload">＋ 导入第一个日志包</button>' : ''}</div>`;
}
function clearResults() {
  state.lastSearch = null; state.lastTrace = null; state.rows.clear(); state.searchSerial++; state.traceSerial++;
  state.correlation = null; $('#correlation').hidden = true;
  empty($('#searchResults'), state.dataset ? '准备好，从一个关键词开始' : '无需解压，直接开始', state.dataset ? '输入接口、异常关键字或时间片段，搜索全部节点；也可以直接搜索查看所有日志。' : '上传包含多个节点 ZIP 的日志包，自动解析 Pod 下的明文与 GZIP 日志。', !state.dataset);
  empty($('#traceResults'),'输入流水号，查看请求时间线','流水号按原始字符串匹配，长数字不会丢失精度。');
}
async function refreshDatasets(selectId) {
  const serial = ++state.refreshSerial;
  const datasets = await api('/api/datasets');
  if (serial !== state.refreshSerial) return;
  const ready = datasets.filter(d => d.state === 'ready');
  const previous = state.dataset;
  if (selectId && ready.some(d => d.id === selectId)) state.dataset = selectId;
  else if (!ready.some(d => d.id === state.dataset)) state.dataset = ready[0]?.id || '';
  $('#dataset').innerHTML = ready.length ? ready.map(d => `<option value="${d.id}">${escapeHTML(d.name)}</option>`).join('') : '<option value="">尚未导入日志包</option>';
  $('#dataset').value = state.dataset;
  const current = ready.find(d => d.id === state.dataset);
  $('#datasetInfo').textContent = current ? `${number(current.files)} 份日志文件 · ${number(current.records)} 条记录` : '上传外层 ZIP，自动检索所有节点。';
  const pending = datasets.find(d => d.state === 'importing');
  const failed = datasets.find(d => d.state === 'failed');
  const status = $('#importStatus');
  status.classList.remove('failed');
  if (pending) {
    status.hidden = false;
    status.textContent = `正在解析 ${pending.name} … 已读取 ${number(pending.progress?.files)} 个文件、${number(pending.progress?.records)} 条记录。完成后将自动切换。`;
  } else if (failed && datasets[0]?.id === failed.id && !selectId) {
    status.hidden = false; status.classList.add('failed'); status.textContent = `导入失败：${failed.name} — ${failed.error}。已有日志包仍可使用。`;
  } else if (current?.warnings.length) {
    status.hidden = false; status.textContent = '导入提示：' + current.warnings.join('；');
  } else status.hidden = true;
  if (previous !== state.dataset || (state.dataset && !state.files.length)) {
    state.files = state.dataset ? await api('/api/files?dataset=' + state.dataset) : [];
    if (serial !== state.refreshSerial) return;
    updateFilters(); clearResults(); renderFiles();
  }
  return datasets;
}
function options(id, values, label) {
  const element = $('#' + id), old = element.value;
  element.innerHTML = `<option value="">${label}</option>` + [...new Set(values)].sort().map(v => `<option value="${escapeHTML(v)}">${escapeHTML(v)}</option>`).join('');
  if (values.includes(old)) element.value=old;
}
function updateFilters() {
  options('node',state.files.map(f => f.node),'全部节点');
  const candidates = state.files.filter(f => !$('#node').value || f.node === $('#node').value);
  // Encode namespace separately so identical pod names in different namespaces stay distinct.
  options('pod',candidates.map(f => f.namespace + '/' + f.pod),'全部 Pod');
  options('kind',candidates.filter(f => !$('#pod').value || f.namespace + '/' + f.pod === $('#pod').value).map(f => f.kind),'全部类型');
  $('#scopeText').textContent = `当前范围：${$('#node').value || '全部节点'} · ${$('#pod').value || '全部 Pod'} · ${$('#kind').value || '全部日志'}`;
}
function searchParams() {
  const params = Object.fromEntries(new FormData($('#searchForm')));
  if (params.pod) { const slash=params.pod.indexOf('/'); params.namespace=params.pod.slice(0,slash); params.pod=params.pod.slice(slash+1); }
  for (const key of ['start','end']) if (params[key]) { if (params[key].length===16) params[key]+=':00'; params[key] = params[key].replace('T',' ') + ' ' + $('#filterOffset').value; }
  return {...params,dataset:state.dataset};
}
function rowHTML(row, keyword, timeline=false) {
  state.rows.set(row.id,row);
  const error = row.status >= 400 || ['ERROR','FATAL'].includes(row.level);
  return `<article class="log-row"><div class="log-meta"><span class="log-time">${escapeHTML(row.time || '未识别时间')}</span><span class="badge ${error ? 'error' : row.level === 'WARN' ? 'warn' : ''}">${escapeHTML(row.level || 'RAW')}</span>${row.status ? `<span class="badge ${row.status>=400?'error':'success'}">${row.status}</span>`:''}<span class="log-pod">${escapeHTML(row.pod)}</span><span class="log-kind">${escapeHTML(row.filename)}</span>${row.duration!=null?`<span>${number(row.duration)} ms</span>`:''}<span class="log-number">L${row.line}${row.end_line>row.line?'–'+row.end_line:''}</span></div><pre class="log-content ${error?'is-error':''}">${highlight(row.raw,keyword)}</pre><p class="source-path"><span>来源</span>${escapeHTML(row.source)}</p><div class="log-actions"><button data-action="context" data-id="${row.id}">查看上下文</button>${row.ts!=null?`<button data-action="correlate" data-id="${row.id}">同 Pod 相邻日志 →</button>`:''}${row.trace && !timeline?`<button data-action="trace" data-id="${row.id}">追踪流水号</button>`:''}<button data-action="copy" data-id="${row.id}">复制原文</button><span class="thread-label">${escapeHTML(row.node)}${row.thread?' · '+escapeHTML(row.thread):''}</span></div></article>`;
}
function metrics(summary, trace=false) {
  return `<div class="metric-grid"><div class="metric"><span class="metric-label">${trace?'流程记录':'匹配日志'} <span>≡</span></span><span class="metric-value">${number(summary.total)}<small>条</small></span></div><div class="metric"><span class="metric-label">命中节点 <span>▦</span></span><span class="metric-value">${number(summary.nodes)}<small>Node / ${number(summary.pods)} Pod</small></span></div><div class="metric"><span class="metric-label">来源文件 <span>▤</span></span><span class="metric-value">${number(summary.files)}<small>份</small></span></div><div class="metric error"><span class="metric-label">异常记录 <span>!</span></span><span class="metric-value">${number(summary.errors)}<small>ERROR / HTTP ≥400</small></span></div></div>`;
}
function renderResults(result, trace=false) {
  const container = trace ? $('#traceResults') : $('#searchResults');
  const pages = Math.max(1, Math.ceil(result.summary.total/result.size));
  const query = trace ? state.lastTrace.trace : state.lastSearch.q;
  const params = trace ? state.lastTrace : state.lastSearch;
  container.innerHTML = metrics(result.summary,trace) + `<div class="results-toolbar"><div><h2>${trace?'请求时间线':'搜索结果'} <span class="subtle">${number(result.summary.total)} 条命中 · ${result.elapsed_ms} ms · 时间正序</span></h2></div><div class="tool-actions">${result.summary.avg_duration!=null?`<span class="subtle">平均 ${Number(result.summary.avg_duration).toFixed(1)} ms</span>`:''}<a class="text-button" href="/api/export?${escapeHTML(paramsURL(params))}">↓ 导出全部</a></div></div>${trace?'<div class="notice">以下是同一流水号的日志时间线；日志未提供父子 Span 关系，因此不推断服务调用拓扑。各节点时钟偏差可能影响先后顺序。</div>':''}<div class="log-list ${trace?'trace-list':''}">${result.rows.map(row => rowHTML(row,query,trace)).join('')}</div><div class="pagination"><span>显示 ${result.summary.total?(result.page-1)*result.size+1:0}–${Math.min(result.page*result.size,result.summary.total)} / ${number(result.summary.total)} 条，无结果截断</span><div class="page-buttons"><button data-action="page" data-trace="${trace}" data-page="${result.page-1}" ${result.page<=1?'disabled':''}>上一页</button><span>${result.page} / ${pages}</span><button data-action="page" data-trace="${trace}" data-page="${result.page+1}" ${result.page>=pages?'disabled':''}>下一页</button><input type="number" min="1" max="${pages}" value="${result.page}" aria-label="跳转页码" data-page-input="${trace}"><button data-action="jump" data-trace="${trace}">跳转</button></div></div>`;
  if (!result.rows.length) empty(container.querySelector('.log-list'),'没有找到匹配日志','试试减少筛选条件，检查节点、日志类型或时间范围。关键词按连续文本匹配。');
}
async function runSearch(page=1, reuse=false) {
  if (!needDataset()) return;
  const serial = ++state.searchSerial;
  if (!reuse) state.lastSearch=searchParams();
  const snapshot = {...state.lastSearch};
  const button=$('#searchForm button[type="submit"]'); button.disabled=true;
  try {
    const result=await api('/api/search?'+paramsURL({...snapshot,page}));
    if (serial !== state.searchSerial || snapshot.dataset !== state.dataset) return;
    state.page=page; renderResults(result);
  } catch(error) { toast(error.message); }
  finally { button.disabled=false; }
}
async function runTrace(page=1,reuse=false) {
  if (!needDataset()) return;
  const trace=$('#traceId').value.trim(); if (!trace) return;
  const serial=++state.traceSerial;
  if (!reuse) state.lastTrace={dataset:state.dataset,trace};
  const snapshot={...state.lastTrace}; const button=$('#traceForm button'); button.disabled=true;
  try {
    const result=await api('/api/search?'+paramsURL({...snapshot,page}));
    if (serial!==state.traceSerial || snapshot.dataset!==state.dataset) return;
    state.tracePage=page; renderResults(result,true);
  } catch(error) { toast(error.message); }
  finally { button.disabled=false; }
}
function localInput(ms) { return new Date(ms + 8*3600000).toISOString().slice(0,23); }
function correlationBanner(row, windowSeconds, sameThread) {
  $('#correlation').hidden=false;
  $('#correlation').innerHTML=`候选关联：${escapeHTML(row.pod)} · ${escapeHTML(row.time)} 前后 ${windowSeconds} 秒${sameThread?' · 同线程':''}。时间与线程可能被复用，请结合流水号确认。<button data-action="toggle-thread">${sameThread?'取消线程限制':'限定同线程'}</button><button data-action="widen">扩大到前后 60 秒</button>`;
}
async function correlate(row, seconds=5, sameThread=false) {
  setView('search'); $('#searchForm').reset();
  $('#node').value=row.node; updateFilters(); $('#pod').value=row.namespace+'/'+row.pod; updateFilters();
  // Root first, run second, otherwise all types. User can adjust freely.
  const kinds=state.files.filter(f => f.node===row.node && f.pod===row.pod && f.namespace===row.namespace).map(f => f.kind);
  $('#kind').value=kinds.includes('root')?'root':kinds.includes('run')?'run':'';
  $('#filterOffset').value='+0800'; $('#start').value=localInput(row.ts-seconds*1000); $('#end').value=localInput(row.ts+seconds*1000);
  $('#thread').value=sameThread?row.thread:''; $('#advanced').open=true;
  state.correlation={row,seconds,sameThread}; correlationBanner(row,seconds,sameThread); updateFilters();
  await runSearch();
}
function renderFiles() {
  if (!state.dataset) return empty($('#fileList'),'还没有日志文件','导入日志包后，这里将显示 Node、Pod、文件名与完整来源。',true);
  const query=$('#fileSearch').value.toLowerCase();
  const files=state.files.filter(f=>[f.source,f.pod,f.node,f.kind].join(' ').toLowerCase().includes(query));
  $('#fileList').innerHTML=files.map(f=>`<article class="file-card"><div class="file-card-header"><span class="log-kind">${escapeHTML(f.kind)}</span><strong>${escapeHTML(f.filename)}</strong><span class="badge">${number(f.records)} 条</span><button class="text-button" data-action="file" data-file="${f.id}">搜索此文件 →</button></div><div class="file-details"><span>Node：${escapeHTML(f.node)}</span><span>Pod：${escapeHTML(f.namespace+'/'+f.pod)}</span><span>Service：${escapeHTML(f.service)}</span></div><p class="source-path">${escapeHTML(f.source)}</p></article>`).join('');
  if(!files.length) empty($('#fileList'),'没有匹配的文件','调整文件名、Pod 或压缩包搜索词。');
}
async function openContext(row) {
  $('#contextSource').textContent=row.source; $('#contextBody').textContent='正在读取上下文…'; $('#contextDialog').showModal();
  try {
    const records=await api('/api/context?id='+row.id);
    $('#contextBody').innerHTML=records.map(r=>`<div class="context-item ${r.id===row.id?'focus':''}"><small>L${r.line}–${r.end_line}${r.id===row.id?' · 当前命中':''}</small><pre class="log-content">${escapeHTML(r.raw)}</pre></div>`).join('');
  } catch(error) { $('#contextBody').textContent=error.message; }
}
async function openSettings() {
  try { const config=await api('/api/ai/config'); $('#baseUrl').value=config.base_url; $('#modelName').value=config.model; $('#keyState').textContent=config.key_ready?'密钥状态：已从环境变量读取':'密钥状态：尚未设置'; $('#configDialog').showModal(); } catch(error) { toast(error.message); }
}
function openUpload() { $('#uploadDialog').showModal(); }
let selectedFile;
function chooseFile(file) { if(!file) return; selectedFile=file; $('#chosenName').textContent=`${file.name} · ${(file.size/1024/1024).toFixed(2)} MB`; }
async function upload(file) {
  if(!file || !file.name.toLowerCase().endsWith('.zip')) return toast('请选择 ZIP 文件');
  const button=$('#uploadSubmit'); button.disabled=true; $('#uploadProgress').textContent='准备上传…';
  try {
    const params={name:file.name,encoding:$('#encoding').value,offset:$('#importOffset').value,unit:$('#durationUnit').value};
    const result=await new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest(); xhr.open('POST','/api/upload?'+paramsURL(params)); xhr.setRequestHeader('Content-Type','application/zip');
      xhr.upload.onprogress=e=>{if(e.lengthComputable) $('#uploadProgress').textContent=`上传中 ${Math.round(e.loaded/e.total*100)}% · 正在写入本机`;};
      xhr.onload=()=>{try{const r=JSON.parse(xhr.responseText); xhr.status>=200&&xhr.status<300?resolve(r):reject(new Error(r.error||'上传失败'));}catch{reject(new Error('上传响应异常'));}};
      xhr.onerror=()=>reject(new Error('上传失败，请检查本地服务是否仍在运行')); xhr.send(file);
    });
    $('#uploadDialog').close(); $('#uploadProgress').textContent=''; toast('上传完成，正在解析日志包');
    await refreshDatasets();
    const poll=async()=>{
      try {
        const datasets=await refreshDatasets(result.id); const current=datasets?.find(d=>d.id===result.id);
        if(current?.state==='ready'){ setView('search'); toast(`导入完成：${number(current.records)} 条日志`); }
        else if(current?.state==='failed'){toast('导入失败：'+current.error);}
        else setTimeout(poll,1200);
      }catch(error){toast(error.message);}
    }; setTimeout(poll,700);
  }catch(error){$('#uploadProgress').textContent=error.message;}
  finally{button.disabled=false;}
}
$('#uploadFile').addEventListener('change',e=>chooseFile(e.target.files[0]));
$('#dropzone').addEventListener('dragover',e=>{e.preventDefault();$('#dropzone').classList.add('dragging');});
$('#dropzone').addEventListener('dragleave',()=>$('#dropzone').classList.remove('dragging'));
$('#dropzone').addEventListener('drop',e=>{e.preventDefault();$('#dropzone').classList.remove('dragging');chooseFile(e.dataTransfer.files[0]);$('#uploadFile').required=false;});
$('#uploadForm').addEventListener('submit',e=>{e.preventDefault();upload(selectedFile);});
$('#searchForm').addEventListener('submit',e=>{e.preventDefault();runSearch();});
$('#traceForm').addEventListener('submit',e=>{e.preventDefault();runTrace();});
$('#configForm').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/ai/config',{base_url:$('#baseUrl').value,model:$('#modelName').value});$('#configDialog').close();toast('模型配置已保存');}catch(error){toast(error.message);}});
$('#aiForm').addEventListener('submit',async e=>{
  e.preventDefault(); if(!needDataset())return;
  $('#analyzeButton').disabled=true; $('#aiAnswer').textContent='正在检索接口与关联日志，并等待模型分析…';
  try{const result=await api('/api/ai/analyze',{dataset:state.dataset,endpoint:$('#aiEndpoint').value,question:$('#aiQuestion').value,consent:$('#consent').checked});$('#aiAnswer').textContent=`本次证据 ${result.evidence_count} 条 · 接口关键词命中 ${result.matched} 条\n\n${result.answer}`;}
  catch(error){$('#aiAnswer').textContent=error.message;}
  finally{$('#analyzeButton').disabled=false;}
});
$('#dataset').addEventListener('change',async()=>{state.dataset=$('#dataset').value;state.files=[];$('#searchForm').reset();try{await refreshDatasets(state.dataset);}catch(error){toast(error.message);}});
$('#refresh').addEventListener('click',()=>refreshDatasets().catch(e=>toast(e.message)));
for(const id of ['node','pod','kind'])$('#'+id).addEventListener('change',updateFilters);
$('#resetFilters').addEventListener('click',()=>{$('#searchForm').reset();state.correlation=null;$('#correlation').hidden=true;updateFilters();});
$('#fileSearch').addEventListener('input',renderFiles);
for(const id of ['sideUpload','topUpload'])$('#'+id).addEventListener('click',openUpload);
for(const id of ['settings','aiSettings'])$('#'+id).addEventListener('click',openSettings);
$$('.nav').forEach(button=>button.addEventListener('click',()=>setView(button.dataset.view)));
$$('.close').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setView('search');$('#query').focus();}});
document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-action]'); if(!button)return;
  const row=state.rows.get(Number(button.dataset.id)); const action=button.dataset.action;
  try {
    if(action==='upload')openUpload();
    if(action==='context')await openContext(row);
    if(action==='correlate')await correlate(row);
    if(action==='trace'){setView('trace');$('#traceId').value=row.trace;await runTrace();}
    if(action==='copy'){await navigator.clipboard.writeText(row.raw);toast('原文已复制');}
    if(action==='page'||action==='jump'){
      const trace=button.dataset.trace==='true';
      let page=Number(button.dataset.page);
      if(action==='jump') { const input=$(`[data-page-input="${trace}"]`);page=Math.max(1,Math.min(Number(input.max),Number(input.value)||1)); }
      await (trace?runTrace(page,true):runSearch(page,true));
    }
    if(action==='file'){
      const file=state.files.find(f=>f.id===Number(button.dataset.file));setView('search');$('#searchForm').reset();$('#node').value=file.node;updateFilters();$('#pod').value=file.namespace+'/'+file.pod;updateFilters();$('#kind').value=file.kind;$('#filename').value=file.filename;
      $('#correlation').hidden=true;state.correlation=null;state.lastSearch={...searchParams(),file_id:file.id};await runSearch(1,true);
    }
    if(action==='toggle-thread'&&state.correlation){const c=state.correlation;if(!c.row.thread)return toast('该日志没有可识别的线程');await correlate(c.row,c.seconds,!c.sameThread);}
    if(action==='widen'&&state.correlation){const c=state.correlation;await correlate(c.row,60,c.sameThread);}
  }catch(error){toast(error.message);}
});
clearResults();
refreshDatasets().then(datasets=>{
  if(datasets?.some(d=>d.state==='importing')) {
    const poll=async()=>{try{const all=await refreshDatasets();if(all?.some(d=>d.state==='importing'))setTimeout(poll,1500);}catch(e){toast(e.message);}};
    setTimeout(poll,1500);
  }
}).catch(error=>toast('无法连接本地服务：'+error.message));
