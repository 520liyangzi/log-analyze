// Real ZIP upload, review, draft, confirmation and search. Generated fixtures only.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const origin = 'http://127.0.0.1:8880';
const fixture = path.resolve('test-results/import-fixture.zip');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function ready(page) {
  for (let attempt = 0; attempt < 30; attempt++) {
    try {
      await page.goto(origin, {waitUntil:'domcontentloaded'});
      return;
    } catch (error) {
      if (attempt === 29) throw error;
      await pause(1000);
    }
  }
}

async function json(page, endpoint) {
  const response = await page.request.get(origin + endpoint);
  assert.equal(response.ok(), true, `${endpoint}: ${response.status()} ${await response.text()}`);
  return response.json();
}

async function waitForReview(page, id) {
  for (let attempt = 0; attempt < 60; attempt++) {
    const preview = await json(page, `/api/imports/preview?dataset=${id}`);
    assert.notEqual(preview.state, 'failed', preview.error);
    if (preview.state === 'review') return preview;
    await pause(250);
  }
  throw new Error('ZIP scan did not reach review within 15 seconds');
}

async function assertNotIndexed(page, id) {
  const datasets = await json(page, '/api/datasets');
  const dataset = datasets.find(item => item.id === id);
  assert(dataset, 'pending ZIP must remain visible after upload');
  assert.equal(dataset.state, 'review');
  assert.equal(dataset.records, 0, 'directory scan must not build log records');
  const response = await page.request.get(`${origin}/api/search?dataset=${id}&q=browser-import`);
  assert([400, 409].includes(response.status()), 'search must reject an unconfirmed import');
  assert((await response.json()).error, 'rejected search should explain why it is unavailable');
}

async function search(page, id, marker) {
  return json(page, `/api/search?dataset=${id}&q=${encodeURIComponent(marker)}`);
}

async function cancelPendingImportRegression(page, retainedId, dialogs) {
  // A second real ZIP remains at review while the first package stays searchable.
  const pendingFixture = path.resolve('test-results/import-cancel-fixture.zip');
  fs.copyFileSync(fixture, pendingFixture);
  await page.locator('#topUpload').click();
  await page.locator('#uploadFile').setInputFiles(pendingFixture);
  const [uploaded] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/upload' && response.request().method() === 'POST'),
    page.locator('#uploadSubmit').click(),
  ]);
  assert.equal(uploaded.status(), 202);
  await page.locator('#importLayoutDialog[open]').waitFor();
  const id = await page.locator('#layoutTaskPicker').inputValue();
  assert.notEqual(id, retainedId);
  await waitForReview(page, id);
  await page.locator('#layoutConfirm:not([disabled])').waitFor();
  await page.locator('.layout-group [data-layout-field="node"]').first().fill('unsaved-cancel-draft');
  const draftKey = 'logscope.import-draft.v1.' + id;
  await page.waitForFunction(key => localStorage.getItem(key)?.includes('unsaved-cancel-draft'), draftKey);
  await page.locator('#importLayoutDialog .close').click();
  await page.locator('#importLayoutDialog').waitFor({state:'hidden'});

  const row = page.locator(`[data-import-task="${id}"]`);
  const cancel = page.locator(`[data-import-cancel="${id}"]`);
  await cancel.waitFor({state:'visible'});
  assert.match(await cancel.innerText(), /取消导入/);
  assert.match(await row.innerText(), /等待确认/);
  assert.equal(await cancel.isEnabled(), true);
  await page.locator('#importReviewQueue').screenshot({path:'test-results/import-cancel-pending.png'});
  await page.setViewportSize({width:390,height:844});
  await page.locator('#importReviewQueue').screenshot({path:'test-results/import-cancel-mobile.png'});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
    'pending task actions must remain usable without horizontal page overflow');
  await page.setViewportSize({width:1440,height:1120});

  let requests = 0;
  const deleteRoute = async route => {
    const body = route.request().postDataJSON();
    if (body.dataset !== id) return route.continue();
    requests++;
    assert.equal(body.compact, false, 'cancelling an unindexed ZIP must not compact the shared database');
    assert.equal(body.import_only, true, 'stale review pages must not delete an already completed import');
    if (requests === 1) return route.fulfill({status:503,contentType:'application/json',
      body:JSON.stringify({error:'取消服务暂不可用，请重试'})});
    return route.continue();
  };
  await page.route('**/api/datasets/delete', deleteRoute);
  try {
    dialogs.accept = false;
    await cancel.click();
    assert.match(dialogs.lastMessage, /ZIP/);
    assert.equal(requests, 0, 'dismissing confirmation must not send a deletion request');
    await assertNotIndexed(page, id);
    assert(await page.evaluate(key => localStorage.getItem(key), draftKey), 'declining cancellation must preserve the local draft');

    dialogs.accept = true;
    await cancel.click();
    await row.locator('.import-cancel-error').filter({hasText:'取消服务暂不可用'}).waitFor();
    assert.equal(await cancel.isEnabled(), true, 'failed cancellation must expose a usable retry');
    assert.equal(await row.locator('[data-import-open]').isEnabled(), true);
    await assertNotIndexed(page, id);
    assert(await page.evaluate(key => localStorage.getItem(key), draftKey), 'failed cancellation must preserve the local draft');

    const [deleted] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/datasets/delete' && response.status() === 202),
      cancel.click(),
    ]);
    assert.equal(deleted.status(), 202);
    await row.waitFor({state:'detached',timeout:15000});
    assert.equal(requests, 2, 'retry should send one new deletion request');
    assert.equal(await page.evaluate(key => localStorage.getItem(key), draftKey), null,
      'completed cancellation must remove this browser\'s unsaved draft');
    assert.equal((await json(page, '/api/datasets')).some(item => item.id === id), false);
    for (const endpoint of ['/api/imports/preview', '/api/archives/download']) {
      const response = await page.request.get(`${origin}${endpoint}?dataset=${id}`);
      assert([400, 404].includes(response.status()), `${endpoint} must not expose the cancelled task or ZIP`);
    }
    await page.reload();
    await page.locator('#dataset option').filter({hasText:'import-fixture.zip'}).waitFor({state:'attached'});
    assert.equal(await page.locator(`[data-import-task="${id}"]`).count(), 0,
      'cancelled pending task must stay removed after reload');
    assert.equal(await page.evaluate(key => localStorage.getItem(key), draftKey), null,
      'closing or unloading the old review must not resurrect its cancelled draft');
    assert.equal((await search(page, retainedId, 'browser-import-special-abc')).summary.total, 1,
      'cancelling the pending ZIP must leave other indexed logs searchable');
  } finally {
    dialogs.accept = true;
    await page.unroute('**/api/datasets/delete', deleteRoute);
  }
}

(async () => {
  fs.mkdirSync('test-results', {recursive:true});
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1120}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const dialogs = {accept:true,lastMessage:''};
  page.on('dialog', dialog => {
    dialogs.lastMessage = dialog.message();
    return dialogs.accept ? dialog.accept() : dialog.dismiss();
  });
  try {
    await ready(page);
    assert(fs.existsSync(fixture), 'fixture server must generate the ZIP before accepting requests');
    const importNavigations = [];
    const recordNavigation = frame => {
      if (frame === page.mainFrame()) importNavigations.push(frame.url());
    };
    page.on('framenavigated', recordNavigation);
    await page.locator('#topUpload').click();
    await page.locator('#uploadFile').setInputFiles(fixture);
    const [uploaded] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/upload' && response.request().method() === 'POST'),
      page.locator('#uploadSubmit').click(),
    ]);
    assert.equal(uploaded.status(), 202);
    assert.equal(uploaded.request().resourceType(), 'xhr');
    assert.equal(uploaded.request().isNavigationRequest(), false);
    await page.locator('#importLayoutDialog[open]').waitFor();
    // Chromium may evict completed XHR bodies from the inspector cache even
    // though the application consumed them. Read the visible task identity,
    // then verify persisted state through the independent HTTP request client.
    const upload = {id: await page.locator('#layoutTaskPicker').inputValue()};
    assert.match(upload.id, /^[a-f0-9]{32}$/);
    assert.equal(page.url(), origin + '/');
    assert.deepEqual(importNavigations, [], 'upload must open review without navigating away');
    const preview = await waitForReview(page, upload.id);
    await page.locator('#layoutConfirm:not([disabled])').waitFor();
    await assertNotIndexed(page, upload.id);

    const custom = preview.groups.find(group => group.directory === 'custom/logs');
    const excluded = preview.groups.find(group => group.directory.includes('excluded-pod'));
    const legacy = preview.groups.find(group => group.directory.includes('legacy-pod'));
    assert(custom && excluded && legacy, 'nested archive and custom directories should all be reviewable');
    const group = id => page.locator(`.layout-group[data-group-id="${id}"]`);
    assert.equal(await group(legacy.id).locator('[data-layout-field="included"]').isChecked(), true,
      'recognized legacy log directory should be suggested by default');
    await group(excluded.id).locator('[data-layout-field="included"]').uncheck();
    await page.locator('#layoutFilter').fill('custom/logs');
    await group(custom.id).locator('[data-layout-field="included"]').check();
    const mappings = {patterns:'*.log, root, stdout, *.abc', node:'edited-node', namespace:'staging',
      pod:'edited-pod', service:'edited-service', kind:'custom'};
    for (const [field, value] of Object.entries(mappings)) {
      await group(custom.id).locator(`[data-layout-field="${field}"]`).fill(value);
    }
    await group(custom.id).locator('[data-layout-field="line_mode"]').selectOption('lines');
    await group(custom.id).locator('details.layout-files > summary').click();
    const [saved] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/imports/draft' && response.request().method() === 'POST'),
      page.locator('#layoutSave').click(),
    ]);
    assert.equal(saved.status(), 200);
    await page.locator('#layoutSave:not([disabled])').waitFor();
    const savedPreview = await json(page, `/api/imports/preview?dataset=${upload.id}`);
    assert.notEqual(savedPreview.revision, preview.revision, 'saving must persist a new draft revision');
    const savedGroup = savedPreview.groups.find(item => item.id === custom.id);
    assert.equal(savedGroup.line_mode, 'lines');
    for (const [field, value] of Object.entries(mappings)) assert.equal(savedGroup[field], value);
    assert.equal(savedPreview.groups.find(item => item.id === excluded.id).included, false);
    assert.deepEqual(importNavigations, [], 'saving a draft must not navigate the page');
    page.off('framenavigated', recordNavigation);

    // Saving alone must not start indexing, and the draft must survive a full reload.
    await assertNotIndexed(page, upload.id);
    await page.locator('#importLayoutDialog .close').click();
    await page.reload();
    await page.locator(`[data-import-open="${upload.id}"]`).click();
    await page.locator('#importLayoutDialog[open]').waitFor();
    await page.locator('#layoutConfirm:not([disabled])').waitFor();
    await page.locator('#layoutFilter').fill('custom/logs');
    for (const [field, value] of Object.entries(mappings)) {
      assert.equal(await group(custom.id).locator(`[data-layout-field="${field}"]`).inputValue(), value);
    }
    assert.equal(await group(custom.id).locator('[data-layout-field="line_mode"]').inputValue(), 'lines');
    assert.equal(await group(custom.id).locator('[data-layout-field="included"]').isChecked(), true);
    await page.locator('#importLayoutDialog').evaluate(dialog => { dialog.scrollTop = 0; });
    await page.screenshot({path:'test-results/import-review-desktop.png',fullPage:true});

    await page.setViewportSize({width:390,height:844});
    await page.locator('#importLayoutDialog').evaluate(dialog => { dialog.scrollTop = 0; });
    await page.screenshot({path:'test-results/import-review-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
      'mobile page must not overflow horizontally');
    assert.equal(await page.locator('#importLayoutDialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth + 1), false,
      'mobile preview must wrap archive paths and editable fields');
    await page.setViewportSize({width:1440,height:1120});

    const [confirmed] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/imports/confirm' && response.request().method() === 'POST'),
      page.locator('#layoutConfirm').click(),
    ]);
    assert.equal(confirmed.status(), 202);
    await page.locator('#importLayoutDialog').waitFor({state:'hidden'});
    await page.waitForFunction(id => document.querySelector('#dataset').value === id &&
      document.querySelector('#datasetInfo').textContent.includes('份日志文件'), upload.id, {timeout:30000});
    const datasets = await json(page, '/api/datasets');
    const dataset = datasets.find(item => item.id === upload.id);
    assert.equal(dataset.state, 'ready');
    assert.equal(dataset.records, 6, 'line mode must preserve the extra plain line as its own record');

    const customMarkers = ['custom-log', 'extensionless-root', 'stdout', 'special-abc', 'plain-line'];
    for (const marker of customMarkers) {
      const result = await search(page, upload.id, 'browser-import-' + marker);
      assert.equal(result.summary.total, 1, `custom pattern should import ${marker}`);
      for (const field of ['node', 'namespace', 'pod', 'service', 'kind']) {
        assert.equal(result.rows[0][field], mappings[field], `persisted metadata: ${field}`);
      }
    }
    assert.equal((await search(page, upload.id, 'browser-import-legacy')).summary.total, 1);
    for (const marker of ['excluded', 'unselected-text']) {
      assert.equal((await search(page, upload.id, 'browser-import-' + marker)).summary.total, 0,
        `${marker} must not be imported outside confirmed selection and patterns`);
    }
    await page.locator('[data-view="search"]').click();
    await page.locator('#query').fill('browser-import-special-abc');
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/search'),
      page.locator('#searchForm button[type="submit"]').click(),
    ]);
    await page.locator('#searchResults .log-list').filter({hasText:'browser-import-special-abc'}).waitFor();
    assert((await page.locator('#searchResults').innerText()).includes('edited-pod'));
    await page.screenshot({path:'test-results/import-search-desktop.png',fullPage:true});
    await cancelPendingImportRegression(page, upload.id, dialogs);
    assert.deepEqual(errors, []);
    console.log('Import browser regression passed: real upload, scan-only review, editable mappings, persistent draft, indexed search, responsive preview, direct pending-task cancellation, declined confirmation, failed cancellation retry, draft cleanup and reload persistence.');
  } catch (error) {
    await page.screenshot({path:'test-results/import-failure.png',fullPage:true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
