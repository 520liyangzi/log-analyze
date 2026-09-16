// Run against tests/chat_browser_server.py. Screenshots contain generated logs only.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1120}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('test-results', {recursive:true});
  try {
    for(let i=0;i<30;i++) {
      try { await page.goto('http://127.0.0.1:8879'); break; }
      catch(error) { if(i===29)throw error; await new Promise(resolve=>setTimeout(resolve,1000)); }
    }
    await page.locator('#datasetInfo').filter({hasText:'份日志文件'}).waitFor();
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
    console.log('Native chat browser regression passed: preview, tools, evidence, follow-up, reload, drafts, rules, sessions, mobile, delete.');
  } catch(error) {
    await page.screenshot({path:'test-results/chat-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
