// Run against tests/chat_browser_server.py. Screenshots contain generated logs only.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

function trackTerminalRequests(page, requests) {
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/terminal' || pathname.startsWith('/api/terminal/')) {
      requests.push(`${request.method()} ${pathname}`);
    }
  });
}

async function legacyViewRegression(browser, sourcePage, terminalRequests) {
  const datasets = await (await sourcePage.request.get('http://127.0.0.1:8879/api/datasets')).json();
  const dataset = datasets.find(item => item.state === 'ready');
  const context = await browser.newContext({viewport:{width:1440,height:1120}});
  await context.addInitScript(value => {
    // Seed once: the reload below must read the application's migrated value.
    if (!localStorage.getItem('logscope.ui.v1')) localStorage.setItem('logscope.ui.v1', JSON.stringify(value));
  }, {dataset:dataset.id, view:'terminal'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  trackTerminalRequests(page, terminalRequests);
  try {
    await page.goto('http://127.0.0.1:8879');
    await page.waitForFunction(() => state.view === 'chat' &&
      JSON.parse(localStorage.getItem('logscope.ui.v1')).view === 'chat');
    await page.locator('#chatView').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-view="terminal"], #terminalView').count(), 0,
      'removed terminal must not remain as a hidden view or navigation entry');
    assert.equal(await page.locator('script[src*="terminal"]').count(), 0);
    await page.reload();
    await page.waitForFunction(() => state.view === 'chat');
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();

    // The native editor must still save versioned rules without terminal.js.
    await page.locator('#chatRules').click();
    await page.locator('#rulesDialog[open]').waitFor();
    const workflow = await page.locator('#rulesWorkflow').inputValue();
    assert(workflow.trim(), 'native rules editor should load the existing workflow');
    const business = await page.locator('#rulesBusiness').inputValue();
    const marker = '浏览器回归规则：将事实证据与待验证推断分别说明。';
    const edited = business ? business + '\n' + marker : marker;
    await page.locator('#rulesBusiness').fill(edited);
    await page.locator('#rulesNote').fill('native-rules-browser-regression');
    const [savedResponse] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/analysis/rules' && response.request().method() === 'POST'),
      page.locator('#saveRules').click(),
    ]);
    assert.equal(savedResponse.status(), 200);
    const saved = await savedResponse.json();
    assert.equal(saved.business, edited);
    assert.equal(saved.workflow, workflow);
    await page.locator('#rulesDialog').waitFor({state:'hidden'});
    await page.locator('#chatRules').click();
    await page.locator('#rulesDialog[open]').waitFor();
    assert.equal(await page.locator('#rulesBusiness').inputValue(), edited);
    assert.equal(await page.locator(`#rulesHistory option[value="${saved.version}"]`).count(), 1);
    await page.locator('#rulesDialog .close').click();
    await page.locator('#chatQuestion').fill('根据已有日志检查接口异常。');
    await page.locator('#chatSend').click();
    await page.locator('#chatPreviewDialog[open]').waitFor();
    assert((await page.locator('#chatPreviewText').innerText()).includes(marker),
      'new native task previews must use the saved business rules');
    await page.locator('#chatPreviewDialog .close').click();
    assert.deepEqual(errors, []);
    assert.deepEqual(terminalRequests, [], 'native pages must never call a removed terminal API');
  } catch (error) {
    await page.screenshot({path:'test-results/chat-legacy-migration-failure.png',fullPage:true});
    throw error;
  } finally { await context.close(); }
}

async function maintenanceRegression(browser, sourcePage, terminalRequests) {
  const datasets = await (await sourcePage.request.get('http://127.0.0.1:8879/api/datasets')).json();
  const dataset = datasets.find(item => item.state === 'ready');
  const files = await (await sourcePage.request.get(`http://127.0.0.1:8879/api/files?dataset=${dataset.id}`)).json();
  const file = files.find(item => item.kind === 'root') || files[0];
  const draft = {dataset:dataset.id, view:'trace', advanced:true, trace:'saved-trace',
    search:{q:'saved-query', start:'2026-09-08 15:15:30.243', node:file.node,
      pod:file.namespace+'/'+file.pod, kind:file.kind}, collector:{pod:'saved-collector'}};
  const context = await browser.newContext({viewport:{width:1440,height:1120}});
  await context.addInitScript(value => localStorage.setItem('logscope.ui.v1', JSON.stringify(value)), draft);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  trackTerminalRequests(page, terminalRequests);
  let blocked = true, failStatus = false, cancelCalls = 0;
  let status = {enabled:true, hours:72, schedule:'02:00', timezone:'中国标准时间',
    phase:'running', maintenance:true, can_cancel:true, next_run:null, last_result:null,
    last_run:'2026-09-16T02:00:00+08:00', message:'正在清理过期日志',
    progress:{stage:'vacuum', elapsed_seconds:123, completed:2, total:3,
      current:'logs.sqlite3', message:'正在收缩日志数据库文件', cancel_requested:false}};
  await page.route('**/api/datasets', route => blocked
    ? route.fulfill({status:503,json:{code:'INDEX_MAINTENANCE',error:'日志数据库正在维护'}})
    : route.continue());
  await page.route('**/api/retention', route => route.fulfill(failStatus
    ? {status:503,json:{error:'test status unavailable'}} : {json:status}));
  await page.route('**/api/retention/cancel', route => {
    cancelCalls++;
    status = {...status,phase:'cancelling',can_cancel:false,message:'等待 SQLite 回滚完成',
      progress:{...status.progress,cancel_requested:true}};
    return route.fulfill({json:status});
  });
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto('http://127.0.0.1:8879');
    await page.locator('#datasetLoadNotice:not([hidden])').waitFor();
    await page.locator('#retentionCancel:not([hidden])').waitFor();
    await page.waitForFunction(() => state.view === 'trace');
    assert.equal(await page.locator('#query').inputValue(), draft.search.q);
    assert.equal(await page.locator('#start').inputValue(), draft.search.start);
    assert.equal(await page.locator('#node').inputValue(), draft.search.node);
    assert.equal(await page.locator('#pod').inputValue(), draft.search.pod);
    assert.equal(await page.locator('#kind').inputValue(), draft.search.kind);
    assert.equal(await page.locator('#traceId').inputValue(), draft.trace);
    assert.equal(await page.locator('#collectPod').inputValue(), draft.collector.pod);
    assert(!(await page.locator('body').innerText()).includes('无法连接本地服务'));
    assert((await page.locator('#retentionProgress').innerText()).includes('2'));
    assert.equal(await page.locator('#retentionCancel').isEnabled(),true);
    assert.equal(await page.locator('#retentionCancel').evaluate(element => getComputedStyle(element).color),'rgb(23, 35, 59)');
    await page.screenshot({path:'test-results/retention-maintenance.png',fullPage:true});
    await page.locator('[data-view="search"]').click();
    await page.locator('#query').fill('draft-edited-during-maintenance');
    await Promise.all([page.waitForResponse(response => response.url().endsWith('/api/retention/cancel')),
      page.locator('#retentionCancel').click()]);
    await page.waitForFunction(() => document.querySelector('#retentionCancel').disabled || document.querySelector('#retentionCancel').hidden);
    assert.equal(cancelCalls,1);
    assert.equal(await page.evaluate(() => state.retentionBusy),true,'keep reservation until actual rollback ends');
    blocked = false;
    status = {...status,phase:'cancelled',maintenance:false,can_cancel:false,
      last_result:{cancelled:true,reason:'本次清理已取消，日志查询已恢复'},next_run:'2026-09-17T02:00:00+08:00'};
    await page.locator('#retentionRetry').click();
    await page.waitForFunction(() => !state.retentionBusy && !state.datasetRefreshPending);
    await page.locator('#datasetLoadNotice').waitFor({state:'hidden'});
    assert.equal(await page.locator('#query').inputValue(),'draft-edited-during-maintenance');
    assert.equal(await page.locator('#start').inputValue(),draft.search.start);
    assert.equal(await page.locator('#node').inputValue(),draft.search.node);
    assert.equal(await page.locator('#pod').inputValue(),draft.search.pod);
    assert.equal(await page.locator('#kind').inputValue(),draft.search.kind);
    assert.equal(await page.evaluate(() => state.view),'search');
    // A failed status poll must not leave a stale client-side search lock.
    status = {...status,phase:'running',maintenance:true,can_cancel:true};
    await page.locator('#retentionRetry').click();
    await page.waitForFunction(() => state.retentionBusy === true);
    failStatus = true;
    await page.locator('#retentionRetry').click();
    await page.waitForFunction(() => state.retentionBusy === false);
    assert.deepEqual(errors,[]);
  } catch (error) {
    await page.screenshot({path:'test-results/retention-failure.png',fullPage:true});
    throw error;
  } finally { await context.close(); }

  // The initial 503 may finish before the first (already idle) status response.
  // Recovery must not depend on observing a running -> idle transition.
  const recovered = await browser.newPage();
  trackTerminalRequests(recovered, terminalRequests);
  let datasetCalls = 0;
  await recovered.route('**/api/datasets', route => ++datasetCalls === 1
    ? route.fulfill({status:503,json:{code:'INDEX_MAINTENANCE',error:'正在结束维护'}})
    : route.continue());
  await recovered.route('**/api/retention', route => route.fulfill({json:{...status,phase:'idle',maintenance:false,can_cancel:false}}));
  try {
    await recovered.goto('http://127.0.0.1:8879');
    await recovered.locator('#datasetInfo').filter({hasText:'份日志文件'}).waitFor({timeout:15000});
    assert(datasetCalls >= 2);
    assert.equal(await recovered.evaluate(() => Boolean(state.retentionBusy)),false);
  } finally { await recovered.close(); }
}

(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1120}});
  const errors = [];
  const terminalRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  trackTerminalRequests(page, terminalRequests);
  fs.mkdirSync('test-results', {recursive:true});
  try {
    for(let i=0;i<30;i++) {
      try { await page.goto('http://127.0.0.1:8879'); break; }
      catch(error) { if(i===29)throw error; await new Promise(resolve=>setTimeout(resolve,1000)); }
    }
    await page.locator('#datasetInfo').filter({hasText:'份日志文件'}).waitFor();
    await page.locator('#retainedCount').filter({hasText:'1'}).waitFor();
    await page.locator('#retainedArchives').click();
    await page.locator('#retainedDialog[open]').waitFor();
    await page.locator('#retainedArchiveList').filter({hasText:'过期示例.zip'}).waitFor();
    const [archive] = await Promise.all([page.waitForEvent('download'), page.locator('.retained-download').click()]);
    assert.equal(archive.suggestedFilename(),'过期示例.zip');
    assert.equal(await archive.failure(),null);
    await page.screenshot({path:'test-results/retention-archives.png',fullPage:true});
    await page.locator('#retainedDialog .close').click();
    await maintenanceRegression(browser,page,terminalRequests);
    await legacyViewRegression(browser,page,terminalRequests);
    await page.locator('[data-view="chat"]').click();
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();
    await page.locator('#chatQuestion').fill('帮我分析 /api/model/map 为什么报错，需要给出日志证据。');
    await page.locator('#chatSend').click();
    await page.locator('#chatPreviewDialog[open]').waitFor();
    const preview = await page.locator('#chatPreviewText').innerText();
    assert(!preview.includes('fake-secret'));
    assert(!preview.includes('http://127.0.0.1:'));
    await page.locator('#chatConfirm').click();
    await page.locator('#chatMessages').filter({hasText:'时间关联本身不能证明代码根因'}).waitFor();
    await page.locator('.chat-tool summary').first().click();
    await page.screenshot({path:'test-results/chat-desktop.png',fullPage:true});
    await page.locator('[data-chat-log]').first().click();
    await page.locator('#contextDialog[open]').waitFor();
    await page.locator('#contextDialog .close').click();
    await page.locator('#chatQuestion').fill('是否有下游连接池异常？');
    await page.locator('#chatSend').click();
    await page.locator('#chatConfirm').click();
    await page.locator('#chatMessages').filter({hasText:'继续查看上下文可以验证该请求是否受下游连接池影响'}).waitFor();
    await page.reload();
    await page.locator('#chatMessages').filter({hasText:'是否有下游连接池异常？'}).waitFor();
    await page.locator('#chatQuestion').fill('保留未发送草稿');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#chatQuestion').value==='保留未发送草稿');
    await page.locator('#chatRules').click();
    await page.locator('#rulesDialog[open]').waitFor();
    await page.locator('#rulesDialog .close').click();
    await page.locator('#chatNew').click();
    await page.locator('#chatTitle').filter({hasText:'新建排查'}).waitFor();
    await page.locator('[data-chat-session]').first().click();
    await page.locator('#chatMessages').filter({hasText:'是否有下游连接池异常？'}).waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'test-results/chat-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'mobile horizontal overflow');
    assert.deepEqual(errors,[]);
    page.on('dialog', dialog=>dialog.accept());
    await page.locator('#chatDelete').click();
    await page.locator('#chatTitle').filter({hasText:'新建排查'}).waitFor();
    assert.equal(await page.locator('[data-chat-session]').count(),0);
    assert.deepEqual(terminalRequests, [], 'all browser flows must avoid removed terminal APIs');
    console.log('Browser regression passed: maintenance 503/drafts/progress/cancel/recovery, retained ZIP/download, terminal-view migration, no terminal API requests, native rules save/preview, tools, evidence, follow-up, reload, drafts, sessions, mobile, delete.');
  } catch(error) {
    await page.screenshot({path:'test-results/chat-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
