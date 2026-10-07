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

async function reloadWithDelayedChat(page, sessionId) {
  // A defer script may still be downloading when an earlier script's timer runs.
  // Hold chat.js until app.js restored state and its queued zero-delay work ran;
  // startup must still restore the chat without requiring a navigation click.
  let releaseChat;
  const chatReady = new Promise(resolve => { releaseChat = resolve; });
  const holdChat = async route => { await chatReady; await route.continue(); };
  await page.route('**/chat.js', holdChat);
  try {
    const requested = page.waitForRequest(request => new URL(request.url()).pathname === '/chat.js');
    await page.reload({waitUntil:'commit'});
    await requested;
    await page.waitForFunction(() => typeof state !== 'undefined' && state.uiRestored);
    // This is an event-loop barrier, not a timing-dependent sleep.
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    releaseChat();
    await page.waitForLoadState('domcontentloaded');
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();
    await page.locator(`[data-chat-session="${sessionId}"].active`).waitFor();
    await page.locator('#chatMessages').filter({hasText:'是否有下游连接池异常？'}).waitFor();
    await page.waitForFunction(() => document.querySelector('#chatQuestion').value === '保留未发送草稿');
  } finally {
    releaseChat();
    await page.unroute('**/chat.js', holdChat);
  }
}

async function sessionLoadRecoveryRegression(page, sessionId, expectedProject) {
  let releaseLoad;
  const loadGate = new Promise(resolve => { releaseLoad = resolve; });
  let failedOnce = false;
  const attemptedTurns = [];
  const trackTurns = request => {
    if (request.method() === 'POST' && ['/api/chat/preview', '/api/chat/send'].includes(new URL(request.url()).pathname))
      attemptedTurns.push(request);
  };
  const firstLoadFails = async route => {
    const url = new URL(route.request().url());
    if (!failedOnce && url.searchParams.get('id') === sessionId && !url.searchParams.has('after')) {
      failedOnce = true;
      await loadGate;
      await route.fulfill({status:503, json:{error:'测试：会话记录暂时无法读取，请重试'}});
    } else await route.continue();
  };
  page.on('request', trackTurns);
  await page.route('**/api/chat/session?*', firstLoadFails);
  try {
    const loading = page.waitForRequest(request => {
      const url = new URL(request.url());
      return url.pathname === '/api/chat/session' && url.searchParams.get('id') === sessionId && !url.searchParams.has('after');
    });
    await page.locator(`[data-chat-session="${sessionId}"]`).click();
    await loading;
    assert.equal(await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected')), sessionId);
    assert.equal(await page.locator('#chatSend').isDisabled(), true, 'switching conversations must block sends while history loads');
    const draft = '历史加载期间写下的追问，恢复后继续同一个会话。';
    await page.locator('#chatQuestion').fill(draft);
    await page.locator('#chatQuestion').press('Control+Enter');
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    assert.equal(attemptedTurns.length, 0, 'the send hotkey must also respect the history-loading guard');

    releaseLoad();
    await page.locator('#chatRetrySession').waitFor({state:'visible'});
    assert.equal(await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected')), sessionId,
      'a failed history request must not silently turn a follow-up into a new conversation');
    assert.equal(await page.locator('#chatSend').isDisabled(), true);
    await page.locator('#chatQuestion').press('Control+Enter');
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    assert.equal(attemptedTurns.length, 0, 'failed history must remain unsendable until it is reloaded');
    await page.screenshot({path:'test-results/chat-session-retry.png', fullPage:true});

    await page.locator('#chatRetrySession').click();
    await page.locator('#chatMessages').filter({hasText:'代码版本已固定，Service.java 中 browser-code-v1 是本轮代码证据'}).waitFor();
    await page.locator('#chatScope').filter({hasText:expectedProject.commit.slice(0,10)}).waitFor();
    assert.equal(await page.locator('#chatUseCode').isChecked(), true);
    assert.equal(await page.locator('#chatRemoteUrl').inputValue(), expectedProject.remote_url);
    assert.equal(await page.locator('#chatBranch').inputValue(), expectedProject.branch);
    assert.equal(await page.locator('#chatQuestion').inputValue(), draft);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected')), sessionId);
    const [preview] = await Promise.all([
      page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/preview'),
      page.locator('#chatSend').click(),
    ]);
    assert.equal(preview.postDataJSON().id, sessionId);
    await page.locator('#chatPreviewDialog[open]').waitFor();
    await page.locator('#chatPreviewTitle').filter({hasText:'继续当前对话'}).waitFor();
    assert((await page.locator('#chatPreviewText').innerText()).includes(expectedProject.commit),
      'a recovered follow-up must keep its original fixed code revision');
    await page.locator('#chatPreviewDialog .close').click();
  } finally {
    releaseLoad();
    await page.unroute('**/api/chat/session?*', firstLoadFails);
    page.off('request', trackTurns);
  }
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

async function projectRepositoryRegression(browser, terminalRequests) {
  // Real Git transport on loopback: no hosted repository or model API is used.
  const fixture = JSON.parse(fs.readFileSync('test-results/chat-git-fixture.json', 'utf8'));
  const context = await browser.newContext({viewport:{width:1440,height:1120}});
  const page = await context.newPage();
  const errors = [];
  const projectSyncRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/project-sync')
      projectSyncRequests.push(request);
  });
  trackTerminalRequests(page, terminalRequests);
  async function openProjectFromTopButton() {
    const syncCount = projectSyncRequests.length;
    await page.locator('#chatOpenProject').waitFor({state:'visible'});
    await page.locator('#chatOpenProject').click();
    await page.locator('#chatProjectOptions[open]').waitFor();
    assert.equal(await page.locator('#chatUseCode').isChecked(), true);
    await page.locator('#chatRemoteUrl').waitFor({state:'visible'});
    assert.equal(projectSyncRequests.length, syncCount,
      'opening project options must not clone or fetch before update/send');
  }
  try {
    const repository = await (await page.request.get(fixture.control_url + '/__fixture__/repository')).json();
    await page.goto('http://127.0.0.1:8879');
    await page.locator('#datasetInfo').filter({hasText:'份日志文件'}).waitFor();
    await page.locator('[data-view="chat"]').click();
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();
    assert.equal(await page.locator('#chatProjectPath').count(), 0,
      'new code investigations should use a Git URL, not a manually prepared local directory');
    await openProjectFromTopButton();
    await page.locator('#chatRemoteUrl').fill(repository.remote_url);
    await page.locator('#chatSyncProject').click();
    await page.waitForFunction(branch => !document.querySelector('#chatBranch').disabled &&
      [...document.querySelector('#chatBranch').options].some(option => option.value === branch), repository.branch);
    const projects = await (await page.request.get('http://127.0.0.1:8879/api/chat/projects')).json();
    assert.equal(projects.repositories.length, 1, 'first sync should register one cloned repository');
    const cached = projects.repositories[0];
    assert.equal(cached.remote_url, repository.remote_url);
    assert(cached.root.replaceAll('\\', '/').startsWith(projects.storage_path.replaceAll('\\', '/') + '/'),
      'clone must be inside the server-managed projects directory');
    assert.equal(await page.locator('#chatRepository').inputValue(), cached.id);
    const branches = await page.locator('#chatBranch option').evaluateAll(options => options.map(option => option.value));
    assert(branches.length >= 2 && branches.every(branch => branch.startsWith('origin/') && branch !== 'origin/HEAD'));
    await page.locator('#chatBranch').selectOption(repository.branch);
    await page.locator('#chatProjectButtonState').filter({hasText:repository.branch}).waitFor();
    await page.locator('#chatQuestion').fill('结合已选分支代码，检查 Service.java 的异常证据。');
    await page.locator('#chatSend').click();
    await page.locator('#chatPreviewDialog[open]').waitFor();
    const initialPreview = await page.locator('#chatPreviewText').innerText();
    assert(initialPreview.includes(repository.initial_commit));
    assert(initialPreview.includes(repository.branch));
    assert(initialPreview.includes('project_read'));
    assert(!initialPreview.includes('fake-secret'));
    await page.locator('#chatConfirm').click();
    await page.locator('#chatMessages').filter({hasText:'代码版本已固定，Service.java 中 browser-code-v1 是本轮代码证据'}).waitFor();
    const sessionId = await page.locator('[data-chat-session].active').getAttribute('data-chat-session');
    const historyUrl = 'http://127.0.0.1:8879/api/chat/session?id=' + sessionId;
    const history = await (await page.request.get(historyUrl)).json();
    assert.equal(history.session.task.project.commit, repository.initial_commit);
    assert.equal(history.session.task.project.branch, repository.branch);
    assert.equal(history.session.task.project.repository_id, cached.id);
    assert(history.events.some(event => event.kind === 'tool' && event.body.name === 'project_read' &&
      event.body.result.commit === repository.initial_commit && event.body.result.content.includes('browser-code-v1')),
      'the real code tool must read the selected remote branch at the previewed commit');
    await page.locator('#chatOpenProject').scrollIntoViewIfNeeded();
    await page.screenshot({path:'test-results/chat-project-code.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#chatOpenProject').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#chatOpenProject').isVisible(), true);
    const projectButton = await page.locator('#chatOpenProject').boundingBox();
    assert(projectButton && projectButton.x >= 0 && projectButton.x + projectButton.width <= 390,
      'the project entry button must fit the narrow viewport');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
      'project controls must not cause mobile horizontal overflow');
    await page.screenshot({path:'test-results/chat-project-mobile.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1120});

    const advanced = await (await page.request.post(fixture.control_url + '/__fixture__/advance')).json();
    assert.notEqual(advanced.current_commit, repository.initial_commit);
    await page.locator('#chatNew').click();
    await openProjectFromTopButton();
    assert.equal(await page.locator('#chatRemoteUrl').inputValue(), repository.remote_url);
    await page.locator('#chatQuestion').fill('为新任务同步最新远程分支，先预览本次代码范围。');
    // Sending a new task must fetch automatically even without an explicit update click.
    await page.locator('#chatSend').click();
    await page.locator('#chatPreviewDialog[open]').waitFor();
    const updatedPreview = await page.locator('#chatPreviewText').innerText();
    assert(updatedPreview.includes(advanced.current_commit));
    assert(updatedPreview.includes(repository.branch));
    assert(!updatedPreview.includes(repository.initial_commit));
    await page.locator('#chatPreviewDialog .close').click();
    const unchanged = await (await page.request.get(historyUrl)).json();
    assert.deepEqual(unchanged.session.task, history.session.task,
      'fetching a new commit must not silently rewrite a saved conversation scope');
    assert.deepEqual(unchanged.events.filter(event => event.kind === 'scope' || event.kind === 'tool'),
      history.events.filter(event => event.kind === 'scope' || event.kind === 'tool'));

    await page.reload();
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();
    await openProjectFromTopButton();
    assert.equal(await page.locator('#chatRemoteUrl').inputValue(), repository.remote_url,
      'refresh should retain the chosen Git URL');
    await page.waitForFunction(id => document.querySelector('#chatRepository').value === id, cached.id);
    await page.locator('#chatRemoteUrl').fill('');
    await page.locator('#chatRepository').selectOption(cached.id);
    assert.equal(await page.locator('#chatRemoteUrl').inputValue(), repository.remote_url,
      'choosing a cached repository should fill its Git URL');
    await page.locator('#chatSyncProject').click();
    await page.waitForFunction(branch => !document.querySelector('#chatBranch').disabled &&
      document.querySelector('#chatBranch').value === branch, repository.branch);
    assert.equal((await (await page.request.get('http://127.0.0.1:8879/api/chat/projects')).json()).repositories.length, 1,
      'repeated updates of a remembered URL must reuse its managed clone');
    await sessionLoadRecoveryRegression(page, sessionId, history.session.task.project);
    page.on('dialog', dialog => dialog.accept());
    await page.locator('#chatDelete').click();
    await page.locator('#chatTitle').filter({hasText:'新建排查'}).waitFor();
    assert.deepEqual(errors, []);
  } catch (error) {
    await page.screenshot({path:'test-results/chat-project-failure.png',fullPage:true});
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
    const oldArchiveId = await page.locator('[data-retained-delete]').getAttribute('data-retained-delete');
    let deleteQuestion = '';
    page.once('dialog', async dialog => { deleteQuestion = dialog.message(); await dialog.accept(); });
    const [removedArchive] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/datasets/delete' &&
        response.request().method() === 'POST'),
      page.locator(`[data-retained-delete="${oldArchiveId}"]`).click(),
    ]);
    assert.equal(removedArchive.status(), 202);
    assert(deleteQuestion.includes('过期示例.zip'), 'manual ZIP deletion must confirm the selected archive name');
    assert.deepEqual(removedArchive.request().postDataJSON(), {dataset:oldArchiveId, compact:false});
    await page.locator(`[data-retained-delete="${oldArchiveId}"]`).waitFor({state:'hidden'});
    await page.locator('#retainedCount').filter({hasText:/^0$/}).waitFor();
    const remainingDatasets = await (await page.request.get('http://127.0.0.1:8879/api/datasets')).json();
    assert(!remainingDatasets.some(dataset => dataset.id === oldArchiveId),
      'deleting a legacy retained ZIP must remove its empty metadata row too');
    assert(remainingDatasets.some(dataset => dataset.state === 'ready'), 'other searchable packages must remain');
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
    let listFailures = 0;
    const failFirstListRefresh = route => listFailures++ === 0
      ? route.fulfill({status:503, json:{error:'测试：会话列表暂时无法刷新'}})
      : route.continue();
    await page.route('**/api/chat/sessions', failFirstListRefresh);
    await page.locator('#chatConfirm').click();
    await page.locator('#chatMessages').filter({hasText:'时间关联本身不能证明代码根因'}).waitFor();
    assert(listFailures >= 1, 'the first send must survive an actual failed sidebar refresh');
    await page.unroute('**/api/chat/sessions', failFirstListRefresh);
    const savedSessions = await (await page.request.get('http://127.0.0.1:8879/api/chat/sessions')).json();
    assert.equal(savedSessions.length, 1);
    const createdSessionId = savedSessions[0].id;
    assert.equal(await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected')), createdSessionId,
      'the successful send must save its conversation ID even when the list refresh fails');
    await page.locator('.chat-tool summary').first().click();
    await page.screenshot({path:'test-results/chat-desktop.png',fullPage:true});
    await page.locator('[data-chat-log]').first().click();
    await page.locator('#contextDialog[open]').waitFor();
    await page.locator('#contextDialog .close').click();
    await page.locator('#chatQuestion').fill('是否有下游连接池异常？');
    const [followUpPreview] = await Promise.all([
      page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/preview'),
      page.locator('#chatSend').click(),
    ]);
    assert.equal(followUpPreview.postDataJSON().id, createdSessionId,
      'a follow-up after a sidebar failure must target the original conversation');
    await page.locator('#chatPreviewTitle').filter({hasText:'继续当前对话'}).waitFor();
    await page.locator('#chatConfirm').click();
    await page.locator('#chatMessages').filter({hasText:'继续查看上下文可以验证该请求是否受下游连接池影响'}).waitFor();
    await page.reload();
    await page.locator('#chatMessages').filter({hasText:'是否有下游连接池异常？'}).waitFor();
    const sessionId = await page.locator('[data-chat-session].active').getAttribute('data-chat-session');
    assert(sessionId, 'restored conversation must appear as the active saved session');
    assert.equal(sessionId, createdSessionId);
    await page.locator('#chatQuestion').fill('保留未发送草稿');
    await reloadWithDelayedChat(page, sessionId);
    await page.locator('#chatRules').click();
    await page.locator('#rulesDialog[open]').waitFor();
    await page.locator('#rulesDialog .close').click();
    await page.locator('#chatNew').click();
    await page.locator('#chatTitle').filter({hasText:'新建排查'}).waitFor();
    await page.locator(`[data-chat-session="${sessionId}"]`).click();
    await page.locator('#chatMessages').filter({hasText:'是否有下游连接池异常？'}).waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'test-results/chat-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'mobile horizontal overflow');
    assert.deepEqual(errors,[]);
    page.on('dialog', dialog=>dialog.accept());
    await page.locator('#chatDelete').click();
    await page.locator('#chatTitle').filter({hasText:'新建排查'}).waitFor();
    assert.equal(await page.locator('[data-chat-session]').count(),0);
    await projectRepositoryRegression(browser, terminalRequests);
    assert.deepEqual(terminalRequests, [], 'all browser flows must avoid removed terminal APIs');
    console.log('Browser regression passed: maintenance 503/drafts/progress/cancel/recovery, legacy ZIP/download/delete, terminal-view migration, no terminal API requests, native rules save/preview, tools, evidence, follow-up across list failure, history loading/error/retry guards, delayed chat startup/reload, drafts, sessions, mobile, delete, Git URL clone/fetch/branch/commit/history/refresh.');
  } catch(error) {
    await page.screenshot({path:'test-results/chat-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
