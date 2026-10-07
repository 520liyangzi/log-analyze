'use strict';
// The native chat shares versioned rules; it has no dependency on a terminal.
(() => {
  let rules=null, editorBase=0, editorDirty=false, opening=false;
  async function refreshRules(){rules=await api('/api/analysis/rules');return rules;}
  function setEditor(value){
    $('#rulesWorkflow').value=value.workflow;$('#rulesBusiness').value=value.business;
    $('#rulesNote').value='';editorDirty=false;
  }
  function historyOptions(){
    $('#rulesHistory').innerHTML=rules.history.map(rule=>`<option value="${rule.version}">v${rule.version} · ${escapeHTML(rule.note)} · ${escapeHTML(new Date(rule.created).toLocaleString())}</option>`).join('');
  }
  async function openRules(){
    if(opening)return;opening=true;
    try{
      if(!editorDirty){await refreshRules();editorBase=rules.version;setEditor(rules);historyOptions();$('#rulesNotice').textContent='';}
      $('#rulesVersionText').textContent='基于版本 v'+editorBase+(editorDirty?' · 未保存':'');
      if(!$('#rulesDialog').open)$('#rulesDialog').showModal();
    }catch(error){toast(error.message);}
    finally{opening=false;}
  }
  $('#chatRules').addEventListener('click',openRules);
  for(const id of ['rulesWorkflow','rulesBusiness','rulesNote'])$('#'+id).addEventListener('input',()=>{editorDirty=true;$('#rulesVersionText').textContent='基于版本 v'+editorBase+' · 未保存';});
  $('#loadRulesVersion').addEventListener('click',async()=>{
    if(editorDirty&&!confirm('载入版本会替换当前未保存的编辑，是否继续？'))return;
    try{
      const selected=await api('/api/analysis/rules?version='+encodeURIComponent($('#rulesHistory').value));
      setEditor(selected);editorBase=selected.latest_version;editorDirty=true;
      $('#rulesVersionText').textContent='已载入 v'+selected.version+' · 保存后创建新版本';
      $('#rulesNotice').textContent='这只是编辑草稿，点击保存后才会生效。';
    }catch(error){$('#rulesNotice').textContent=error.message;}
  });
  $('#refreshRulesHistory').addEventListener('click',async()=>{
    try{await refreshRules();historyOptions();$('#rulesNotice').textContent='版本列表已刷新，当前编辑已保留。可载入最新版本后合并修改。';}
    catch(error){$('#rulesNotice').textContent=error.message;}
  });
  $('#restoreRules').addEventListener('click',()=>{
    if(!rules||!confirm('恢复默认会替换当前流程并清空业务规则，保存后才生效。继续？'))return;
    setEditor(rules.defaults);editorDirty=true;$('#rulesNote').value='恢复默认规则';
    $('#rulesNotice').textContent='默认内容已载入，点击保存后才会生效。';
  });
  $('#saveRules').addEventListener('click',async()=>{
    if($('#saveRules').disabled)return;
    $('#saveRules').disabled=true;
    try{
      rules=await api('/api/analysis/rules',{workflow:$('#rulesWorkflow').value,business:$('#rulesBusiness').value,note:$('#rulesNote').value,base_version:editorBase});
      editorDirty=false;editorBase=rules.version;
      $('#rulesDialog').close();toast('规则 v'+rules.version+' 已保存，新排查将使用这个版本');
    }catch(error){$('#rulesNotice').textContent=error.message;}
    finally{$('#saveRules').disabled=false;}
  });
})();
