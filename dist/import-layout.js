'use strict';
(() => {
  const PAGE_SIZE = 30, DRAFT_PREFIX = 'logscope.import-draft.v1.';
  const dialog = $('#importLayoutDialog');
  const editableStates = new Set(['review', 'failed']);
  const taskStates = new Set(['scanning', 'review', 'importing', 'failed']);
  const fields = ['included','patterns','node','namespace','pod','service','kind','line_mode'];
  const activeTasks = new Set(), cancellations = new Map(), cancelErrors = new Map();
  let current = null, generation = 0, pollTimer, localTimer, queuePage = 1;

  function taskRows() { return state.datasets.filter(row => taskStates.has(row.state) && (row.layout_available || ['scanning','review'].includes(row.state))); }
  function label(value) { return ({scanning:'扫描目录',review:'等待确认',importing:'建立索引',failed:'需要处理',ready:'导入完成'})[value] || value; }
  function chainText(group) { return Array.isArray(group.archive_chain) ? group.archive_chain.join(' → ') : String(group.archive_chain || current?.plan.name || '原始 ZIP'); }
  function revisionText(value) { return String(value ?? ''); }
  function edits(group) {
    return Object.fromEntries([['id',group.id], ...fields.map(key => [key, key === 'included' ? Boolean(group[key]) : key === 'patterns' ? String(group[key] || '').replace(/[\r\n]+/g, ',') : String(group[key] ?? (key === 'line_mode' ? 'lines' : ''))])]);
  }
  function config() { return {encoding:$('#layoutEncoding').value,offset:$('#layoutOffset').value,unit:$('#layoutUnit').value}; }
  function encodingNeedsScan() { return Boolean(current?.plan && $('#layoutEncoding').value !== (current.plan.scan_encoding || current.plan.encoding || 'auto')); }
  function payload() { return {dataset:current.id,revision:current.plan.revision,groups:current.groups.map(edits),...config()}; }
  function setMessage(message, tone='') { $('#importLayoutStatus').textContent=message; $('#importLayoutStatus').className='layout-status'+(tone?' '+tone:''); }
  function statusNote() {
    if (!current) return;
    $('#layoutDraftStatus').textContent = current.conflict ? '草稿版本冲突，请重新加载服务器版本' : current.dirty ? current.localSaved === false ? '未保存：本机暂无法保存，请点击“保存草稿”' : '未提交改动已保存在此浏览器；可保存到服务电脑' : `服务器草稿 · 版本 ${current.plan?.revision ?? '—'}`;
  }
  function persistDraft() {
    clearTimeout(localTimer);
    if (!current?.dirty || !current.plan) return;
    try { localStorage.setItem(DRAFT_PREFIX+current.id, JSON.stringify(payload())); current.localSaved=true; }
    catch { current.localSaved=false; }
    statusNote();
  }
  function dirty() {
    if (!current) return;
    current.dirty=true;statusNote();clearTimeout(localTimer);
    localTimer=setTimeout(persistDraft,200);
    updateSummary();updateButtons();
  }
  function discardLocal(id) { try {localStorage.removeItem(DRAFT_PREFIX+id);} catch {} }
  function restoreLocal() {
    let saved;
    try {saved=JSON.parse(localStorage.getItem(DRAFT_PREFIX+current.id)||'null');} catch {return;}
    if (!saved || saved.dataset!==current.id) return;
    if (revisionText(saved.revision)!==revisionText(current.plan.revision)) {
      current.conflict=true;
      setMessage('服务器草稿已更新，本浏览器的旧草稿未自动覆盖。点击“重新加载”确认使用服务器版本。','error');
      return;
    }
    const overrides=new Map((saved.groups||[]).map(group=>[String(group.id),group]));
    for(const group of current.groups){const override=overrides.get(String(group.id));if(override)Object.assign(group,edits({...group,...override}));}
    for(const [id,key] of [['layoutEncoding','encoding'],['layoutOffset','offset'],['layoutUnit','unit']])if(saved[key]!==undefined)$('#'+id).value=saved[key];
    current.dirty=true;current.localSaved=true;
    setMessage('已恢复本浏览器尚未提交的改动。确认目录后，可以保存草稿或开始建立索引。');
  }
  function updateButtons() {
    if (!current) return;
    const editable=editableStates.has(current.plan?.state), busy=current.busy||activeTasks.has(current.id)||cancellations.has(current.id);
    $('#layoutEditorFields').disabled=busy||!editable||current.conflict;
    $('#layoutTaskPicker').disabled=busy;
    $('#layoutSave').disabled=busy||!editable||current.conflict;
    $('#layoutConfirm').disabled=busy||!editable||current.conflict||encodingNeedsScan()||!current.groups.some(group=>group.included);
    $('#layoutReload').disabled=busy;
    $('#layoutRescan').hidden=!editable;$('#layoutRescan').disabled=busy||current.conflict;
    $('#layoutDiscard').disabled=busy||!editable;
    $('#layoutDiscard').textContent=cancellations.has(current.id)?'正在取消…':'取消导入';
    $('#layoutConfirm').textContent=busy&&current.action==='confirm'?'正在提交…':'确认并建立索引 →';
    $('#layoutEncodingNotice').textContent=encodingNeedsScan()?'编码已更改：请点击“重新扫描”更新样例与文件识别，再确认建立索引。可以先保存草稿。':'更改编码后，请重新扫描以更新样例与文件识别。';
    $('#layoutEncodingNotice').classList.toggle('layout-warnings',encodingNeedsScan());
  }
  function filteredGroups() {
    const query=$('#layoutFilter').value.trim().toLowerCase(), selection=$('#layoutSelectionFilter').value;
    return current.groups.filter(group=>(selection==='all'||Boolean(group.included)===(selection==='included'))&&(!query||[group.directory,chainText(group),group.node,group.namespace,group.pod,group.service].join(' ').toLowerCase().includes(query)));
  }
  function updateSummary() {
    if (!current) return;
    const included=current.groups.filter(group=>group.included).length;
    $('#layoutSelectionSummary').textContent=`共 ${number(current.groups.length)} 个目录 · 已勾选 ${number(included)} 个 · 当前筛选 ${number(filteredGroups().length)} 个。文件是否匹配以导入时规则校验为准。`;
  }
  function confidence(value) {
    const key=String(value??'unknown').toLowerCase();
    return ({high:'高',medium:'中',low:'低',confirmed:'已确认',unknown:'待确认'})[key]||String(value);
  }
  function inputField(group,key,title,placeholder='') {
    return `<label>${title}<input data-layout-field="${key}" value="${escapeHTML(group[key]||'')}" placeholder="${escapeHTML(placeholder)}" maxlength="${key==='patterns'?2048:300}"${key==='kind'?' list="layoutKinds"':''}></label>`;
  }
  function renderGroups() {
    if (!current) return;
    const groups=filteredGroups(), pages=Math.max(1,Math.ceil(groups.length/PAGE_SIZE));
    current.page=Math.max(1,Math.min(current.page,pages));
    const visible=groups.slice((current.page-1)*PAGE_SIZE,current.page*PAGE_SIZE);
    $('#layoutGroups').innerHTML=visible.length?visible.map(group=>`<article class="layout-group ${group.included?'':'excluded'}" data-group-id="${escapeHTML(group.id)}"><div class="layout-group-heading"><label class="layout-group-check"><input type="checkbox" data-layout-field="included" ${group.included?'checked':''}><strong>${escapeHTML(group.directory||'/（ZIP 根目录）')}</strong></label><span class="layout-confidence ${['high','low','medium'].includes(String(group.confidence))?group.confidence:''}">识别置信度：${escapeHTML(confidence(group.confidence))}</span></div><div class="layout-source">${escapeHTML(chainText(group))}</div><p class="layout-reason">${escapeHTML(group.reason||'请按真实业务归属确认目录字段。')} · ${number(group.file_count??group.files?.length)} 个文件 · ${formatBytes(group.bytes)}</p><div class="layout-fields">${inputField(group,'patterns','文件匹配规则','*.log*,*.txt*')}${inputField(group,'node','Node 节点','可手动指定')}${inputField(group,'namespace','Namespace','可留空')}${inputField(group,'pod','Pod 实例','可手动指定')}${inputField(group,'service','Service 服务','可留空')}${inputField(group,'kind','日志类型','留空按文件识别')}<label>文本解析<select data-layout-field="line_mode"><option value="lines" ${group.line_mode==='lines'?'selected':''}>逐行文本（每行一条）</option><option value="auto" ${group.line_mode==='auto'?'selected':''}>自动日志（合并异常堆栈）</option></select></label></div><details class="layout-files"><summary>查看文件与日志样例</summary><div class="layout-file-browser"></div></details></article>`).join(''):'<div class="layout-empty">'+(current.groups.length?'没有匹配的目录，试试清除筛选。':'扫描完成后，这里会列出 ZIP 内实际目录。')+'</div>';
    $('#layoutPageInfo').textContent=`第 ${current.page} / ${pages} 页 · 每页最多 ${PAGE_SIZE} 个目录`;
    $('#layoutPrev').disabled=current.page<=1;$('#layoutNext').disabled=current.page>=pages;
    updateSummary();
  }
  function renderFileList(card, group) {
    const query=card.querySelector('[data-file-filter]')?.value.trim().toLowerCase()||'';
    const files=(group.files||[]).filter(file=>!query||String(file.path||file.name).toLowerCase().includes(query));
    card.querySelector('.layout-file-list').innerHTML=files.slice(0,10).map(file=>`<div class="layout-file-row"><div class="layout-file-main"><code>${escapeHTML(file.path||file.name)}</code>${file.reason?`<small>${escapeHTML(file.reason)}</small>`:''}</div><span class="${file.text===false?'layout-file-blocked':''}">${formatBytes(file.bytes)} · ${file.text===false?'不可导入':file.candidate?'日志候选':'其它文本'}</span></div>`).join('')||'<p>没有匹配的文件。</p>';
    const blocked=files.filter(file=>file.text===false).length;
    card.querySelector('.layout-file-count').textContent=`匹配 ${number(files.length)} 个文件，当前展示前 ${Math.min(10,files.length)} 个${blocked?'；其中 '+number(blocked)+' 个文件不是可读文本，无法导入':''}。此处用于查看；导入范围由上方目录勾选和文件规则决定。`;
    const sampled=(!query&&files.find(file=>file.sample&&file.sample===group.sample))||files.find(file=>file.sample);
    card.querySelector('.layout-sample-heading').textContent=sampled?`样例来源：${sampled.path||sampled.name}（文件片段）`:'当前筛选暂无可显示的文本样例';
    const sample=sampled?String(sampled.sample):query?'筛选结果中的文件未留存文本样例。清除文件筛选可以查看该目录其它文件的样例。':'扫描只保存少量文件的文本片段；没有样例不代表此文件会被排除，请结合文件状态和匹配规则确认。';
    card.querySelector('.layout-sample').textContent=sample.slice(0,8000)+(sample.length>8000?'\n…样例已截断':'');
  }
  function showFiles(details) {
    const card=details.closest('[data-group-id]'),group=current?.byId.get(card.dataset.groupId);if(!group)return;
    const host=details.querySelector('.layout-file-browser');
    host.innerHTML='<label>查找文件<input type="search" data-file-filter placeholder="按文件名或路径筛选"></label><p class="layout-file-count"></p><div class="layout-file-list"></div><strong class="layout-sample-heading">文本样例（只用于辅助识别）</strong><pre class="layout-sample"></pre>';
    renderFileList(card,group);
  }
  function renderPicker() {
    if(!current)return;
    const rows=taskRows().filter(row=>row.id===current.id||!cancellations.has(row.id));if(!rows.some(row=>row.id===current.id))rows.unshift({id:current.id,name:current.plan?.name||current.id,state:current.plan?.state||'scanning'});
    $('#layoutTaskPicker').innerHTML=rows.map(row=>`<option value="${escapeHTML(row.id)}">${escapeHTML(row.name)} · ${escapeHTML(label(row.state))}</option>`).join('');
    $('#layoutTaskPicker').value=current.id;
  }
  function renderQueue() {
    const rows=taskRows();
    for(const [id,task] of cancellations)if(!rows.some(row=>row.id===id))rows.push(task.row);
    const pages=Math.max(1,Math.ceil(rows.length/10));queuePage=Math.min(queuePage,pages);
    $('#importReviewQueue').hidden=!rows.length;
    $('#importReviewTasks').innerHTML=rows.slice((queuePage-1)*10,queuePage*10).map(row=>{
      const cancelling=cancellations.has(row.id),busy=cancelling||activeTasks.has(row.id)||(current?.id===row.id&&dialog.open&&current.busy);
      return `<div class="import-review-task" data-import-task="${escapeHTML(row.id)}" aria-busy="${busy}"><div class="import-review-task-info"><strong>${escapeHTML(row.name)}</strong><span>${cancelling?'正在取消，等待清理此任务…':escapeHTML(label(row.state))} · 原 ZIP ${formatBytes(row.archive_bytes)}</span>${cancelErrors.has(row.id)?`<p class="import-cancel-error" role="status">${escapeHTML(cancelErrors.get(row.id))}</p>`:''}</div><div class="import-review-actions"><button type="button" class="quiet" data-import-open="${escapeHTML(row.id)}" ${busy?'disabled':''}>${row.state==='review'?'确认导入范围':row.state==='failed'?'查看并重试':'查看进度'}</button>${editableStates.has(row.state)||cancelling?`<button type="button" class="danger-outline import-cancel-button" data-import-cancel="${escapeHTML(row.id)}" ${busy?'disabled':''}>${cancelling?'正在取消…':'取消导入'}</button>`:''}</div></div>`;
    }).join('')+(pages>1?`<div class="layout-pagination"><button type="button" data-import-queue-page="${queuePage-1}" ${queuePage===1?'disabled':''}>上一页</button><span>第 ${queuePage} / ${pages} 页</span><button type="button" data-import-queue-page="${queuePage+1}" ${queuePage===pages?'disabled':''}>下一页</button></div>`:'');
    if(dialog.open)renderPicker();
  }
  function cancellationFailed(id,message) {
    const task=cancellations.get(id);if(!task)return;
    clearTimeout(task.timer);cancellations.delete(id);cancelErrors.set(id,message);
    if(current?.id===id){current.busy=false;current.action='';if(dialog.open){setMessage(message,'error');updateButtons();}}
    renderQueue();toast(message);
  }
  async function pollCancellation(id) {
    const task=cancellations.get(id);if(!task)return;
    try{
      const rows=await refreshDatasets();
      if(cancellations.get(id)!==task)return;
      if(Array.isArray(rows)){
        const row=rows.find(item=>item.id===id);
        if(!row){
          cancellations.delete(id);cancelErrors.delete(id);discardLocal(id);
          if(current?.id===id){clearTimeout(localTimer);current.dirty=false;current.busy=false;if(current.plan)current.plan.state='missing';if(dialog.open)dialog.close();}
          renderQueue();toast(`已取消导入“${task.row.name}”，该任务的 ZIP、草稿和残留索引已删除`);return;
        }
        if(row.state!=='deleting'){cancellationFailed(id,'取消失败：'+(row.error||'任务状态已变化，请刷新后重试'));return;}
        cancelErrors.delete(id);
      }
    }catch(error){
      if(cancellations.get(id)!==task)return;
      cancelErrors.set(id,'暂时无法确认取消结果，正在自动重试：'+error.message);renderQueue();
    }
    task.timer=setTimeout(()=>{void pollCancellation(id);},1500);
  }
  async function cancelImport(identifier) {
    const id=String(identifier),row=state.datasets.find(item=>item.id===id)||(current?.id===id?current.plan:null);
    if(!row||!editableStates.has(row.state)||activeTasks.has(id)||cancellations.has(id)||(current?.id===id&&dialog.open&&current.busy))return;
    if(!confirm(`确定取消导入“${row.name}”？将删除该任务上传的 ZIP、导入方案（含草稿）和残留索引，不影响其它日志包。此操作不能撤销。`))return;
    const task={row:{...row,id},timer:null};cancellations.set(id,task);cancelErrors.delete(id);
    if(current?.id===id){current.busy=true;current.action='cancel';if(dialog.open){setMessage('正在取消导入并清理该任务…');updateButtons();}}
    renderQueue();
    try{
      await api('/api/datasets/delete',{dataset:id,compact:false,import_only:true});
      if(current?.id===id&&dialog.open)dialog.close();
      void pollCancellation(id);
    }catch(error){
      cancellationFailed(id,'取消失败：'+error.message+'。请刷新查看任务当前状态，仍待处理时可重试。');
      void refreshDatasets().catch(()=>{});
    }
  }
  function renderPlanStatus(plan) {
    $('#importLayoutMeta').textContent=`${plan.name||current.id} · ${label(plan.state)} · 规则版本 ${plan.revision??'—'}`;
    const progress=plan.progress||{};
    setMessage(plan.state==='scanning'?`正在扫描实际目录与日志样例，尚未建立索引。已检查 ${number(progress.entries)} 个条目 · ${number(progress.groups)} 个目录。${progress.message||''}`:plan.state==='importing'?`正在按确认的规则建立索引：${number(progress.files)} 个文件 · ${number(progress.records)} 条记录 · ${formatBytes(progress.bytes)}。可关闭窗口继续使用其它日志包。`:plan.state==='failed'?`上次处理未完成：${plan.error||'请检查规则或重新扫描'}。已保存原 ZIP，可以修改后重试。`:plan.state==='ready'?'导入完成，可以开始搜索。':'请核对目录归属与文件规则；未勾选目录不会导入。',plan.state==='failed'?'error':'');
  }
  function adopt(plan, restore=true) {
    current.plan=plan;current.groups=(plan.groups||[]).map(group=>({...group,line_mode:group.line_mode||'lines'}));current.byId=new Map(current.groups.map(group=>[String(group.id),group]));current.dirty=false;current.conflict=false;
    $('#layoutEncoding').value=plan.encoding||'auto';$('#layoutOffset').value=plan.offset||'+0800';$('#layoutUnit').value=plan.unit||'ms';
    renderPlanStatus(plan);
    $('#importLayoutWarnings').hidden=!plan.warnings?.length;
    $('#importLayoutWarnings').textContent=(plan.warnings||[]).map(value=>typeof value==='string'?value:JSON.stringify(value)).join('\n');
    if(restore&&editableStates.has(plan.state))restoreLocal();
    renderPicker();renderGroups();statusNote();updateButtons();
  }
  function schedulePreview(delay=1800) {
    clearTimeout(pollTimer);const token=generation,id=current?.id;
    if(!id||!dialog.open)return;
    pollTimer=setTimeout(async()=>{
      if(token!==generation||!dialog.open)return;
      try{
        // Poll only lightweight dataset metadata; a directory plan can be 24 MB.
        const datasets=await api('/api/datasets');
        if(token!==generation||current?.id!==id||!dialog.open)return;
        const row=datasets.find(item=>item.id===id);
        if(!row){current.plan.state='missing';setMessage('该任务已被删除，停止刷新。可以关闭窗口查看其它导入任务。','error');updateButtons();void refreshDatasets().catch(()=>{});return;}
        if(['scanning','importing'].includes(row.state)){
          Object.assign(current.plan,{state:row.state,error:row.error,progress:row.progress});
          renderPlanStatus(current.plan);updateButtons();schedulePreview();
        }else{
          const plan=await api('/api/imports/preview?dataset='+encodeURIComponent(id));
          if(token!==generation||current?.id!==id||!dialog.open)return;
          adopt(plan);
          if(['scanning','importing'].includes(plan.state))schedulePreview();
          void refreshDatasets(plan.state==='ready'?id:undefined).catch(()=>{});
          if(plan.state==='ready'&&current.submitted){dialog.close();setView('search');toast('索引已完成，可以搜索该日志包');}
        }
      }catch(error){if(token!==generation||!dialog.open)return;setMessage(`任务状态暂不可用：${error.message}。稍后自动重试。`,'error');if(error.status!==404)schedulePreview(5000);}
    },delay);
  }
  async function open(id, {reload=false}={}) {
    if(activeTasks.has(String(id))||cancellations.has(String(id)))return;
    persistDraft();clearTimeout(pollTimer);const token=++generation;
    current={id:String(id),plan:null,groups:[],byId:new Map(),page:1,dirty:false,conflict:false,busy:true};
    $('#layoutFilter').value='';$('#layoutSelectionFilter').value='all';
    $('#importLayoutMeta').textContent='正在读取 '+String(id);$('#layoutGroups').innerHTML='';$('#importLayoutWarnings').hidden=true;
    setMessage('正在读取目录扫描结果…');statusNote();updateButtons();renderPicker();
    if(!dialog.open)dialog.showModal();
    try{const plan=await api('/api/imports/preview?dataset='+encodeURIComponent(id));if(token!==generation||!dialog.open)return;current.busy=false;adopt(plan,!reload);renderQueue();if(['scanning','importing'].includes(plan.state))schedulePreview();}
    catch(error){if(token!==generation)return;current.busy=false;setMessage('读取失败：'+error.message+'。可以点击重新加载。','error');updateButtons();renderQueue();}
  }
  async function submit(action) {
    if(!current||current.busy||current.conflict||!editableStates.has(current.plan?.state))return;
    if(!$('#importLayoutForm').reportValidity())return;
    if(action==='confirm'&&encodingNeedsScan())return setMessage('编码已更改，请重新扫描以更新文本样例与文件识别后再确认。','error');
    if(action==='confirm'&&!current.groups.some(group=>group.included))return setMessage('请至少勾选一个目录。','error');
    persistDraft();const id=current.id,token=generation,body=payload();current.busy=true;current.action=action;activeTasks.add(id);updateButtons();renderQueue();
    setMessage(action==='confirm'?'正在保存规则并提交索引任务…':'正在保存草稿…');
    try{
      const result=await api('/api/imports/'+(action==='confirm'?'confirm':'draft'),body);
      if(token!==generation||current?.id!==id)return;
      discardLocal(id);current.dirty=false;current.busy=false;
      if(action==='confirm'){current.submitted=true;current.plan.state=result.state||'importing';setMessage('确认成功，正在建立索引。可关闭窗口，导入任务会继续运行。');updateButtons();statusNote();schedulePreview(500);void refreshDatasets().catch(()=>{});}
      else {adopt(result,false);setMessage('草稿已保存到服务电脑，刷新页面或稍后回来都可以继续。');void refreshDatasets().catch(()=>{});}
    }catch(error){
      if(token!==generation||current?.id!==id)return;
      current.busy=false;
      if(error.status===409){current.conflict=true;setMessage('该任务已被更新或提交。你的改动仍保留在本浏览器；请重新加载服务器版本后再继续，避免覆盖他人的选择。','error');}
      else setMessage((action==='confirm'?'提交失败：':'保存失败：')+error.message+'。草稿仍保留，可修改后重试。','error');
      statusNote();updateButtons();
    }finally{activeTasks.delete(id);if(current?.id===id){current.busy=false;updateButtons();}renderQueue();}
  }

  $('#layoutGroups').addEventListener('input',event=>{
    const card=event.target.closest('[data-group-id]'),group=card&&current?.byId.get(card.dataset.groupId);if(!group)return;
    if(event.target.matches('[data-file-filter]'))return renderFileList(card,group);
    const key=event.target.dataset.layoutField;if(!fields.includes(key)||current.busy||current.conflict)return;
    group[key]=key==='included'?event.target.checked:event.target.value;
    card.classList.toggle('excluded',!group.included);dirty();
  });
  $('#layoutGroups').addEventListener('toggle',event=>{if(event.target.matches('details.layout-files')&&event.target.open)showFiles(event.target);},true);
  for(const id of ['layoutEncoding','layoutOffset','layoutUnit'])$('#'+id).addEventListener('input',dirty);
  for(const id of ['layoutFilter','layoutSelectionFilter'])$('#'+id).addEventListener('input',()=>{if(current){current.page=1;renderGroups();}});
  $('#layoutPrev').addEventListener('click',()=>{current.page--;renderGroups();});$('#layoutNext').addEventListener('click',()=>{current.page++;renderGroups();});
  for(const [id,included] of [['layoutSelectAll',true],['layoutSelectNone',false]])$('#'+id).addEventListener('click',()=>{for(const group of filteredGroups())group.included=included;dirty();renderGroups();});
  $('#layoutSave').addEventListener('click',()=>{void submit('draft');});
  $('#importLayoutForm').addEventListener('submit',event=>{event.preventDefault();void submit('confirm');});
  $('#layoutReload').addEventListener('click',()=>{if(!current||current.busy)return;if((current.dirty||current.conflict)&&!confirm('重新加载会放弃本浏览器未提交的改动，使用服务器最新草稿。继续吗？'))return;const id=current.id;discardLocal(id);current.dirty=false;void open(id,{reload:true});});
  $('#layoutTaskPicker').addEventListener('change',()=>{void open($('#layoutTaskPicker').value);});
  $('#layoutRescan').addEventListener('click',async()=>{
    if(!current||current.busy||current.conflict||!editableStates.has(current.plan?.state))return;
    if(!$('#importLayoutForm').reportValidity())return;
    if(!confirm('重新扫描将使用当前编码、时区与耗时单位，并重置目录勾选、文件规则和字段修改。原 ZIP 保留；继续吗？'))return;
    const id=current.id,token=generation,operation=current;current.busy=true;activeTasks.add(id);updateButtons();renderQueue();
    try{
      if(current.plan.revision){
        const saved=await api('/api/imports/draft',{dataset:id,revision:current.plan.revision,...config()});
        if(token!==generation)return;
        Object.assign(current.plan,{revision:saved.revision,encoding:saved.encoding,scan_encoding:saved.scan_encoding,offset:saved.offset,unit:saved.unit});
        persistDraft();
      }
      await api('/api/imports/rescan',{dataset:id});if(token!==generation)return;
      discardLocal(id);current.dirty=false;activeTasks.delete(id);void open(id,{reload:true});void refreshDatasets().catch(()=>{});
    }catch(error){if(token!==generation)return;current.busy=false;if(error.status===409)current.conflict=true;setMessage('重新扫描失败：'+error.message+(error.status===409?'。请重新加载服务器版本后再继续。':''),'error');statusNote();updateButtons();}
    finally{activeTasks.delete(id);if(current===operation){current.busy=false;updateButtons();}renderQueue();}
  });
  $('#layoutDiscard').addEventListener('click',()=>{if(current)void cancelImport(current.id);});
  $('#importReviewTasks').addEventListener('click',event=>{
    const cancel=event.target.closest('[data-import-cancel]'),task=event.target.closest('[data-import-open]'),page=event.target.closest('[data-import-queue-page]');
    if(cancel){if(!cancel.disabled)void cancelImport(cancel.dataset.importCancel);return;}
    if(task){if(!task.disabled)void open(task.dataset.importOpen);}else if(page){queuePage=Number(page.dataset.importQueuePage);renderQueue();}
  });
  dialog.addEventListener('close',()=>{persistDraft();clearTimeout(pollTimer);generation++;renderQueue();});
  window.addEventListener('beforeunload',persistDraft);
  document.addEventListener('logscope:datasets',renderQueue);
  window.LogScopeImports={open};renderQueue();
})();
