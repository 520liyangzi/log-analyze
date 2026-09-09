'use strict';
// xterm handles the actual VT/ANSI stream. HTTP is transport only; input goes to a PTY.
(() => {
  let term, fit, sessionId='', cursor=0, timer, running=false, pollGeneration=0;
  let inputQueue='', sending=false, resizeTimer, reportText='';
  let inflightInput=null;
  const request=api;
  function initialize() {
    if(term)return;
    term=new Terminal({cursorBlink:true, fontFamily:'"Cascadia Mono", Consolas, "SFMono-Regular", monospace',fontSize:14,
      scrollback:10000,allowProposedApi:false,convertEol:false,theme:{background:'#0c1525',foreground:'#d2ddef',cursor:'#86aaff',
      selectionBackground:'#345986',black:'#111a2a',red:'#f48b99',green:'#76d7ae',yellow:'#eccc89',blue:'#7da8ff',magenta:'#b69cf3',cyan:'#79cee1',white:'#d2ddef'}});
    fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open($('#terminalMount'));
    term.writeln('\x1b[38;5;111mLogScope · 本机交互终端\x1b[0m');
    term.writeln('选择日志包，填写排查问题，然后启动 Claude 或公司 Agent。');
    term.writeln('登录、权限确认、键盘输入与后续追问，都可以在这里完成。');
    term.onData(data=>queueInput(data));
    term.onResize(({cols,rows})=>{if(sessionId&&running)request('/api/terminal/resize',{id:sessionId,cols,rows}).catch(e=>notice(e.message));});
    new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(!$('#terminalView').hidden)fit.fit();},120);}).observe($('#terminalMount'));
  }
  function notice(text){$('#terminalNotice').textContent=text;}
  function setStatus(info) {
    running=info.state==='running';$('#terminalWorkspace').classList.toggle('connected',running);
    $('#terminalState').textContent=running?'终端运行中':info.state==='stopped'?'终端已结束':'进程已退出';
    $('#terminalCwd').textContent=info.cwd||'';
    for(const id of ['sendTask','interruptTerminal','stopTerminal'])$('#'+id).disabled=!running;
    if(info.error)notice(info.error);
  }
  async function queueInput(data) {
    if(!sessionId||!running)return;
    inputQueue+=data;
    if(sending)return;
    sending=true;
    try{
      while(inputQueue&&running){
        const payload=inputQueue.slice(0,60000);inputQueue=inputQueue.slice(60000);
        inflightInput=request('/api/terminal/input',{id:sessionId,data:payload});
        await inflightInput;
      }
    }catch(e){inputQueue='';notice('输入发送失败：'+e.message);}
    finally{sending=false;inflightInput=null;}
  }
  async function connect(info) {
    if(sending){notice('输入正在发送，请稍后切换终端。');return;}
    clearTimeout(timer);const generation=++pollGeneration;
    sessionId=info.id;cursor=0;inputQueue='';sessionStorage.setItem('logscopeTerminal',sessionId);
    initialize();term.reset();setStatus(info);await sessions();fit.fit();term.focus();
    if(running)await request('/api/terminal/resize',{id:sessionId,cols:term.cols,rows:term.rows});
    const poll=async()=>{
      if(generation!==pollGeneration)return;
      try{
        const result=await request('/api/terminal/output?'+paramsURL({id:sessionId,cursor}));
        if(generation!==pollGeneration)return;
        if(result.reset){term.reset();notice('输出超过缓存范围，已从最近的终端内容恢复。');}
        // Acknowledge only after xterm processes the chunk, keeping large output bounded.
        if(result.output)await new Promise(resolve=>term.write(result.output,resolve));
        cursor=result.cursor;setStatus(result);
        if(result.state==='running'||result.more)timer=setTimeout(poll,result.more?10:150);
        else await sessions();
      }catch(e){notice('终端连接中断：'+e.message+'。页面会自动重试；服务重启后需要新建终端。');timer=setTimeout(poll,2000);}
    };
    poll();
  }
  async function sessions() {
    const items=await request('/api/terminal/sessions');
    $('#terminalSessions').innerHTML='<option value="">选择会话</option>'+items.map((s,i)=>`<option value="${s.id}">终端 ${i+1} · ${s.state==='running'?'运行中':'已结束'} · ${escapeHTML(s.command||'Shell')}</option>`).join('');
    $('#terminalSessions').value=sessionId;
    return items;
  }
  async function configuration() {
    const config=await request('/api/terminal/config');
    $('#terminalCommand').value=config.command;
    $('#terminalCapability').textContent=config.available?config.platform:config.reason;
    $('#terminalStart').disabled=!config.available;$('#shellStart').disabled=!config.available;
    if(!config.available)notice(config.reason);
    return config;
  }
  async function start(runCommand) {
    if(!needDataset())return;
    initialize();$('#terminalStart').disabled=true;$('#shellStart').disabled=true;
    try{
      if(sending)throw new Error('输入正在发送，请稍后新建终端');
      await request('/api/terminal/config',{command:$('#terminalCommand').value});
      fit.fit();
      const info=await request('/api/terminal/start',{dataset:state.dataset,question:$('#terminalQuestion').value,
        cols:term.cols,rows:term.rows,run_command:runCommand});
      await connect(info);
      notice('当前会话已绑定创建时的日志包和问题。等 Agent 进入对话界面后，点击「发送排查任务」；也可以直接输入。');
    }catch(e){notice(e.message);toast(e.message);}
    finally{$('#terminalStart').disabled=false;$('#shellStart').disabled=false;}
  }
  async function loadReport() {
    if(!sessionId)return toast('请先启动终端');
    try{
      const result=await request('/api/terminal/report?id='+sessionId);
      reportText=result.text;$('#terminalReport').textContent=result.available?result.text:'还没有 report.md。请在终端中让 Agent 将完整分析写入当前目录 report.md。';
    }catch(e){toast(e.message);}
  }
  $('#terminalStart').addEventListener('click',()=>start(true));
  $('#shellStart').addEventListener('click',()=>start(false));
  $('#terminalSessions').addEventListener('change',async()=>{try{const target=$('#terminalSessions').value;const items=await sessions();const selected=items.find(s=>s.id===target);if(selected)await connect(selected);}catch(e){notice(e.message);}});
  // This sends a prompt only when explicitly clicked after the Agent is ready. No
  // question text is interpolated into shell commands during process startup.
  $('#sendTask').addEventListener('click',async()=>{try{const config=await request('/api/terminal/config');await queueInput(config.prompt+'\r');term.focus();}catch(e){notice(e.message);}});
  $('#interruptTerminal').addEventListener('click',()=>{queueInput('\x03');term?.focus();});
  $('#stopTerminal').addEventListener('click',async()=>{try{await request('/api/terminal/stop',{id:sessionId});setStatus({state:'stopped',cwd:$('#terminalCwd').textContent});await sessions();}catch(e){notice(e.message);}});
  $('#toggleFullscreen').addEventListener('click',()=>{$('#terminalWorkspace').classList.toggle('fullscreen');fit?.fit();term?.focus();});
  $('#copySelection').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(term?.getSelection()||'');toast('已复制选中内容');}catch{toast('请使用 Ctrl+Shift+C 复制选中内容');}});
  $('#pasteTerminal').addEventListener('click',async()=>{try{const text=await navigator.clipboard.readText();term.paste(text);term.focus();}catch{toast('请在终端中使用 Ctrl+Shift+V 粘贴');}});
  $('#refreshReport').addEventListener('click',loadReport);
  $('#downloadReport').addEventListener('click',async()=>{await loadReport();if(!reportText)return;const url=URL.createObjectURL(new Blob([reportText],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='logscope-report.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  let loaded=false;
  document.addEventListener('logscope:view',async e=>{
    if(e.detail!=='terminal')return;
    try{initialize();fit.fit();if(!loaded){loaded=true;await configuration();const items=await sessions();const previous=items.find(s=>s.id===sessionStorage.getItem('logscopeTerminal'));if(previous)await connect(previous);}}catch(error){loaded=false;notice(error.message);}
  });
})();
