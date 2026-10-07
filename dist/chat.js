'use strict';
(() => {
  let selected=sessionStorage.getItem('logscope.chat.selected')||'', current=null, cursor=0, generation=0;
  let timer=null, busy=false, configured=false, project=null, projectDirty=false, pending=null;
  let sessions=[], listing=0, repositories=[], projectListing=0, projectGeneration=0, syncing=false, preparation=0, submitting=false;
  const labels={search_logs:'搜索日志索引',log_context:'读取异常上下文',correlate_logs:'查询相邻 Pod 日志',verify_log:'核验原始日志',project_search:'搜索项目代码',project_read:'读取局部代码'};
  const states={running:'排查中',stopping:'正在停止',idle:'已保存',stopped:'已停止',failed:'可重试',interrupted:'已中断'};
  const running=()=>['running','stopping'].includes(current?.state);
  const storage=(key,value)=>{try{if(value===undefined)return localStorage.getItem(key);localStorage.setItem(key,value);}catch{}return '';};
  const draftKey=()=>`logscope.chat.draft.${selected||'new'}`;
  const remoteUrl=()=>$('#chatRemoteUrl').value.trim().replace(/\/+$/, '');
  const branchKey=url=>'logscope.chat.branch.'+encodeURIComponent(url);
  const cancelled=()=>Object.assign(new Error('项目选择已更改，本次准备已取消'),{cancelled:true});
  const projectHint='首次使用会自动克隆，后续更新会拉取远程分支。请选择与日志部署版本一致的分支；发送前可预览固定版本。';
  $('#chatQuestion').value=storage(draftKey())||'';
  $('#chatRemoteUrl').value=storage('logscope.chat.remote_url')||'';
  function remember(){storage(draftKey(),$('#chatQuestion').value);}
  function invalidatePreview(){preparation++;pending=null;if(!submitting)busy=false;if($('#chatPreviewDialog').open)$('#chatPreviewDialog').close();}
  function resetProject(dirty=true){projectGeneration++;syncing=false;project=null;projectDirty=dirty;invalidatePreview();$('#chatBranch').innerHTML='<option value="">更新代码后选择分支</option>';$('#chatProjectNote').textContent=projectHint;}
  function renderRepositories(){
    $('#chatRepository').innerHTML='<option value="">输入新的 Git 仓库地址</option>'+repositories.map(repo=>`<option value="${escapeHTML(repo.id)}">${escapeHTML(repo.name)} · ${escapeHTML(repo.remote_url)}</option>`).join('');
    $('#chatRepository').value=repositories.find(repo=>repo.remote_url===remoteUrl())?.id||'';
  }
  async function refreshProjects(){
    const serial=++projectListing;$('#chatRefreshProjects').disabled=true;
    try{const result=await api('/api/chat/projects');if(serial!==projectListing)return;repositories=result.repositories||[];renderRepositories();$('#chatProjectStorage').textContent='仓库保存位置：'+(result.storage_path||'data/projects');}
    finally{if(serial===projectListing)$('#chatRefreshProjects').disabled=false;}
  }
  function inline(text){return escapeHTML(text).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');}
  function markdown(text){
    return String(text||'').split(/```[^\n]*\n([\s\S]*?)(?:```|$)/g).map((part,i)=>i%2?`<pre><code>${escapeHTML(part)}</code></pre>`:part.split('\n').map(line=>{
      const heading=line.match(/^(#{1,4})\s+(.*)$/);if(heading)return `<h${heading[1].length+2}>${inline(heading[2])}</h${heading[1].length+2}>`;
      if(/^[-*]\s/.test(line))return `<div class="chat-bullet">${inline(line.slice(2))}</div>`;
      return line?`<div>${inline(line)}</div>`:'<div class="chat-paragraph-gap"></div>';
    }).join('')).join('');
  }
  function controls(){
    $('#chatSend').disabled=busy||syncing||running()||!configured;
    $('#chatSend').textContent=syncing?'正在同步代码…':busy?'正在准备…':current?.state==='failed'?'继续排查 ↑':'发送并排查 ↑';
    $('#chatStop').hidden=!running();$('#chatStop').disabled=current?.state==='stopping';
    $('#chatDelete').disabled=!selected||running()||busy;$('#chatDownload').disabled=!selected;
    $('#chatNew').disabled=busy&&!syncing;$('#chatActivity').hidden=!current;
    if(current){$('#chatActivity').textContent=current.status;$('#chatActivity').classList.toggle('working',running());}
    $('#chatProjectSummary').textContent=$('#chatUseCode').checked?(syncing?'正在同步代码…':project?'已同步 · '+$('#chatBranch').value:current?.task.project&&!projectDirty?'固定版本 · '+current.task.project.branch:'待同步 · Git 仓库'):'未启用 · 仅分析日志';
    $('#chatProjectFields').hidden=!$('#chatUseCode').checked;
    $('#chatSyncProject').disabled=syncing||busy||running()||!remoteUrl();
    $('#chatBranch').disabled=!project||syncing||busy||running();
    $('#chatRemoteUrl').disabled=submitting;$('#chatRepository').disabled=submitting;$('#chatUseCode').disabled=submitting;
    $('#chatProjectNote').dataset.working=String(syncing);
  }
  function renderSessions(){
    $('#chatSessionList').innerHTML=sessions.length?sessions.map(s=>`<button class="chat-session ${s.id===selected?'active':''}" data-chat-session="${s.id}"><span class="chat-session-title">${escapeHTML(s.title)}</span><span><i class="${s.state==='running'?'live':''}"></i>${states[s.state]||s.state} · ${escapeHTML(s.task.name)}</span></button>`).join(''):'<div class="chat-list-empty">暂无排查会话<br>发出第一个问题后自动保存</div>';
  }
  async function refreshList(){const serial=++listing;const data=await api('/api/chat/sessions');if(serial!==listing)return;sessions=data;renderSessions();}
  function welcome(){
    $('#chatMessages').innerHTML='<div class="chat-welcome"><div>✧</div><h2>从日志线索，找到问题原因</h2><p>描述接口、时间、流水号或异常现象。已有索引直接查询；选择项目后，AI 可继续对照代码定位。</p><button data-chat-example="帮我检查当前日志包的异常接口和慢请求，查找关键异常并给出证据。">排查异常与慢请求 ↗</button></div>';
  }
  function applySession(info){
    current=info;$('#chatTitle').textContent=info.title;
    $('#chatScope').textContent=`${info.task.name} · 规则 v${info.task.rules_version}`+(info.task.project?` · ${info.task.project.branch} @ ${info.task.project.commit.slice(0,10)}`:' · 仅日志');
    controls();
  }
  function eventHTML(item){
    const b=item.body;
    if(item.kind==='user')return `<div class="chat-role">你</div><div class="chat-prose">${markdown(b.text)}</div>`;
    if(item.kind==='assistant')return `<div class="chat-role">LogScope ${b.streaming?'<span class="chat-typing">正在输出</span>':b.elapsed_ms!=null?`<small>模型 ${number(b.elapsed_ms)} ms</small>`:''}</div><div class="chat-prose">${b.text?markdown(b.text):'<span class="chat-thinking">等待模型返回…</span>'}</div>${b.interrupted?'<small>本轮输出已中断，收到的内容已保留。</small>':''}`;
    if(item.kind==='scope')return `<div class="chat-scope-chip">${escapeHTML(b.task.name)}${b.task.project?' · 代码 '+escapeHTML(b.task.project.branch)+' @ '+b.task.project.commit.slice(0,10):' · 仅查询日志'} · 规则 v${b.task.rules_version}</div>`;
    if(item.kind==='notice')return `<div class="chat-note">${escapeHTML(b.text)}</div>`;
    const r=b.result||{}, rows=r.rows||[], code=r.matches||[];
    const total=r.summary?.total;
    const summary=total!=null?`命中 ${number(total)} 条 · 返回 ${number(rows.length)} 条`:r.path?`${r.path} · L${r.start}–${r.end}`:code.length?`返回 ${number(code.length)} 处代码` : r.verified===true?'原文核验一致':r.error?'查询未完成':'';
    const evidence=rows.slice(0,20).map(row=>`<button class="chat-evidence" data-chat-log="${row.id}" data-chat-dataset="${escapeHTML(row.dataset||current?.task?.dataset||'')}">日志 #${row.id} · ${escapeHTML(row.pod||'')} · ${escapeHTML(row.filename||'')} · L${row.line}</button>`).join('');
    return `<details class="chat-tool"><summary><span class="chat-tool-icon ${b.state}">${b.state==='running'?'◌':b.state==='failed'?'!':'✓'}</span><strong>${escapeHTML(labels[b.name]||b.name)}</strong><span>${escapeHTML(summary)}</span><small>${b.elapsed_ms!=null?`${number(b.elapsed_ms)} ms`:'进行中'}</small></summary><div class="chat-tool-content"><p>查询条件</p><pre>${escapeHTML(JSON.stringify(b.args,null,2))}</pre>${r.truncated||r.has_more?'<p class="chat-truncation">结果未全部展示；需缩小条件或继续查询。不能把当前结果当作全量。</p>':''}<div class="chat-evidence-list">${evidence}</div>${b.result?`<p>返回证据</p><pre>${escapeHTML(JSON.stringify(r,null,2))}</pre>`:''}</div></details>`;
  }
  function renderEvents(events){
    const box=$('#chatMessages'),atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<90;
    if(events.length)box.querySelector('.chat-welcome')?.remove();
    for(const item of events){
      let element=document.getElementById('chat-event-'+item.id);
      const open=element?.querySelector('details')?.open;
      if(!element){element=document.createElement('article');element.id='chat-event-'+item.id;element.className='chat-event '+item.kind;box.appendChild(element);}
      element.innerHTML=eventHTML(item);if(open&&element.querySelector('details'))element.querySelector('details').open=true;
    }
    if(atBottom)box.scrollTop=box.scrollHeight;
  }
  async function poll(serial){
    clearTimeout(timer);if(!selected||serial!==generation)return;
    try{
      const result=await api('/api/chat/session?id='+encodeURIComponent(selected)+'&after='+cursor);
      if(serial!==generation)return;
      cursor=result.cursor;applySession(result.session);renderEvents(result.events);
      if(result.events.length)await refreshList();
      timer=setTimeout(()=>poll(serial),running()?650:2500);
    }catch(e){if(serial===generation){toast(e.message);timer=setTimeout(()=>poll(serial),4000);}}
  }
  async function selectSession(id){
    if(busy&&!syncing)return;remember();clearTimeout(timer);resetProject(false);const serial=++generation;
    selected=id;sessionStorage.setItem('logscope.chat.selected',id);cursor=0;current=null;project=null;projectDirty=false;
    $('#chatQuestion').value=storage(draftKey())||'';$('#chatMessages').innerHTML='';
    try{
      const result=await api('/api/chat/session?id='+encodeURIComponent(id));if(serial!==generation)return;
      cursor=result.cursor;applySession(result.session);renderEvents(result.events);
      const p=current.task.project;$('#chatUseCode').checked=Boolean(p);
      $('#chatRemoteUrl').value=p?(p.remote_url||''):(storage('logscope.chat.remote_url')||'');
      if(p){$('#chatBranch').innerHTML=`<option value="${escapeHTML(p.branch)}">${escapeHTML(p.branch)}</option>`;$('#chatProjectNote').textContent=`固定代码版本 ${p.commit}，继续对话将沿用此版本。`+(p.remote_url?'如需更新，请更新代码并重新预览。':'此历史会话未记录 Git 地址，可填写 Git 仓库地址后更新。');}
      renderRepositories();
      controls();renderSessions();timer=setTimeout(()=>poll(serial),650);
    }catch(e){if(serial===generation){toast(e.message);newSession();}}
  }
  function newSession(){
    if(busy&&!syncing)return;remember();clearTimeout(timer);resetProject(false);generation++;selected='';current=null;project=null;cursor=0;projectDirty=false;
    sessionStorage.removeItem('logscope.chat.selected');$('#chatTitle').textContent='新建排查';$('#chatScope').textContent='使用左侧当前日志包';
    $('#chatQuestion').value=storage(draftKey())||'';$('#chatUseCode').checked=false;$('#chatRemoteUrl').value=storage('logscope.chat.remote_url')||'';renderRepositories();
    welcome();controls();renderSessions();$('#chatQuestion').focus();
  }
  async function capability(){
    const result=await api('/api/chat/capability');configured=result.configured;$('#chatCapability').textContent=result.message;
    $('.chat-status-dot').classList.toggle('ready',configured);
    controls();
  }
  async function syncProject(){
    const url=remoteUrl();if(!url)throw new Error('请填写 Git 仓库地址，或选择已有仓库');
    const serial=++projectGeneration,session=selected,oldBranch=$('#chatBranch').value;
    const valid=()=>serial===projectGeneration&&session===selected&&url===remoteUrl();
    const checkCurrent=()=>{if(!valid())throw cancelled();};
    syncing=true;project=null;projectDirty=true;$('#chatProjectNote').textContent='正在准备仓库；首次克隆可能需要一些时间…';controls();
    try{
      const job=await api('/api/chat/project-sync',{remote_url:url});checkCurrent();
      let result=job;
      while(!['ready','failed'].includes(result.state)){
        $('#chatProjectNote').textContent=result.message||'正在克隆 / 更新远程仓库，请稍候…';
        await new Promise(resolve=>setTimeout(resolve,900));checkCurrent();
        result=await api('/api/chat/project-sync?id='+encodeURIComponent(job.id));checkCurrent();
      }
      if(result.state==='failed')throw new Error(result.message||'仓库同步失败，请检查地址与服务电脑的 Git 访问权限后重试');
      const branches=(result.branches||[]).filter(branch=>branch.startsWith('origin/')&&branch!=='origin/HEAD');
      if(!branches.length)throw new Error('仓库没有可用的远程分支，请检查仓库后重试');
      project=result;projectDirty=false;storage('logscope.chat.remote_url',url);
      $('#chatBranch').innerHTML=branches.map(branch=>`<option value="${escapeHTML(branch)}">${escapeHTML(branch)}</option>`).join('');
      const preferred=oldBranch||storage(branchKey(url));
      $('#chatBranch').value=branches.includes(preferred)?preferred:branches.includes(result.current)?result.current:branches[0];
      storage(branchKey(url),$('#chatBranch').value);
      $('#chatProjectNote').textContent=(result.message||'代码已更新')+'。请选择与日志部署版本一致的远程分支；预览将显示固定 commit。';
      await refreshProjects().catch(()=>{});checkCurrent();
      controls();return result;
    }catch(e){if(!valid())throw cancelled();$('#chatProjectNote').textContent=e.message;throw e;}
    finally{if(valid()){syncing=false;controls();}}
  }
  async function prepare(){
    if(busy||syncing||running())return;if(!$('#chatQuestion').value.trim())return toast('请先输入问题');if(!selected&&!needDataset())return;
    const serial=++preparation,session=selected,question=$('#chatQuestion').value,dataset=state.dataset;
    const checkCurrent=()=>{if(serial!==preparation||session!==selected)throw cancelled();};
    busy=true;controls();
    try{
      const useCode=$('#chatUseCode').checked;
      if(useCode&&!project&&(!current?.task.project||projectDirty))await syncProject();
      checkCurrent();
      const result=await api('/api/chat/preview',{id:session,dataset,question,use_code:useCode,
        sync_id:useCode&&project?project.id:'',branch:$('#chatBranch').value});
      checkCurrent();pending=result;
      pending.request_id=globalThis.crypto?.randomUUID?.()||Date.now()+'-'+Math.random().toString(36).slice(2);
      pending.draft_text=question;
      if($('#chatPreviewEnabled').checked){$('#chatPreviewText').textContent=pending.text;$('#chatPreviewDialog').showModal();}
      else await submit();
    }catch(e){if(!e.cancelled)toast(e.message);}
    finally{if(serial===preparation){busy=false;controls();}}
  }
  async function submit(){
    if(!pending||submitting)return;
    const request=pending;submitting=true;busy=true;$('#chatConfirm').disabled=true;controls();
    try{
      const result=await api('/api/chat/send',{preview_id:request.preview_id,request_id:request.request_id});
      if($('#chatQuestion').value===request.draft_text){$('#chatQuestion').value='';remember();}
      $('#chatPreviewDialog').close();pending=null;
      busy=false;submitting=false;await refreshList();await selectSession(result.id);
    }catch(e){toast(e.message);}
    finally{submitting=false;busy=false;$('#chatConfirm').disabled=false;controls();}
  }
  $('#chatSend').addEventListener('click',prepare);$('#chatConfirm').addEventListener('click',submit);
  $('#chatQuestion').addEventListener('input',remember);
  $('#chatQuestion').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();prepare();}});
  $('#chatPreviewEnabled').checked=storage('logscope.chat.preview')!=='false';
  $('#chatPreviewEnabled').addEventListener('change',()=>storage('logscope.chat.preview',String($('#chatPreviewEnabled').checked)));
  $('#chatUseCode').addEventListener('change',()=>{if(syncing)resetProject();else invalidatePreview();controls();});
  $('#chatRemoteUrl').addEventListener('input',()=>{resetProject();renderRepositories();controls();});
  $('#chatRepository').addEventListener('change',()=>{const repo=repositories.find(item=>item.id===$('#chatRepository').value);resetProject();$('#chatRemoteUrl').value=repo?.remote_url||'';controls();});
  $('#chatRefreshProjects').addEventListener('click',()=>refreshProjects().catch(e=>toast(e.message)));
  $('#chatBranch').addEventListener('change',()=>{invalidatePreview();storage(branchKey(remoteUrl()),$('#chatBranch').value);controls();});
  $('#chatSyncProject').addEventListener('click',()=>{invalidatePreview();syncProject().catch(e=>{if(!e.cancelled)toast(e.message);});});
  $('#chatNew').addEventListener('click',newSession);
  $('#chatRefresh').addEventListener('click',()=>refreshList().catch(e=>toast(e.message)));
  $('#chatRefreshConfig').addEventListener('click',()=>capability().catch(e=>toast(e.message)));
  $('#chatStop').addEventListener('click',async()=>{try{await api('/api/chat/stop',{id:selected});await poll(generation);}catch(e){toast(e.message);}});
  $('#chatDelete').addEventListener('click',async()=>{
    if(!selected||!confirm('删除这个排查会话、聊天记录和报告？此操作不可恢复；不会删除日志包和项目。'))return;
    try{await api('/api/chat/delete',{id:selected});storage(draftKey(),'');newSession();await refreshList();toast('会话与报告已删除，日志包和项目未改动。');}catch(e){toast(e.message);}
  });
  $('#chatDownload').addEventListener('click',async()=>{
    try{const result=await api('/api/chat/report?id='+selected);if(!result.text)return toast('本会话尚未生成完整报告');const url=URL.createObjectURL(new Blob([result.text],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='logscope-report.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message);}
  });
  document.addEventListener('click',async event=>{
    const session=event.target.closest('[data-chat-session]');if(session){await selectSession(session.dataset.chatSession);return;}
    const example=event.target.closest('[data-chat-example]');if(example){$('#chatQuestion').value=example.dataset.chatExample;remember();$('#chatQuestion').focus();}
    const evidence=event.target.closest('[data-chat-log]');if(evidence){try{await openContext(await api('/api/record?id='+evidence.dataset.chatLog+'&dataset='+encodeURIComponent(evidence.dataset.chatDataset)));}catch(e){toast(e.message);}}
  });
  let loaded=false;
  document.addEventListener('logscope:view',async event=>{
    if(event.detail!=='chat'){clearTimeout(timer);generation++;if(syncing){resetProject();controls();}return;}
    void refreshProjects().catch(e=>toast('仓库列表暂不可用：'+e.message));
    try{await capability();await refreshList();if(!loaded){loaded=true;if(selected&&sessions.some(s=>s.id===selected))await selectSession(selected);else newSession();}else if(selected)await poll(++generation);}catch(e){toast(e.message);}
  });
})();
