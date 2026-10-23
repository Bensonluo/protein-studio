/* Real browser -> real HTTP -> real temporary reference cache lifecycle.
   No API mocks/interception, production data paths, scientific inference or GPU work.
   Run: PLAYWRIGHT_MODULE=/absolute/path/to/playwright node tests/browser_storage_http.cjs */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),artifacts=path.join(root,'artifacts');
const python=process.env.PYTHON_BIN||'/Users/luopeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const checks=[],errors=[],requests=[],receipts=[];let processHandle,browser,page,temp,base,orphan,cacheRoot,report;
const harness=`
import json,sys,signal,threading
from pathlib import Path
from backend import engine,staged
from backend.server import StudioApplication,StudioHTTPServer
root=Path(sys.argv[1]).resolve()
app=StudioApplication(data_dir=root,workers=1)
job=engine.load_example('crambin')
job['cacheRoot']=str(app.pipeline.cache_root)
prepared=staged.prepare_stage(job,root/'reference-orphan-preparation',lambda *a:None,lambda:False)
# Only StorageService starts maintenance threads on explicit POST. No prediction
# or campaign scheduler threads are started for this test.
http=StudioHTTPServer(('127.0.0.1',0),app)
signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=http.shutdown,daemon=True).start())
print('READY '+json.dumps({'port':http.server_port,'orphan':prepared['cacheDirectory'],'cacheRoot':str(app.pipeline.cache_root),'cacheKey':prepared['cacheKey'],'engine':'reference'}),flush=True)
try:http.serve_forever(poll_interval=.05)
finally:http.server_close();app.close()
`;
async function api(endpoint){const response=await fetch(base+endpoint);const value=await response.json();assert.ok(response.ok,JSON.stringify(value));return value;}
async function readTree(directory){
 const result={};
 async function visit(parent){for(const entry of await fsp.readdir(parent,{withFileTypes:true})){assert.equal(entry.isSymbolicLink(),false);const file=path.join(parent,entry.name);if(entry.isDirectory())await visit(file);else if(entry.isFile())result[path.relative(directory,file).split(path.sep).join('/')]=await fsp.readFile(file);}}
 await visit(directory);return Object.fromEntries(Object.entries(result).sort(([a],[b])=>a.localeCompare(b)));
}
function insideTemp(file){const relative=path.relative(temp,file);assert.ok(relative&&!relative.startsWith('..')&&!path.isAbsolute(relative),'Filesystem assertion must stay inside the owned temp directory');return file;}
async function waitJob(previousId,action){
 await page.waitForFunction(([previous,expected])=>{const j=storageState.snapshot?.job;return j&&j.id!==previous&&j.action===expected&&j.status!=='running';},[previousId,action],{timeout:30000});
 const snapshot=await api('/api/storage'),job=snapshot.job;assert.equal(job.action,action);assert.equal(job.status,'completed',JSON.stringify(job));return job.result;
}
async function clickJob(selector,action){const previous=(await api('/api/storage')).job?.id;await page.locator(selector).click();return waitJob(previous,action);}
async function scan(){const value=await clickJob('#storage-scan-submit','scan');assert.equal(value.status,'completed');assert.equal(value.truncated,false);assert.equal(value.failClosed,false);await page.waitForFunction(id=>storageState.snapshot?.state?.latestScan?.id===id&&!storageState.loading,value.id);return value;}
async function plan(){const value=await clickJob('#storage-plan-submit','plan');await page.waitForSelector('#storage-plan-dialog[open]');assert.equal(value.count,1);assert.equal(value.digest.length,64);assert.equal(await page.locator('#storage-plan-apply').isDisabled(),true);return value;}
async function apply(purge=false){
 const value=await page.evaluate(()=>({id:storageState.plan.id,digest:storageState.plan.digest,action:storageState.plan.action}));
 if(purge)await page.locator('#storage-purge-confirm').fill('永久删除 1');else await page.locator('#storage-apply-confirm').check();
 const receipt=await clickJob('#storage-plan-apply','apply');assert.equal(receipt.planId,value.id);assert.equal(receipt.action,value.action);assert.equal(receipt.status,'completed');const label={quarantine:'移入隔离区',restore:'恢复缓存',purge:'永久删除'}[receipt.action];assert.match(await page.locator('.storage-operation h3').textContent(),new RegExp(label));if(receipt.action==='restore'){const facts=await page.locator('.storage-operation-facts').textContent();assert.match(facts,/已恢复文件逻辑长度/);assert.doesNotMatch(facts,/已隔离|已永久删除|已删除文件/);}receipts.push(receipt);return receipt;
}
(async()=>{
 fs.mkdirSync(artifacts,{recursive:true});temp=await fsp.mkdtemp(path.join(os.tmpdir(),'foldspace-storage-http-reference-'));temp=await fsp.realpath(temp);
 const environment=Object.fromEntries(Object.entries(process.env).filter(([key])=>!key.startsWith('PROTEIN_STUDIO_')));
 Object.assign(environment,{PROTEIN_STUDIO_CACHE_PURGE_ENABLED:'1',PROTEIN_STUDIO_DISTRIBUTED:'0',PROTEIN_STUDIO_PREDICTION_MODE:'legacy'});
 processHandle=spawn(python,['-u','-c',harness,temp],{cwd:root,env:environment,stdio:['ignore','pipe','pipe']});let stderr='';processHandle.stderr.on('data',d=>stderr+=d.toString());
 const metadata=await new Promise((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(new Error('Temporary reference harness timed out: '+stderr)),25000);processHandle.stdout.on('data',d=>{out+=d.toString();const line=out.split('\n').find(s=>s.startsWith('READY '));if(line){clearTimeout(timer);resolve(JSON.parse(line.slice(6)));}});processHandle.once('error',e=>{clearTimeout(timer);reject(e);});processHandle.once('exit',code=>{clearTimeout(timer);reject(new Error('Temporary harness exited '+code+': '+stderr));});});
 assert.equal(metadata.engine,'reference');base='http://127.0.0.1:'+metadata.port;orphan=insideTemp(metadata.orphan);cacheRoot=insideTemp(metadata.cacheRoot);
 const before=await readTree(orphan),logicalBytes=Object.values(before).reduce((n,b)=>n+b.length,0);assert.ok(Object.keys(before).length>=3);assert.ok(before['COMPLETE.json']);assert.ok(logicalBytes>0);
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});page=await browser.newPage({viewport:{width:1500,height:1100}});page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{const url=new URL(request.url());if(url.pathname.startsWith('/api/storage')){assert.equal(url.origin,base);requests.push({method:request.method(),path:url.pathname,body:request.method()==='POST'?JSON.parse(request.postData()||'{}'):undefined});}});
 await page.goto(base+'/#storage',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>storageState.snapshot!==null&&!storageState.loading);assert.equal((await api('/api/storage')).state.latestScan,null);assert.equal(requests.filter(r=>r.method==='POST').length,0);assert.ok(fs.existsSync(orphan));checks.push('Real reference preparation creates an orphan only under the owned temporary data directory; opening the browser page performs no scan');
 await page.locator('#storage-max-entries').fill('10000');await page.locator('#storage-max-seconds').fill('10');await page.locator('#storage-min-age').fill('0');await page.locator('#storage-kind-leaf').uncheck();await page.locator('#storage-kind-partial').uncheck();
 let scanned=await scan();let catalog=await api(`/api/storage/entries?scanId=${scanned.id}&kind=whole&limit=50`);assert.equal(catalog.total,1);assert.equal(catalog.items[0].protected,false);assert.equal(catalog.items[0].logicalBytes,logicalBytes);checks.push('Browser-triggered real scan catalogs exactly one unreferenced whole-target cache with byte counts matching its files');
 const quarantinePlan=await plan();assert.equal(quarantinePlan.action,'quarantine');assert.equal(quarantinePlan.reclaimableBytes,0);assert.equal(quarantinePlan.logicalBytes,logicalBytes);assert.deepEqual(await readTree(orphan),before);assert.equal(await page.locator('.storage-plan-stats strong').nth(2).textContent(),'0 B');checks.push('The concrete quarantine preview contains the original cache and changes no files before exact-plan confirmation');
 let receipt=await apply();assert.equal(receipt.movedEntries,1);assert.equal(receipt.logicalBytes,logicalBytes);assert.equal(fs.existsSync(orphan),false);assert.match(await page.locator('.storage-operation').textContent(),/未释放/);await page.screenshot({path:path.join(artifacts,'storage-http-reference-quarantine.png')});
 scanned=await scan();catalog=await api(`/api/storage/entries?scanId=${scanned.id}&kind=quarantine&limit=50`);assert.equal(catalog.total,1);let payload=insideTemp(path.resolve(cacheRoot,catalog.items[0].relativePath));assert.deepEqual(await readTree(payload),before);checks.push('Confirmed quarantine moves the real cache to the temporary quarantine directory with byte-for-byte content preserved and no claimed physical space release');
 const restorePrevious=(await api('/api/storage')).job.id;await page.locator(`[data-storage-restore="${catalog.items[0].quarantineId}"]`).click();const restorePlan=await waitJob(restorePrevious,'plan');await page.waitForSelector('#storage-plan-dialog[open]');assert.equal(restorePlan.action,'restore');assert.equal(restorePlan.count,1);assert.equal(fs.existsSync(orphan),false);assert.ok(fs.existsSync(payload));receipt=await apply();assert.equal(receipt.restoredEntries,1);assert.deepEqual(await readTree(orphan),before);assert.equal(fs.existsSync(payload),false);await page.screenshot({path:path.join(artifacts,'storage-http-reference-restored.png')});
 scanned=await scan();catalog=await api(`/api/storage/entries?scanId=${scanned.id}&kind=whole&limit=50`);assert.equal(catalog.total,1);assert.equal((await api(`/api/storage/entries?scanId=${scanned.id}&kind=quarantine&limit=50`)).total,0);checks.push('Browser restore preview and confirmation return every original file byte intact; a new real scan sees the restored whole cache and an empty quarantine');
 await plan();await apply();scanned=await scan();catalog=await api(`/api/storage/entries?scanId=${scanned.id}&kind=quarantine&limit=50`);assert.equal(catalog.total,1);payload=insideTemp(path.resolve(cacheRoot,catalog.items[0].relativePath));assert.ok(fs.existsSync(payload));
 assert.equal((await api('/api/storage')).capabilities.purge,true);await page.selectOption('#storage-plan-action','purge');await page.locator('#storage-retention').fill('0');const purgePlan=await plan();assert.equal(purgePlan.action,'purge');assert.equal(purgePlan.logicalBytes,logicalBytes);assert.ok(fs.existsSync(payload));await page.locator('#storage-purge-confirm').fill('永久删除');assert.equal(await page.locator('#storage-plan-apply').isDisabled(),true);receipt=await apply(true);assert.equal(receipt.purgedEntries,1);assert.equal(receipt.logicalBytes,logicalBytes);assert.equal(receipt.spaceReclaimedMeasured,false);assert.equal(receipt.reclaimedBytes,null);assert.equal(fs.existsSync(payload),false);assert.equal(fs.existsSync(orphan),false);assert.match(await page.locator('.storage-operation').textContent(),/已删除文件逻辑长度/);await page.screenshot({path:path.join(artifacts,'storage-http-reference-purged.png')});checks.push('Explicitly enabled temporary-server purge requires the exact typed confirmation, deletes only the selected temporary payload and returns logical-byte evidence without physical-free claims');
 scanned=await scan();assert.equal((await api(`/api/storage/entries?scanId=${scanned.id}&kind=whole&limit=50`)).total,0);assert.equal((await api(`/api/storage/entries?scanId=${scanned.id}&kind=quarantine&limit=50`)).total,0);assert.deepEqual(errors,[]);assert.ok(requests.filter(r=>r.method==='POST').every(r=>['/api/storage/scan','/api/storage/plan','/api/storage/apply'].includes(r.path)));checks.push('Final real scan confirms no remaining original or quarantined cache; all mutations originated through the browser on its isolated loopback server');
 report={status:'passed',scope:'Real browser -> real isolated HTTP -> real temporary reference cache. No API mocks/interception, production workspace data, MSA, new scientific prediction, GPU or cloud use.',count:checks.length,checks,errors,engine:'reference',sourceExample:'crambin',cacheKey:metadata.cacheKey,fileCount:Object.keys(before).length,logicalBytes,fileSha256:Object.fromEntries(Object.entries(before).map(([name,bytes])=>[name,crypto.createHash('sha256').update(bytes).digest('hex')])),receipts,postRequests:requests.filter(r=>r.method==='POST'),temporaryDataDirectory:temp};
})().catch(async error=>{console.error(error);if(page){console.error(await page.locator('#storage-live').innerText().catch(()=>''));await page.screenshot({path:path.join(artifacts,'storage-http-reference-debug.png'),fullPage:true}).catch(()=>{});}process.exitCode=1;}).finally(async()=>{
 await browser?.close();if(processHandle&&processHandle.exitCode===null){processHandle.kill('SIGTERM');await new Promise(resolve=>{processHandle.once('exit',resolve);setTimeout(()=>{processHandle.kill('SIGKILL');resolve();},6000).unref();});}
 if(temp)await fsp.rm(temp,{recursive:true,force:true});
 if(report){report.temporaryDataRemoved=!fs.existsSync(temp);await fsp.writeFile(path.join(artifacts,'storage-http-reference-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify({status:report.status,scope:report.scope,count:report.count,checks,temporaryDataRemoved:report.temporaryDataRemoved,fileCount:report.fileCount,logicalBytes:report.logicalBytes,actualReceipts:report.receipts.length},null,2));}
});
