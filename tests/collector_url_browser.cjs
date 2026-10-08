// Platform address validation uses an isolated local server and synthetic credentials.
// Collection is intercepted: no external platform or user script is ever contacted.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const origin = 'http://127.0.0.1:8880';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function ready(page) {
  for (let attempt = 0; attempt < 30; attempt++) {
    try { await page.goto(origin, {waitUntil:'domcontentloaded'}); return; }
    catch (error) { if (attempt === 29) throw error; await pause(1000); }
  }
}

async function saveEnvironment(page) {
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/collector/environments' && response.request().method() === 'POST'),
    page.locator('#environmentForm button[type="submit"]').click(),
  ]);
  assert.equal(response.status(), 200, await response.text());
  const saved = await response.json();
  await page.locator(`[data-environment-id="${saved.id}"]`).waitFor();
  await page.waitForFunction(saved => document.querySelector('#environmentId').value === saved.id &&
    document.querySelector('#environmentPassword').value === '' &&
    document.querySelector(`[data-environment-id="${saved.id}"] span`)?.textContent === saved.url, saved);
  return saved;
}

(async () => {
  fs.mkdirSync('test-results', {recursive:true});
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let saves = 0;
  let starts = 0;
  let lastStart;
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/collector/environments' && request.method() === 'POST') saves++;
  });
  await page.route('**/api/collector/capability', route => route.fulfill({status:200,contentType:'application/json',
    body:JSON.stringify({available:true,script:'synthetic-collect-fixture.py',reason:''})}));
  await page.route('**/api/collector/start', route => {
    starts++;
    lastStart = route.request().postDataJSON();
    return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'浏览器测试：采集请求已拦截'})});
  });
  try {
    await ready(page);
    await page.locator('#topEnvironments').click();
    await page.locator('#environmentDialog[open]').waitFor();
    await page.locator('#environmentName').fill('浏览器地址校验');
    await page.locator('#environmentUser').fill('synthetic-admin');
    await page.locator('#environmentPassword').fill('synthetic-password');
    for (const [address, message] of [
      ['https://192.0.2.10', /端口/],
      ['https://192.0.2.10:31945/', /\/|斜杠/],
      ['https://192.0.2.10:65536', /65535/],
    ]) {
      await page.locator('#environmentUrl').fill(address);
      await page.locator('#environmentForm button[type="submit"]').click();
      await page.locator('#environmentUrlError').waitFor({state:'visible'});
      assert.match(await page.locator('#environmentUrlError').innerText(), message);
      assert.equal(await page.locator('#environmentUrl').getAttribute('aria-invalid'), 'true');
      assert.equal(saves, 0, 'invalid environments must be blocked before the save API');
      assert.equal(await page.locator('#environmentPassword').inputValue(), 'synthetic-password');
    }
    await page.locator('#environmentUrl').fill('https://192.0.2.10:31945/');
    await page.locator('#environmentUser').focus();
    await page.screenshot({path:'test-results/environment-url-validation.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'test-results/environment-url-validation-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.setViewportSize({width:1440,height:1000});

    await page.locator('#environmentUrl').fill('https://192.0.2.10:31945');
    const created = await saveEnvironment(page);
    assert.equal(created.url, 'https://192.0.2.10:31945');
    assert.equal(created.has_password, true);
    assert.equal(Object.hasOwn(created, 'password'), false);
    assert.equal(saves, 1);
    // Editing with an empty password must retain the saved secret and accept an explicit default port.
    await page.locator('#environmentUrl').fill('https://192.0.2.10:443');
    await page.locator('#environmentUser').fill('synthetic-operator');
    const edited = await saveEnvironment(page);
    assert.equal(edited.id, created.id);
    assert.equal(edited.url, 'https://192.0.2.10:443');
    assert.equal(edited.has_password, true);
    assert.equal(edited.user, 'synthetic-operator');
    assert.equal(await page.locator('#environmentPassword').inputValue(), '');
    const stored = await (await page.request.get(origin + '/api/collector/environments')).json();
    assert.equal(stored.find(item => item.id === created.id).url, edited.url);
    assert.equal(JSON.stringify(stored).includes('synthetic-password'), false);
    await page.locator('#environmentDialog .close').click();

    await page.locator('#topCollect').click();
    await page.locator('#collectSubmit:not([disabled])').waitFor();
    await page.locator('#collectEnvironment').selectOption(created.id);
    assert.equal(await page.locator('#collectUrl').inputValue(), edited.url);
    assert.equal(await page.locator('#collectUrl').isDisabled(), true);
    assert.equal(await page.locator('#collectPassword').inputValue(), '');
    await page.locator('#collectEnvironment').selectOption('');
    await page.locator('#collectPod').fill('synthetic-pod');
    await page.locator('#collectStart').fill('2026-09-10 14:00:00');
    await page.locator('#collectEnd').fill('2026-09-10 16:30:00');
    await page.locator('#collectUser').fill('synthetic-admin');
    await page.locator('#collectPassword').fill('synthetic-password');
    for (const address of ['https://192.0.2.10', 'https://192.0.2.10:31945/']) {
      await page.locator('#collectUrl').fill(address);
      await page.locator('#collectSubmit').click();
      await page.locator('#collectUrlError').waitFor({state:'visible'});
      assert.match(await page.locator('#collectUrlError').innerText(), /端口/);
      assert.equal(starts, 0, 'invalid manual collection must never send a start request');
    }
    await page.locator('#collectUrl').fill('http://192.0.2.10:80');
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/collector/start'),
      page.locator('#collectSubmit').click(),
    ]);
    await page.locator('#collectSubmit:not([disabled])').waitFor();
    assert.equal(starts, 1);
    assert.equal(lastStart.url, 'http://192.0.2.10:80', 'explicit default ports must not be dropped');
    assert.equal(lastStart.environment_id, '');
    await page.locator('#collectDialog .close').click();

    // Read old persisted data without rewriting it. The disabled address field must still be checked.
    await page.route('**/api/collector/environments', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify([
        ...stored, {id:'legacy-invalid',name:'旧环境需补端口',url:'https://192.0.2.20',user:'legacy-admin',has_password:true},
      ])});
    });
    await page.locator('#topCollect').click();
    await page.locator('#collectEnvironment option[value="legacy-invalid"]').waitFor({state:'attached'});
    await page.locator('#collectSubmit:not([disabled])').waitFor();
    await page.locator('#collectEnvironment').selectOption('legacy-invalid');
    assert.equal(await page.locator('#collectUrl').isDisabled(), true);
    await page.locator('#collectSubmit').click();
    await page.locator('#collectUrlError').waitFor({state:'visible'});
    assert.match(await page.locator('#collectUrlError').innerText(), /管理环境/);
    assert.match(await page.locator('#collectUrlError').innerText(), /端口/);
    assert.equal(starts, 1, 'legacy invalid saved addresses must not launch collection');
    await page.screenshot({path:'test-results/collector-legacy-url-validation.png',fullPage:true});
    assert.deepEqual(errors, []);
    console.log('Collector URL browser regression passed: inline errors, blocked invalid saves and collection, valid correction, editing with preserved secret, explicit default ports, responsive hints and legacy saved environments.');
  } catch (error) {
    await page.screenshot({path:'test-results/collector-url-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
