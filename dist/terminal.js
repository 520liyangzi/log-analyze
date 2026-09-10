'use strict';
// xterm renders the real PTY. No timed prompt injection or readiness guessing.
(() => {
  let term, fit, sessionId='', cursor=0, timer, running=false, pollGeneration=0;
  let inputQueue='', sending=false, resizeTimer, reportText='', currentInfo=null, available=false;
  let rules=null, editorBase=0, editorDirty=false, previewText='', previewAction=null, projectSnapshot=null, lastReportAt=0, starting=false;
  const request=api;
  const draftKey='logscopeAnalysisQuestion';
  $('#terminalQuestion').value=localStorage.getItem(draftKey)||'';
  $('#projectPath').value=localStorage.getItem('logscopeProjectPath')||'';
  $('#terminalQuestion').addEventListener('input',()=>localStorage.setItem(draftKey,$('#terminalQuestion').value));
  function notice(text){$('#terminalNotice').textContent=text;}
  function datasetLabel(){
    $('#analysisDataset').textContent='日志包：'+($('#dataset').selectedOptions[0]?.textContent||'请先导入');
  }
  function launchLabel(){
    const command=$('#terminalCommand').value.trim()||'未设置命令';
    $('#launchSummary').textContent=command+($('#terminalLaunchMode').value==='argument'?' · 自动读取任务':' · AI 就绪后发送任务');
  }
  function stateLabel(state){return state==='running'?'运行中':state==='stopped'?'已结束':state==='interrupted'?'待恢复':state==='exited'?'已退出':'已保存';}
  function initialize() {
    if(term)return;
    term=new Terminal({cursorBlink:true,fontFamily:'"Cascadia Mono", Consolas, "SFMono-Regular", monospace',fontSize:14,
      scrollback:10000,allowProposedApi:false,convertEol:false,theme:{background:'#0c1525',foreground:'#d2ddef',cursor:'#86aaff',
      selectionBackground:'#345986',black:'#111a2a',red:'#f48b99',green:'#76d7ae',yellow:'#eccc89',blue:'#7da8ff',magenta:'#b69cf3',cyan:'#79cee1',white:'#d2ddef'}});
    fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open($('#terminalMount'));
    term.writeln('\x1b[38;5;111mLogScope · AI 排查终端\x1b[0m');
    term.writeln('填写上方问题，点击「开始排查」。');
    term.writeln('登录、权限确认和后续追问，都可以在这里完成。');
    term.onData(data=>queueInput(data).catch(e=>notice(e.message)));
    term.onResize(({cols,rows})=>{if(sessionId&&running)request('/api/terminal/resize',{id:sessionId,cols,rows}).catch(e=>notice(e.message));});
    new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(!$('#terminalView').hidden)fit.fit();},120);}).observe($('#terminalMount'));
  }
  function setStatus(info) {
    currentInfo={...currentInfo,...info};
    running=info.state==='running';$('#terminalWorkspace').classList.toggle('connected',running);
    $('#terminalState').textContent=running?'终端运行中':stateLabel(info.state);
    $('#terminalCwd').textContent=info.cwd||'';
    for(const id of ['sendTask','interruptTerminal','stopTerminal','sendLatestRules'])$('#'+id).disabled=!running;
    $('#sendTask').textContent=currentInfo.launch_mode==='argument'?'重新发送任务':'AI 就绪后发送任务';
    $('#analysisSessionBar').hidden=false;
    $('#sessionIdentity').hidden=false;
    if(document.activeElement!==$('#aiSessionId'))$('#aiSessionId').value=currentInfo.ai_session_id||'';
    $('#resumeTerminal').disabled=running||!currentInfo.ai_session_id;
    $('#deleteTerminalSession').disabled=running;
    $('#sessionIdentityHint').textContent=currentInfo.ai_session_id
      ? `已保存 Session ID；${running?'当前对话仍在运行':'可按启动设置中的模板恢复对话'}。`
      : '若终端输出包含 Session ID 会自动识别，也可手动粘贴。';
    const updates=currentInfo.rule_updates||[];
    $('#sessionTaskSummary').textContent=`本次会话：${currentInfo.name||currentInfo.dataset||''} · 初始规则 v${currentInfo.rules_version||'?'}${updates.length?' · 已准备更新 v'+updates[updates.length-1].version:''} · ${currentInfo.question||'未填写问题'}`;
    if(info.error)notice(info.error);
  }
  async function queueInput(data) {
    if(!sessionId||!running)throw new Error('请先启动终端');
    inputQueue+=data;
    if(sending)return;
    sending=true;
    try{
      while(inputQueue&&running){
        const payload=inputQueue.slice(0,60000);inputQueue=inputQueue.slice(60000);
        await request('/api/terminal/input',{id:sessionId,data:payload});
      }
    }catch(e){inputQueue='';notice('输入发送失败：'+e.message);throw e;}
    finally{sending=false;}
  }
  async function connect(info) {
    if(sending)throw new Error('输入正在发送，请稍后切换终端');
    clearTimeout(timer);const generation=++pollGeneration;
    sessionId=info.id;cursor=0;inputQueue='';currentInfo=info;
    sessionStorage.setItem('logscopeTerminal',sessionId);
    reportText='';lastReportAt=0;$('#terminalReport').textContent='正在检查本次报告…';
    initialize();term.reset();setStatus(info);await sessions();fit.fit();term.focus();
    if(running)await request('/api/terminal/resize',{id:sessionId,cols:term.cols,rows:term.rows});
    await loadReport(true);
    const poll=async()=>{
      if(generation!==pollGeneration)return;
      try{
        const result=await request('/api/terminal/output?'+paramsURL({id:sessionId,cursor}));
        if(generation!==pollGeneration)return;
        if(result.reset){term.reset();notice('输出超过缓存范围，已从最近的终端内容恢复。');}
        if(result.output)await new Promise(resolve=>term.write(result.output,resolve));
        if(generation!==pollGeneration)return;
        cursor=result.cursor;setStatus(result);
        if((!$('#terminalView').hidden&&Date.now()-lastReportAt>5000)||result.state!=='running')await loadReport(true);
        if(generation!==pollGeneration)return;
        if(result.state==='running'||result.more)timer=setTimeout(poll,result.more?10:200);
        else await sessions();
      }catch(e){
        if(generation!==pollGeneration)return;
        notice('终端连接中断：'+e.message+'。页面会自动重试；服务重启后可用保存的 Session ID 恢复。');timer=setTimeout(poll,2000);
      }
    };
    poll();
  }
  async function openHistory(info){
    if(sending)throw new Error('输入正在发送，请稍后切换窗口');
    clearTimeout(timer);++pollGeneration;sessionId=info.id;cursor=0;inputQueue='';currentInfo=info;running=false;
    sessionStorage.setItem('logscopeTerminal',sessionId);initialize();term.reset();
    const result=await request('/api/terminal/history?id='+sessionId);
    currentInfo=result.info;setStatus(result.info);
    if(result.truncated)term.writeln('\x1b[38;5;214m[较早的终端记录已省略，当前显示最后 2 MB]\x1b[0m');
    if(result.transcript)await new Promise(resolve=>term.write(result.transcript,resolve));
    else term.writeln('\x1b[38;5;111m[这个任务还没有保存终端输出]\x1b[0m');
    await sessions();await loadReport(true);fit.fit();
    notice(result.info.ai_session_id?'历史任务已打开；点击「恢复这个对话」可继续排查。':'历史任务已打开。保存 AI Session ID 后可以恢复原对话。');
  }
  function renderTaskTabs(items){
    $('#taskTabs').innerHTML=items.length?items.map(s=>`<button class="task-tab${s.id===sessionId?' active':''}" data-task-id="${s.id}" title="${escapeHTML(s.question||s.name||'排查任务')}"><i class="task-tab-state ${s.state}"></i><span class="task-tab-label">${escapeHTML(s.question||s.name||'排查任务')} · ${stateLabel(s.state)}</span></button>`).join(''):'<span class="subtle">启动任务后会显示在这里</span>';
  }
  async function sessions() {
    const items=await request('/api/terminal/sessions');
    $('#terminalSessions').innerHTML='<option value="">选择会话</option>'+items.map((s,i)=>`<option value="${s.id}">${i+1} · ${stateLabel(s.state)} · ${escapeHTML(s.question||s.name||s.command||'Shell')}</option>`).join('');
    $('#terminalSessions').value=sessionId;
    renderTaskTabs(items);
    return items;
  }
  async function configuration() {
    const config=await request('/api/terminal/config');
    $('#terminalCommand').value=config.command;$('#terminalLaunchMode').value=config.launch_mode;$('#resumeCommandTemplate').value=config.resume_template;
    available=config.available;
    $('#terminalCapability').textContent=config.available?config.platform:config.reason;
    $('#terminalStart').disabled=!available;$('#shellStart').disabled=!available;
    if(!available)notice(config.reason);
    launchLabel();return config;
  }
  async function saveSettings(){
    await request('/api/terminal/config',{command:$('#terminalCommand').value,launch_mode:$('#terminalLaunchMode').value,resume_template:$('#resumeCommandTemplate').value});
    launchLabel();
  }
  async function refreshRules(){
    rules=await request('/api/analysis/rules');
    $('#activeRulesBadge').textContent='分析规则 v'+rules.version;return rules;
  }
  function taskBody(){
    return {dataset:state.dataset,question:$('#terminalQuestion').value.trim(),rules_version:rules?.version};
  }
  async function start(runCommand,preparedBody=null) {
    if(!needDataset()||starting)return;
    if(runCommand&&!$('#terminalQuestion').value.trim()){toast('先写下你想排查的问题');$('#terminalQuestion').focus();return;}
    if(runCommand&&!$('#terminalCommand').value.trim()){toast('请设置本机 AI 启动命令');$('#agentSettings').open=true;$('#terminalCommand').focus();return;}
    starting=true;initialize();$('#terminalStart').disabled=true;$('#shellStart').disabled=true;
    try{
      if(sending)throw new Error('输入正在发送，请稍后新建终端');
      await saveSettings();if(!preparedBody)await refreshRules();fit.fit();
      const info=await request('/api/terminal/start',{...(preparedBody||taskBody()),cols:term.cols,rows:term.rows,run_command:runCommand});
      await connect(info);
      notice(info.launch_mode==='argument'?'任务已随启动命令传入。请在终端完成首次登录或权限确认，AI 会读取任务；之后可直接追问。若命令不支持启动问题，请在启动设置切换兼容模式。':'任务已准备好。先等 AI 进入对话界面，再点击「AI 就绪后发送任务」。只打开 Shell 时，请先输入你的 AI 启动命令。');
    }catch(e){notice(e.message);toast(e.message);}
    finally{starting=false;$('#terminalStart').disabled=!available;$('#shellStart').disabled=!available;}
  }
  async function loadReport(quiet=false) {
    if(!sessionId){if(!quiet)toast('请先启动终端');return;}
    const target=sessionId;
    lastReportAt=Date.now();
    try{
      const result=await request('/api/terminal/report?id='+target);
      if(target!==sessionId)return;
      const first=!reportText&&result.available&&result.text;
      reportText=result.available?result.text:'';
      $('#terminalReport').textContent=result.available?result.text:'等待 AI 保存报告。你也可以在终端里让它将完整结果写入 report.md，保存后这里会自动更新。';
      $('#codeInvestigation').hidden=!result.available;
      if(first){$('#reportPanel').open=true;toast('本次分析报告已生成');}
    }catch(e){if(!quiet)toast(e.message);}
  }
  function setEditor(value){
    $('#rulesWorkflow').value=value.workflow;$('#rulesBusiness').value=value.business;
    $('#rulesNote').value='';editorDirty=false;
  }
  function historyOptions(){
    $('#rulesHistory').innerHTML=rules.history.map(r=>`<option value="${r.version}">v${r.version} · ${escapeHTML(r.note)} · ${escapeHTML(new Date(r.created).toLocaleString())}</option>`).join('');
  }
  async function openRules(){
    try{
      if(!editorDirty){await refreshRules();editorBase=rules.version;setEditor(rules);historyOptions();$('#rulesNotice').textContent='';}
      $('#rulesVersionText').textContent='基于版本 v'+editorBase+ (editorDirty?' · 未保存':'');
      $('#rulesDialog').showModal();
    }catch(e){toast(e.message);}
  }
  function showPreview(text,title,note,action=null){
    previewText=text;previewAction=action;$('#taskPreviewTitle').textContent=title;$('#taskPreviewNote').textContent=note;
    $('#taskPreviewText').textContent=text;$('#taskPreviewDialog').showModal();
    $('#confirmTaskPreview').hidden=!action;
    $('#confirmTaskPreview').textContent=action?.kind==='code'?'确认并发送代码定位任务':'确认并启动 AI';
    $('#taskPreviewSafety').textContent=action?'请核对内容；只有点击右侧确认按钮才会发送。':'当前仅供查看，尚未发送给 AI。';
  }
  async function previewBeforeStart(){
    if(!needDataset()||starting)return;
    if(!$('#terminalQuestion').value.trim()){toast('先写下你想排查的问题');$('#terminalQuestion').focus();return;}
    if(!$('#terminalCommand').value.trim()){toast('请设置本机 AI 启动命令');$('#agentSettings').open=true;$('#terminalCommand').focus();return;}
    starting=true;$('#terminalStart').disabled=true;
    try{
      await refreshRules();const body=taskBody();const result=await request('/api/terminal/preview',body);
      showPreview(result.text,'启动前预览 task.md','日志范围、查询线索、分析规则和可用命令都会原样写入任务文件。确认前不会启动 AI。',{kind:'start',body});
    }catch(e){toast(e.message);}
    finally{starting=false;$('#terminalStart').disabled=!available;}
  }
  $('#terminalStart').addEventListener('click',previewBeforeStart);
  $('#shellStart').addEventListener('click',()=>start(false));
  $('#saveTerminalSettings').addEventListener('click',async()=>{try{await saveSettings();toast('启动设置已保存');}catch(e){toast(e.message);}});
  $('#terminalCommand').addEventListener('input',launchLabel);$('#terminalLaunchMode').addEventListener('change',launchLabel);
  $('#dataset').addEventListener('change',datasetLabel);
  $$('[data-question]').forEach(button=>button.addEventListener('click',()=>{
    $('#terminalQuestion').value=button.dataset.question;localStorage.setItem(draftKey,button.dataset.question);$('#terminalQuestion').focus();
  }));
  $('#editRules').addEventListener('click',openRules);
  for(const id of ['rulesWorkflow','rulesBusiness','rulesNote'])$('#'+id).addEventListener('input',()=>{editorDirty=true;$('#rulesVersionText').textContent='基于版本 v'+editorBase+' · 未保存';});
  $('#loadRulesVersion').addEventListener('click',async()=>{
    if(editorDirty&&!confirm('载入版本会替换当前未保存的编辑，是否继续？'))return;
    try{
      const selected=await request('/api/analysis/rules?version='+$('#rulesHistory').value);
      setEditor(selected);editorBase=selected.latest_version;editorDirty=true;
      $('#rulesVersionText').textContent='已载入 v'+selected.version+' · 保存后创建新版本';
      $('#rulesNotice').textContent='这只是编辑草稿，点击保存后才会生效。';
    }catch(e){$('#rulesNotice').textContent=e.message;}
  });
  $('#refreshRulesHistory').addEventListener('click',async()=>{
    try{await refreshRules();historyOptions();$('#rulesNotice').textContent='版本列表已刷新，当前编辑已保留。可载入最新版本后合并修改。';}catch(e){$('#rulesNotice').textContent=e.message;}
  });
  $('#restoreRules').addEventListener('click',()=>{
    if(!confirm('恢复默认会替换当前流程并清空业务规则，保存后才生效。继续？'))return;
    setEditor(rules.defaults);editorDirty=true;$('#rulesNote').value='恢复默认规则';
    $('#rulesNotice').textContent='默认内容已载入，点击保存后才会生效。';
  });
  $('#saveRules').addEventListener('click',async()=>{
    $('#saveRules').disabled=true;
    try{
      rules=await request('/api/analysis/rules',{workflow:$('#rulesWorkflow').value,business:$('#rulesBusiness').value,note:$('#rulesNote').value,base_version:editorBase});
      editorDirty=false;editorBase=rules.version;$('#activeRulesBadge').textContent='分析规则 v'+rules.version;
      $('#rulesDialog').close();toast('规则 v'+rules.version+' 已保存，新任务将使用这个版本');
    }catch(e){$('#rulesNotice').textContent=e.message;}
    finally{$('#saveRules').disabled=false;}
  });
  $('#previewTask').addEventListener('click',async()=>{
    if(!needDataset())return;
    try{await refreshRules();const result=await request('/api/terminal/preview',taskBody());showPreview(result.text,'新任务预览','这里使用已保存的规则 v'+result.rules.version+'。尚未启动 AI，也没有发送日志。');}catch(e){toast(e.message);}
  });
  $('#confirmTaskPreview').addEventListener('click',async()=>{
    if(!previewAction)return;
    const action=previewAction,button=$('#confirmTaskPreview');button.disabled=true;
    try{
      if(action.kind==='start'){
        $('#taskPreviewDialog').close();previewAction=null;await start(true,action.body);
      }else if(action.kind==='code'){
        if(!sessionId||!running)throw new Error('AI 终端已经结束，请先选择运行中的会话');
        const result=await request('/api/terminal/code-task',action.body);
        await queueInput(result.prompt+'\r');$('#taskPreviewDialog').close();previewAction=null;term.focus();
        notice(`代码定位任务已发送：${result.task.branch} @ ${result.task.commit.slice(0,12)}。AI 会继续更新 report.md。`);
        toast('代码定位任务已发送给当前 AI');
      }
    }catch(e){notice(e.message);toast(e.message);}
    finally{button.disabled=false;}
  });
  async function loadProjectBranches(){
    const path=$('#projectPath').value.trim();if(!path){toast('请填写项目的绝对路径');$('#projectPath').focus();return;}
    $('#projectBranch').disabled=true;$('#previewCodeTask').disabled=true;$('#projectStatus').textContent='正在读取 Git 分支…';
    try{
      projectSnapshot=await request('/api/project/branches?'+paramsURL({path}));
      localStorage.setItem('logscopeProjectPath',projectSnapshot.root);$('#projectPath').value=projectSnapshot.root;
      $('#projectBranch').innerHTML=projectSnapshot.branches.map(branch=>`<option value="${escapeHTML(branch)}">${escapeHTML(branch)}${branch===projectSnapshot.current?' · 当前分支':''}</option>`).join('');
      $('#projectBranch').disabled=false;$('#previewCodeTask').disabled=false;
      $('#projectStatus').textContent=`已识别 ${projectSnapshot.branches.length} 个分支；代码定位固定到提交，不切换你的工作区。`;
    }catch(e){projectSnapshot=null;$('#projectStatus').textContent=e.message;toast(e.message);}
  }
  $('#projectPath').addEventListener('input',()=>{projectSnapshot=null;$('#projectBranch').disabled=true;$('#previewCodeTask').disabled=true;localStorage.setItem('logscopeProjectPath',$('#projectPath').value);});
  $('#loadProjectBranches').addEventListener('click',loadProjectBranches);
  $('#previewCodeTask').addEventListener('click',async()=>{
    if(!sessionId||!running)return toast('请先选择正在运行的 AI 排查会话');
    if(!projectSnapshot)return loadProjectBranches();
    const body={id:sessionId,project_path:projectSnapshot.root,branch:$('#projectBranch').value};
    try{
      const result=await request('/api/terminal/code-preview',body);
      showPreview(result.text,'发送前预览 code-task.md',`将基于日志报告检查 ${result.task.branch}，固定提交 ${result.task.commit.slice(0,12)}。确认前不会把任务发送给 AI。`,{kind:'code',body:{...body,commit:result.task.commit}});
    }catch(e){toast(e.message);$('#projectStatus').textContent=e.message;}
  });
  $('#viewSessionTask').addEventListener('click',async()=>{
    try{
      const result=await request('/api/terminal/task?id='+sessionId);
      const updates=result.rule_updates.map(u=>'v'+u.version+'（'+u.file+'）').join('、');
      showPreview(result.text,'本次任务快照','初始规则 v'+result.task.rules_version+'。'+(updates?'已准备的规则更新：'+updates+'；是否采用以 AI 回复和报告为准。':'页面修改不会自动改变这个任务。'));
    }catch(e){toast(e.message);}
  });
  $('#copyTaskPreview').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(previewText);toast('任务内容已复制');}catch{toast('复制失败，请选中任务内容手动复制');}});
  async function selectTask(target){
    const items=await sessions(),selected=items.find(s=>s.id===target);if(!selected)return;
    if(selected.live&&selected.state==='running')await connect(selected);else await openHistory(selected);
  }
  $('#terminalSessions').addEventListener('change',async()=>{try{await selectTask($('#terminalSessions').value);}catch(e){notice(e.message);}});
  $('#taskTabs').addEventListener('click',async event=>{const button=event.target.closest('[data-task-id]');if(!button)return;try{await selectTask(button.dataset.taskId);}catch(e){notice(e.message);}});
  $('#refreshTaskWindows').addEventListener('click',async()=>{try{await sessions();toast('排查窗口已刷新');}catch(e){toast(e.message);}});
  $('#saveAiSessionId').addEventListener('click',async()=>{
    if(!sessionId)return toast('请先选择一个排查任务');
    try{const info=await request('/api/terminal/session-id',{id:sessionId,ai_session_id:$('#aiSessionId').value});currentInfo=info;setStatus(info);await sessions();toast('Session ID 已保存');}catch(e){toast(e.message);}
  });
  $('#resumeTerminal').addEventListener('click',async()=>{
    if(!sessionId||running)return;
    $('#resumeTerminal').disabled=true;
    try{
      initialize();fit.fit();await saveSettings();
      const info=await request('/api/terminal/resume',{id:sessionId,ai_session_id:$('#aiSessionId').value,cols:term.cols,rows:term.rows});
      await connect(info);notice(`已执行恢复命令，Session ID：${info.ai_session_id}。`);
    }catch(e){notice(e.message);toast(e.message);setStatus(currentInfo);}
  });
  $('#deleteTerminalSession').addEventListener('click',async()=>{
    if(!sessionId||running)return;
    const label=currentInfo?.question||currentInfo?.name||sessionId;
    if(!confirm(`确定删除这个 AI 排查会话吗？\n\n${label}\n\n任务文件、Session ID、终端记录和 report.md 都会永久删除，无法恢复。`))return;
    const target=sessionId;$('#deleteTerminalSession').disabled=true;
    try{
      await request('/api/terminal/delete',{id:target});
      clearTimeout(timer);++pollGeneration;sessionId='';cursor=0;currentInfo=null;running=false;reportText='';
      sessionStorage.removeItem('logscopeTerminal');initialize();term.reset();
      term.writeln('\x1b[38;5;111mLogScope · AI 排查终端\x1b[0m');term.writeln('排查会话已删除，可以开始新的任务。');
      $('#analysisSessionBar').hidden=true;$('#sessionIdentity').hidden=true;$('#terminalWorkspace').classList.remove('connected');
      $('#terminalState').textContent='尚未启动';$('#terminalCwd').textContent='终端尚未启动。任务、终端记录和报告会自动保存在本机。';
      $('#terminalReport').textContent='Agent 写入 report.md 后，可在这里查看。终端内的分析回复也会正常显示。';
      $('#codeInvestigation').hidden=true;await sessions();toast('AI 排查会话已删除');notice('会话及其任务文件、终端记录和报告已删除。');
    }catch(e){toast(e.message);notice(e.message);$('#deleteTerminalSession').disabled=false;}
  });
  $('#sendTask').addEventListener('click',async()=>{try{if(sending)throw new Error('输入正在发送，请稍后重试');const target=sessionId;const config=await request('/api/terminal/config');if(target!==sessionId)throw new Error('会话已切换，请重新发送');await queueInput(config.prompt+'\r');term.focus();notice('读取任务的指令已发送。请查看 AI 回复确认；若当前仍是 CMD，请先启动 AI 再发送。');}catch(e){notice(e.message);}});
  $('#sendLatestRules').addEventListener('click',async()=>{
    if(!confirm('请确认 AI 已进入对话界面且可以接收输入。将发送最新已保存规则，请它重新核对本次分析。继续？'))return;
    $('#sendLatestRules').disabled=true;
    try{
      if(sending)throw new Error('输入正在发送，请稍后重试');
      const target=sessionId;await refreshRules();
      const result=await request('/api/terminal/rules',{id:target,rules_version:rules.version});
      if(sessionId!==target)throw new Error('会话已切换，请回到原会话再发送规则');
      await queueInput(result.prompt+'\r');term.focus();
      notice('规则 v'+result.version+' 更新指令已发送，请从 AI 回复确认是否采用。初始任务快照仍保留。');
    }catch(e){notice(e.message);}
    finally{$('#sendLatestRules').disabled=!running;}
  });
  $('#interruptTerminal').addEventListener('click',()=>{queueInput('\x03').catch(e=>notice(e.message));term?.focus();});
  $('#stopTerminal').addEventListener('click',async()=>{try{await request('/api/terminal/stop',{id:sessionId});setStatus({...currentInfo,state:'stopped'});await sessions();}catch(e){notice(e.message);}});
  $('#toggleFullscreen').addEventListener('click',()=>{$('#terminalWorkspace').classList.toggle('fullscreen');fit?.fit();term?.focus();});
  $('#copySelection').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(term?.getSelection()||'');toast('已复制选中内容');}catch{toast('请使用 Ctrl+Shift+C 复制选中内容');}});
  $('#pasteTerminal').addEventListener('click',async()=>{try{const text=await navigator.clipboard.readText();term.paste(text);term.focus();}catch{toast('请在终端中使用 Ctrl+Shift+V 粘贴');}});
  $('#refreshReport').addEventListener('click',()=>loadReport());
  $('#reportPanel').addEventListener('toggle',()=>{if($('#reportPanel').open)loadReport(true);});
  $('#downloadReport').addEventListener('click',async()=>{await loadReport();if(!reportText)return;const url=URL.createObjectURL(new Blob([reportText],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='logscope-report.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  let loaded=false;
  document.addEventListener('logscope:view',async e=>{
    if(e.detail!=='terminal')return;
    try{
      initialize();fit.fit();datasetLabel();await refreshRules();
      if(!loaded){await configuration();const items=await sessions();const previous=items.find(s=>s.id===sessionStorage.getItem('logscopeTerminal'));if(previous)await connect(previous);loaded=true;}
    }catch(error){notice(error.message);}
  });
})();
