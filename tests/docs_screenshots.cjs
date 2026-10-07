// README images: actual UI + generated demo.zip + local mock model and Git.
// No DOM replacements, hidden product controls, private logs or external APIs.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const origin = 'http://127.0.0.1:8881';
const output = path.resolve('test-results/docs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function ready(page) {
  for (let attempt=0; attempt<30; attempt++) {
    try { await page.goto(origin, {waitUntil:'domcontentloaded'}); return; }
    catch (error) { if (attempt===29) throw error; await pause(1000); }
  }
}

async function capture(page, name, {fullPage=false, selector}={}) {
  await page.evaluate(() => document.fonts.ready);
  const options={path:path.join(output,name+'.png'), animations:'disabled'};
  if(selector) await page.locator(selector).screenshot(options);
  else await page.screenshot({...options,fullPage});
}

async function send(page, question, expected) {
  await page.locator('#chatQuestion').fill(question);
  await page.locator('#chatSend').click();
  await page.locator('#chatPreviewDialog[open]').waitFor();
  await page.locator('#chatConfirm').click();
  await page.locator('#chatPreviewDialog').waitFor({state:'hidden'});
  await page.locator('#chatMessages').filter({hasText:expected}).waitFor();
  await page.waitForFunction(() => !document.querySelector('#chatSend').disabled);
}

(async () => {
  fs.mkdirSync(output,{recursive:true});
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1120}, deviceScaleFactor:1});
  page.setDefaultTimeout(30000);
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await ready(page);
    await page.locator('#topUpload').click();
    await page.locator('#uploadFile').setInputFiles(path.join(output,'demo-logs.zip'));
    await page.locator('#uploadSubmit').click();
    await page.locator('#importLayoutDialog[open]').waitFor();
    await page.locator('#layoutConfirm:not([disabled])').waitFor();
    // The root manifest is also listed for review, but is not an included log
    // directory. Count only selected directories rather than all scanned groups.
    assert.equal(await page.locator('.layout-group [data-layout-field="included"]:checked').count(),2);
    await page.locator('#layoutFilter').fill('node-a');
    assert.equal(await page.locator('.layout-group').count(),1);
    await page.locator('#importLayoutDialog').evaluate(dialog => {dialog.scrollTop=0;});
    await capture(page,'import-layout',{selector:'#importLayoutDialog'});
    await page.locator('#layoutFilter').fill('');
    await page.locator('#layoutConfirm').click();
    await page.locator('#importLayoutDialog').waitFor({state:'hidden'});
    await page.locator('#datasetInfo').filter({hasText:'份日志文件'}).waitFor();
    // Let the success toast finish; no product text or layout is changed for images.
    await page.locator('#toast').waitFor({state:'hidden'});

    await page.locator('#query').fill('/api/model/map');
    await page.locator('#kind').selectOption('access');
    await page.locator('#advanced summary').click();
    await page.locator('#status').fill('5xx');
    await page.locator('#searchForm button[type="submit"]').click();
    await page.locator('#searchResults .log-list').filter({hasText:'3051'}).waitFor();
    assert.equal(await page.locator('#searchResults .log-row').count(),1);
    await page.locator('#advanced summary').click();
    await page.evaluate(() => window.scrollTo(0,0));
    await capture(page,'search');

    await page.locator('[data-view="trace"]').click();
    await page.locator('#traceId').fill('9124859898865451127');
    await page.locator('#traceForm button').click();
    await page.locator('#traceResults .log-list').filter({hasText:'HikariPool'}).waitFor();
    assert.equal(await page.locator('#traceResults .log-row').count(),6);
    await page.evaluate(() => window.scrollTo(0,0));
    await capture(page,'trace',{fullPage:true});

    await page.locator('[data-view="chat"]').click();
    await page.locator('#chatCapability').filter({hasText:'共享模型已配置'}).waitFor();
    await send(page,'帮我排查 /api/model/map 接口的 500 错误和慢请求。','3051 ms');
    const sessionId=await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected'));
    await send(page,'继续追踪流水号 9124859898865451127，看看异常堆栈。','不能只凭这一条日志断言最终根因');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('logscope.chat.selected')),sessionId);
    assert.equal(await page.locator('#chatMessages .chat-event.user').count(),2);
    assert.equal(await page.locator('#chatMessages .chat-tool').count(),2);
    await page.locator('#chatQuestion').fill('下一步结合部署分支，检查连接释放与超时配置。');
    await page.locator('#chatMessages').evaluate(box => {box.scrollTop=box.scrollHeight;});
    await page.evaluate(() => window.scrollTo(0,0));
    await capture(page,'ai-chat',{fullPage:true});

    const fixture=JSON.parse(fs.readFileSync(path.join(output,'fixture.json'),'utf8'));
    await page.locator('#chatOpenProject').click();
    await page.locator('dialog#chatProjectOptions[open]').waitFor();
    await page.locator('#chatRemoteUrl').fill(fixture.remote_url);
    await page.locator('#chatSyncProject').click();
    await page.waitForFunction(branch => !document.querySelector('#chatBranch').disabled &&
      [...document.querySelector('#chatBranch').options].some(option => option.value===branch),fixture.branch);
    await page.locator('#chatBranch').selectOption(fixture.branch);
    await page.locator('#toast').waitFor({state:'hidden'});
    await capture(page,'project-settings',{selector:'#chatProjectOptions'});
    assert.deepEqual(errors,[]);
    console.log('Captured 5 README screenshots with generated logs, a loopback mock model and local Git.');
  } catch (error) {
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
    throw error;
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exit(1);});
