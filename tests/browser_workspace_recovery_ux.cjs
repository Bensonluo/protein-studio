/* Recovery and navigation regressions: isolated static server; every API request is mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const checks = [], errors = [], requests = [];
const jobs = [{id:'job-a', name:'恢复测试任务', engine:'reference', status:'queued',
  updatedAt:'2026-10-23T10:00:00Z', chains:[{id:'A', sequence:'ACDE'}]}];
let server, browser, detailFails = false, listFails = false, engineAvailable = true;
let postRoute, resolvePost;
const postReceived = new Promise(resolve => { resolvePost = resolve; });

async function respond(route, status, data) {
  await route.fulfill({status, contentType:'application/json', body:JSON.stringify(data)});
}

(async () => {
  server = http.createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, 'http://localhost').pathname;
      const file = path.resolve(root, 'web', pathname === '/' ? 'index.html' : decodeURIComponent(pathname.slice(1)));
      if (!file.startsWith(path.join(root, 'web') + path.sep)) {
        res.writeHead(403).end(); return;
      }
      const bytes = await fs.readFile(file);
      res.writeHead(200, {'Content-Type':({'.html':'text/html', '.js':'text/javascript', '.css':'text/css'})[path.extname(file)] || 'application/octet-stream'});
      res.end(bytes);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  browser = await chromium.launch({
    executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless:true, args:['--no-sandbox']
  });
  const page = await browser.newPage({viewport:{width:390, height:844}});
  page.on('pageerror', error => errors.push(error.message));
  // Poll deterministically at the exact failure/recovery boundaries below.
  await page.addInitScript(() => { window.setInterval = () => 0; });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    requests.push({method, path:url.pathname});
    if (method === 'POST') {
      assert.equal(url.pathname, '/api/jobs', 'Only the explicitly mocked submission is expected');
      postRoute = route; resolvePost(); return;
    }
    assert.equal(method, 'GET');
    if (url.pathname === '/api/health') return respond(route, 200, {status:'ok'});
    if (url.pathname === '/api/system') return respond(route, 200, {
      engines:[{id:'reference', available:true, defaultNumSamples:1},
        {id:'alphafold3', available:engineAvailable, defaultNumSamples:5, maxNumSamples:20}],
      pipeline:{predictionMode:'local'}
    });
    if (url.pathname === '/api/jobs') return respond(route, listFails ? 503 : 200,
      listFails ? {error:'Fixture list connection failure'} : jobs);
    if (url.pathname === '/api/jobs/job-a') return respond(route, detailFails ? 503 : 200,
      detailFails ? {error:'Fixture detail connection failure'} : jobs.find(job => job.id === 'job-a'));
    if (url.pathname === '/api/examples/ubiquitin-afdb') return respond(route, 200,
      {name:'Fixture example', chains:[{id:'A', sequence:'ACDE'}]});
    if (url.pathname === '/api/campaigns' || url.pathname === '/api/databases') return respond(route, 200, []);
    if (url.pathname === '/api/pipeline/config') return respond(route, 200, {capabilities:[]});
    if (url.pathname === '/api/cluster') return respond(route, 200, {nodes:[], batches:[], transfers:[], events:[]});
    throw Error('Unexpected API request: ' + url.pathname);
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/#studio`);
  await page.waitForSelector('.job-detail-bar');

  await page.locator('[data-view="jobs"]').click();
  assert.match(await page.locator('[data-job-id="job-a"]').textContent(), /排队中/);
  jobs[0].status = 'running'; jobs[0].updatedAt = '2026-10-23T11:00:00Z';
  detailFails = true;
  await page.evaluate(() => pollWorkspace());
  assert.match(await page.locator('[data-job-id="job-a"]').textContent(), /运行中/,
    'A successful list update must reach the table even when selected-job detail fails');
  assert.equal(await page.locator('#service-notice').isVisible(), true);
  detailFails = false;
  await page.evaluate(() => pollWorkspace());
  assert.equal(await page.evaluate(() => state.job.status), 'running');
  assert.match(await page.locator('[data-job-id="job-a"]').textContent(), /运行中/);
  assert.equal(await page.locator('#service-notice').isVisible(), false);
  checks.push({name:'Queue updates survive detail failure and subsequent recovery', status:'passed'});

  await page.locator('#new-job-button').click();
  await page.waitForFunction(() => document.querySelector('#sequence-input').value.length > 0);
  await page.locator('[data-engine="alphafold3"]').click();
  await page.locator('#job-name').fill('Accepted navigation fixture');
  await page.locator('#sequence-input').fill('ACDE');
  await page.locator('#submit-job-button').click();
  await postReceived;
  await page.keyboard.press('Escape');
  await page.locator('[data-view="environment"]').click();
  assert.equal(await page.evaluate(() => state.view), 'environment');
  const accepted = {...jobs[0], id:'accepted-job', name:'Accepted navigation fixture', status:'queued'};
  jobs.push(accepted);
  await respond(postRoute, 200, accepted);
  await page.waitForFunction(() => !state.submitting);
  assert.match(page.url(), /#environment$/);
  assert.equal(await page.evaluate(() => state.view), 'environment');
  assert.equal(await page.evaluate(() => state.selectedId), 'job-a');
  assert.equal(await page.evaluate(() => state.jobs.some(job => job.id === 'accepted-job')), true);
  assert.match(await page.locator('#toast-container').textContent(), /任务已提交/);
  checks.push({name:'Delayed accepted submission preserves later navigation and records the accepted job', status:'passed'});

  // Refresh to an unavailable engine, then recover with the same form still open.
  listFails = true; await page.evaluate(() => pollWorkspace());
  listFails = false; engineAvailable = false; await page.evaluate(() => pollWorkspace());
  await page.locator('#new-job-button').click();
  await page.locator('[data-engine="alphafold3"]').click();
  assert.equal(await page.locator('#submit-job-button').isDisabled(), true);
  await page.locator('#job-seeds').fill('7, 19');
  await page.locator('#job-samples').fill('2');
  await page.locator('#job-recycles').fill('7');
  listFails = true; await page.evaluate(() => pollWorkspace());
  listFails = false; engineAvailable = true; await page.evaluate(() => pollWorkspace());
  assert.equal(await page.locator('#job-dialog').evaluate(element => element.open), true);
  assert.equal(await page.evaluate(() => state.engines.find(engine => engine.id === 'alphafold3').available), true);
  assert.equal(await page.locator('#submit-job-button').isDisabled(), false);
  assert.match(await page.locator('[data-engine="alphafold3"]').textContent(), /已配置运行器/);
  assert.equal(await page.locator('#job-seeds').inputValue(), '7, 19');
  assert.equal(await page.locator('#job-samples').inputValue(), '2');
  assert.equal(await page.locator('#job-recycles').inputValue(), '7');
  checks.push({name:'Reconnect updates the open form engine availability without replacing its parameter draft', status:'passed'});

  assert.deepEqual(errors, []);
  assert.equal(requests.filter(request => request.method === 'POST').length, 1);
  const report = {status:'passed', count:checks.length, checks, errors,
    scope:'Isolated static HTTP server with every API mocked; no localhost:8765 access, real task mutation, model execution or deployment.'};
  await fs.mkdir(path.join(root, 'artifacts'), {recursive:true});
  await fs.writeFile(path.join(root, 'artifacts/operations-ux-workspace-recovery.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
})().catch(error => {
  console.error(error); process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});
