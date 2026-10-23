/* Isolated UI regressions. Every /api request is intercepted; no real mutation. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[],errors=[],requests=[];
let server,browser,holdPlan=false,pending=[];
const plan={id:'ux-plan',digest:'fixture-digest',action:'quarantine',status:'planned',scanId:'ux-scan',count:120,logicalBytes:120*1024};
const items=Array.from({length:120},(_,i)=>({id:'entry-'+i,kind:'whole',relativePath:'cache/entry-'+String(i).padStart(3,'0'),logicalBytes:1024}));
const snapshot={capabilities:{scan:true,quarantine:true,restore:true,purge:false},disk:{freeBytes:1024**3,totalBytes:10*1024**3},
  state:{latestScan:{id:'ux-scan',status:'completed',inventoryComplete:true,truncated:false,failClosed:false,logicalBytes:120*1024,byKind:{whole:{entries:120,logicalBytes:120*1024}}}},
  job:{id:'ux-job',action:'plan',status:'completed',result:plan}};
const cluster={enabled:true,planned:{localGpuCards:16},nodes:[{id:'ux-node',name:'键盘操作测试节点',location:'local',engine:'reference',demo:true,status:'online',processedTargets:1,modelLoads:0}],
  batches:[{id:'ux-batch',nodeId:'ux-node',status:'running',total:2,completed:1,remaining:1,members:[{itemId:'ux-item',campaignId:'ux-campaign',name:'测试目标',stage:'ready'}]}],
  transfers:[{id:'ux-transfer',nodeId:'ux-node',direction:'download',bytes:1024,sha256:'a'.repeat(64),status:'verified'}],events:[]};
function response(url){
  if(url.pathname==='/api/cluster')return cluster;
  if(url.pathname==='/api/storage')return snapshot;
  if(url.pathname==='/api/storage/entries')return {items:items.slice(0,50),total:120};
  if(url.pathname.startsWith('/api/storage/plans/')){const offset=Number(url.searchParams.get('offset')||0);return {plan,items:items.slice(offset,offset+50),total:120,offset,limit:50};}
  if(url.pathname==='/api/health')return {status:'ok'};
  if(url.pathname==='/api/system')return {engines:[]};
  if(url.pathname==='/api/jobs')return [{id:'ux-placeholder',engine:'reference',status:'queued',chains:[]}];
  if(url.pathname==='/api/jobs/ux-placeholder')return {id:'ux-placeholder',name:'fixture only',engine:'reference',status:'queued',chains:[]};
  if(url.pathname==='/api/campaigns'||url.pathname==='/api/databases')return [];
  if(url.pathname==='/api/pipeline/config')return {capabilities:[]};
  throw new Error('Unexpected request '+url.pathname);
}
const fulfill=(route,status=200,body=response(new URL(route.request().url())))=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
(async()=>{
  server=http.createServer(async(req,res)=>{
    try{const url=new URL(req.url,'http://localhost'),file=path.resolve(root,'web',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname.slice(1)));
      if(!file.startsWith(path.join(root,'web')+path.sep)){res.writeHead(403).end();return;}
      const data=await fs.readFile(file);res.writeHead(200,{'Content-Type':({'.html':'text/html','.css':'text/css','.js':'text/javascript'})[path.extname(file)]||'application/octet-stream'});res.end(data);
    }catch{res.writeHead(404).end();}
  });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1400,height:1050}});page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url());requests.push({method:request.method(),path:url.pathname,offset:url.searchParams.get('offset')});
    assert.equal(request.method(),'GET','This test must never send mutation requests');
    if(holdPlan&&url.pathname.startsWith('/api/storage/plans/')){pending.push(route);return;}
    await fulfill(route);
  });
  await page.goto('http://127.0.0.1:'+server.address().port+'/#storage',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#storage-reopen-plan');await page.locator('#storage-reopen-plan').click();await page.waitForSelector('#storage-plan-dialog[open]');
  await page.locator('#storage-apply-confirm').check();assert.equal(await page.locator('#storage-plan-apply').isDisabled(),false);
  holdPlan=true;
  await page.evaluate(()=>{document.querySelector('#storage-plan-next').click();document.querySelector('#storage-plan-next').click();});
  await page.waitForFunction(()=>document.querySelector('#storage-plan-dialog').getAttribute('aria-busy')==='true');
  assert.equal(await page.locator('#storage-plan-next').isDisabled(),true);assert.equal(await page.locator('#storage-plan-prev').isDisabled(),true);assert.equal(await page.locator('#storage-plan-apply').isDisabled(),true);
  assert.match(await page.locator('#storage-plan-loading').textContent(),/第 2 页/);
  assert.equal(await page.evaluate(()=>storageState.planOffset),0);
  await page.waitForTimeout(80);assert.equal(pending.length,1);await fulfill(pending.shift(),503,{error:'临时网络故障'});
  await page.waitForFunction(()=>document.querySelector('#storage-plan-error').textContent.includes('临时网络故障'));
  assert.equal(await page.evaluate(()=>storageState.planOffset),0);
  assert.match(await page.locator('.storage-plan-dialog tbody tr').first().textContent(),/entry-000/);
  assert.equal(await page.locator('#storage-plan-next').isDisabled(),false);
  checks.push('Plan pagination blocks duplicate requests and apply while loading; a failed request keeps the original rows/offset and exposes retry feedback.');
  await page.locator('#storage-plan-next').click();await page.waitForTimeout(80);assert.equal(pending.length,1);
  assert.equal(new URL(pending[0].request().url()).searchParams.get('offset'),'50');await fulfill(pending.shift());
  await page.waitForFunction(()=>storageState.planOffset===50);
  assert.match(await page.locator('.storage-plan-dialog tbody tr').first().textContent(),/entry-050/);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'storage-plan-next');
  assert.match(await page.locator('.storage-plan-pagination').textContent(),/第 2 \/ 3 页/);
  assert.equal(await page.locator('#storage-plan-next').evaluate(el=>{const button=el.getBoundingClientRect(),body=el.closest('.storage-plan-body').getBoundingClientRect();return button.top>=body.top&&button.bottom<=body.bottom;}),true);
  assert.equal(await page.locator('#storage-plan-apply').isDisabled(),true);
  checks.push('Retry fetches the failed page rather than skipping ahead; successful pagination commits rows and offset together and keeps keyboard focus on pagination.');
  await page.screenshot({path:path.join(root,'artifacts/operations-ux-storage-page.png')});
  await page.locator('#storage-plan-next').click();await page.waitForTimeout(80);assert.equal(pending.length,1);
  await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#storage-plan-dialog').open);
  await fulfill(pending.shift());await page.waitForTimeout(80);
  assert.equal(await page.evaluate(()=>storageState.planOffset),50);assert.equal(await page.locator('#storage-plan-dialog').evaluate(el=>el.open),false);
  await page.locator('#storage-reopen-plan').click();await page.waitForTimeout(80);assert.equal(pending.length,1);
  await page.locator('[data-view="cluster"]').click();await page.waitForSelector('#cluster-page');await fulfill(pending.shift());await page.waitForTimeout(80);
  assert.equal(await page.locator('#storage-plan-dialog').evaluate(el=>el.open),false);
  checks.push('Closing the dialog invalidates its pending page request; navigating away before the initial load completes never opens a stale storage modal.');
  holdPlan=false;
  await page.waitForSelector('[data-cv-batch="ux-batch"]');await page.locator('#cluster-auto').uncheck();
  const batch=page.locator('[data-cv-batch="ux-batch"]');await batch.focus();await page.evaluate(()=>refreshCluster());
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.cvBatch),'ux-batch');
  const summary=page.locator('[data-cluster-detail="transfer-ux-transfer"] > summary');await summary.focus();await page.keyboard.press('Enter');await page.evaluate(()=>refreshCluster());
  assert.equal(await summary.evaluate(el=>el===document.activeElement),true);assert.equal(await summary.locator('..').evaluate(el=>el.open),true);
  checks.push('Cluster refresh preserves keyboard focus for batch actions and expanded checksum summaries.');
  await batch.focus();await page.keyboard.press('Enter');await page.waitForSelector('#cv-batch-dialog[open]');
  assert.equal(await page.getByRole('dialog',{name:'常驻批次详情'}).count(),1);
  await page.locator('[data-cv-item="ux-item"]').focus();await page.evaluate(()=>refreshCluster());
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.cvItem),'ux-item');
  await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#cv-batch-dialog').open);
  await page.waitForFunction(()=>document.activeElement.dataset.cvBatch==='ux-batch');
  checks.push('Batch inspector has an accessible name; close restores the refreshed trigger, and member focus survives snapshot updates.');
  await page.screenshot({path:path.join(root,'artifacts/operations-ux-cluster-focus.png')});
  assert.deepEqual(errors,[]);
  const report={status:'passed',scope:'Isolated static server and intercepted GET-only API fixtures; localhost:8765 and real storage untouched.',checks,count:checks.length,errors,mutationRequests:requests.filter(x=>x.method!=='GET').length};
  await fs.writeFile(path.join(root,'artifacts/operations-ux-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));});
