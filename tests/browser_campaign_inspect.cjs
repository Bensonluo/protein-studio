/* Real browser + isolated stdlib server. Only the named preparation failure is injected.
   Run: PYTHON_BIN=/path/to/python3.12 PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser_campaign_inspect.cjs
   No existing application data, external inference, cloud nodes or GPU are used. */
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),output=path.join(root,'artifacts');
const python=process.env.PYTHON_BIN||'/Users/luopeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const checks=[],errors=[];let browser,server,temp,base,page;
const fixture=`
import os,sys,signal,threading
from backend import staged
from backend.server import StudioApplication,StudioHTTPServer
original=staged.prepare_cooperative
def prepare(job,*args,**kwargs):
    if job.get('name')=='QA injected preparation failure':
        raise RuntimeError('QA fixture: sequence index is unavailable; restore the configured database asset.')
    return original(job,*args,**kwargs)
staged.prepare_cooperative=prepare
app=StudioApplication(data_dir=sys.argv[1],workers=1)
http=StudioHTTPServer(('127.0.0.1',0),app)
app.start()
signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=http.shutdown,daemon=True).start())
print('READY '+str(http.server_port),flush=True)
try:http.serve_forever(poll_interval=.05)
finally:http.server_close();app.close()
`;
async function api(url,options={}){
  const response=await fetch(base+url,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  const value=await response.json();assert.ok(response.ok,JSON.stringify(value));return value;
}
async function post(url,value={}){return api(url,{method:'POST',body:JSON.stringify(value)});}
async function waitUntil(fn){for(let i=0;i<150;i++){const value=await fn();if(value)return value;await new Promise(r=>setTimeout(r,100));}throw new Error('Condition did not become true');}
async function downloadJsonl(page,selector){
  const pending=page.waitForEvent('download');await page.locator(selector).click();const download=await pending;
  assert.equal(await download.failure(),null);const file=await download.path();return (await fsp.readFile(file,'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
}
(async()=>{
  fs.mkdirSync(output,{recursive:true});temp=await fsp.mkdtemp(path.join(os.tmpdir(),'foldspace-inspect-'));
  server=spawn(python,['-u','-c',fixture,temp],{cwd:root,env:{...process.env,PROTEIN_STUDIO_DISTRIBUTED:'0',PROTEIN_STUDIO_PREDICTION_MODE:'legacy',PROTEIN_STUDIO_CPU_PREP_WORKERS:'1',PROTEIN_STUDIO_GPU_WORKERS:'1',PROTEIN_STUDIO_CPU_VALIDATE_WORKERS:'1'},stdio:['ignore','pipe','pipe']});
  let logs='';server.stderr.on('data',d=>{logs+=d.toString();});
  const port=await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(new Error('Fixture server timeout: '+logs)),20000);let out='';server.stdout.on('data',d=>{out+=d;const m=out.match(/READY (\d+)/);if(m){clearTimeout(timeout);resolve(Number(m[1]));}});server.once('error',e=>{clearTimeout(timeout);reject(e);});server.once('exit',code=>{clearTimeout(timeout);reject(new Error('Fixture server exited '+code+': '+logs));});});
  base='http://127.0.0.1:'+port;
  const queued=await post('/api/campaigns',{name:'QA · queued target inspector',engine:'reference',status:'paused',settings:{example:'crambin'}});
  const queuedItems=await post(`/api/campaigns/${queued.id}/items`,{items:Array.from({length:28},(_,i)=>({targetId:'queued-'+i,name:i===0?'QA <img src=x onerror=alert(1)>':`QA queued ${i}`}))});
  const running=await post('/api/campaigns',{name:'QA · completed and failed inspector',engine:'reference',status:'paused',settings:{example:'crambin'}});
  const items=await post(`/api/campaigns/${running.id}/items`,{items:[{targetId:'success-1',name:'QA delivered one'},{targetId:'failure',name:'QA injected preparation failure'},{targetId:'success-2',name:'QA delivered cached'}]});
  await post(`/api/campaigns/${running.id}/seal`);await post(`/api/campaigns/${running.id}/resume`);
  const finished=await waitUntil(async()=>{const c=await api(`/api/campaigns/${running.id}`);return c.counts.completed===2&&c.counts.failed===1?c:null;});
  assert.ok(finished.metrics.cacheHits>=1);checks.push('Real reference pipeline produces two published structures, one cache hit and a deliberately injected preparation error');
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  page=await browser.newPage({viewport:{width:1500,height:1050},acceptDownloads:true});page.on('pageerror',error=>{errors.push(error.message);console.error('PAGE ERROR',error.message);});
  await page.goto(base+'/#campaigns',{waitUntil:'domcontentloaded'});await page.waitForSelector('#campaign-select');
  await page.waitForFunction(()=>state.selectedId&&state.job);assert.equal(new URL(page.url()).hash,'#campaigns');checks.push('An empty workspace opened directly at #campaigns stays on the campaign page after reference initialization');
  await page.waitForFunction(id=>Array.from(document.querySelector('#campaign-select')?.options||[]).some(o=>o.value===id),queued.id);
  await page.selectOption('#campaign-select',queued.id);await page.waitForFunction(()=>document.querySelector('.batch-stats strong')?.textContent==='28');
  await page.locator('#campaign-name').fill('Unsubmitted form must survive refresh');
  await page.locator('#campaign-file').setInputFiles({name:'retained.fasta',mimeType:'text/plain',buffer:Buffer.from('>retained\nACDEFG\n')});
  await page.locator('.campaign-target-link').first().click();await page.waitForSelector('#campaign-inspector[open] .campaign-inspect-rail');
  assert.ok((await page.locator('#campaign-inspect-title').textContent()).includes('<img'));
  assert.equal(await page.locator('#campaign-inspect-title img').count(),0);
  assert.ok((await page.locator('.campaign-inspect-body').textContent()).includes('尚未分配执行节点'));
  assert.equal(await page.locator('#campaign-inspect-structure').count(),0);checks.push('Queued targets open accessible details; hostile-looking names remain text, with no fabricated result or worker');
  await page.locator('.campaign-inspect-body').evaluate(el=>{el.scrollTop=300;});const scroll=await page.locator('.campaign-inspect-body').evaluate(el=>el.scrollTop);
  await page.waitForTimeout(2800);assert.equal(await page.locator('#campaign-inspector[open]').count(),1);
  assert.equal(await page.locator('.campaign-inspect-body').evaluate(el=>el.scrollTop),scroll);
  await page.keyboard.press('Escape');await page.waitForSelector('#campaign-inspector[open]',{state:'hidden'});
  assert.equal(await page.locator('#campaign-name').inputValue(),'Unsubmitted form must survive refresh');
  assert.equal(await page.locator('#campaign-file').evaluate(el=>el.files[0].name),'retained.fasta');
  assert.ok(await page.evaluate(()=>document.activeElement.closest('#campaign-monitor')!==null));checks.push('Background polling preserves dialog scroll, unsubmitted form/File and keyboard focus on Escape');
  await page.locator('#campaign-next').click();await page.waitForFunction(()=>document.querySelectorAll('.batch-items tbody tr').length===3);
  await page.selectOption('#campaign-stage-filter','completed');await page.waitForFunction(()=>document.querySelector('.campaign-table-empty')?.textContent.includes('没有目标'));
  await page.locator('.campaign-filter-chips [data-stage-filter=""]').click();await page.waitForFunction(()=>document.querySelectorAll('.batch-items tbody tr').length===25);checks.push('Pagination and quick stage filters work without loading the entire manifest');
  await page.locator('.campaign-filter-chips [data-stage-filter=""]').focus();await page.evaluate(()=>refreshCampaignData());assert.ok(await page.evaluate(()=>document.activeElement.closest('.campaign-filter-chips')!==null));checks.push('Ledger refresh preserves keyboard focus on quick stage filters');
  const all=await downloadJsonl(page,'[data-campaign-export="all"]');assert.equal(all.length,28);assert.ok(all.every(x=>!('result' in x)&&!('input' in x)&&!('resultPath' in x)));checks.push('Native all-target JSONL download contains metadata, not sequences, PAE or private result paths');
  await page.selectOption('#campaign-select',running.id);await page.waitForFunction(()=>document.querySelector('.campaign-overview')?.textContent.includes('66.7%'));
  await page.locator('.campaign-filter-chips [data-stage-filter="failed"]').click();await page.waitForFunction(()=>document.querySelectorAll('.batch-items tbody tr').length===1);
  await page.locator('.campaign-target-link').click();await page.waitForSelector('.campaign-inspect-error');
  assert.ok((await page.locator('.campaign-inspect-error').textContent()).includes('sequence index is unavailable'));
  assert.equal(await page.locator('.campaign-inspect-rail .failed').count(),1);assert.ok(await page.locator('.campaign-inspect-events li').count()>=3);
  await page.locator('#campaign-inspect-refresh').click();await page.waitForSelector('.campaign-inspect-error');
  await page.screenshot({path:path.join(output,'campaign-inspector-failure.png'),fullPage:false});
  checks.push('Failed target shows exact injected error, failed phase, recorded event trail and manually refreshed details');
  await page.keyboard.press('Escape');const failures=await downloadJsonl(page,'[data-campaign-export="failed"]');assert.equal(failures.length,1);assert.equal(failures[0].failedStage,'preparing');
  const complete=await downloadJsonl(page,'[data-campaign-export="completed"]');assert.equal(complete.length,2);assert.equal((await fetch(base+complete[0].structureUrl)).status,200);checks.push('Failed/completed exports preserve failure phase and working structure URLs');
  await page.locator('[data-view="cluster"]').click();await page.waitForURL('**/#cluster');
  await page.evaluate(([campaignId,itemId])=>window.inspectCampaignTarget(campaignId,itemId),[running.id,items.items[2].id]);
  await page.waitForSelector('#campaign-inspect-structure');
  assert.ok((await page.locator('.campaign-inspect-body').textContent()).includes('命中已完成准备缓存'));
  await page.locator('.campaign-inspect-identifiers summary').click();assert.ok((await page.locator('.campaign-inspect-identifiers').textContent()).includes('reference'));
  await page.locator('.campaign-inspect-body').evaluate(el=>{el.scrollTop=0;});
  await page.screenshot({path:path.join(output,'campaign-inspector-completed.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.ok(await page.locator('#campaign-inspector').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
  await page.locator('.campaign-inspect-body').evaluate(el=>{el.scrollTop=0;});
  await page.screenshot({path:path.join(output,'campaign-inspector-mobile.png'),fullPage:false});checks.push('Public cross-page inspector entry works; completed evidence and 390px drawer remain readable without horizontal overflow');
  await page.locator('#campaign-inspect-structure').click();await page.waitForSelector('#viewer-loading.hidden',{state:'attached',timeout:20000});assert.ok(await page.locator('#molecule-viewer canvas').count());assert.equal(await page.locator('#campaign-inspector[open]').count(),0);checks.push('Completed target opens the actual 3D structure and closes the inspector');
  assert.deepEqual(errors,[]);checks.push('No browser JavaScript errors');
  const report={scope:'Real isolated local HTTP service and reference-fixture pipeline; one named preparation failure is intentionally injected. No GPU or cloud execution.',date:new Date().toISOString(),checks,errors,screenshots:['campaign-inspector-failure.png','campaign-inspector-completed.png','campaign-inspector-mobile.png']};
  fs.writeFileSync(path.join(output,'campaign-inspect-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
})().catch(async error=>{console.error(error);if(page){console.error(await page.evaluate(()=>({text:document.querySelector('#campaign-monitor')?.innerText,state:typeof batchState==='object'?batchState:null})));await page.screenshot({path:path.join(output,'campaign-inspector-debug.png'),fullPage:true}).catch(()=>{});}process.exitCode=1}).finally(async()=>{
  await browser?.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});}
  if(temp)await fsp.rm(temp,{recursive:true,force:true});
});
