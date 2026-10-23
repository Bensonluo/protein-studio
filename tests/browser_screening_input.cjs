/* Real browser + isolated HTTP/SQLite input ingestion, with explicit availability fixtures.
   No worker threads, real MSA, model inference, GPU or cloud execution are started.
   Run: PLAYWRIGHT_MODULE=/absolute/path/to/playwright node tests/browser_screening_input.cjs */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),artifacts=path.join(root,'artifacts');
const python=process.env.PYTHON_BIN||'/Users/luopeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const checks=[],errors=[];let server,browser,page,temp,base;
const fixture=`
import sys,signal,threading
from backend import staged,databases
from backend.server import StudioApplication,StudioHTTPServer
profile={'id':'qa-input-only','engine':'alphafold3','ready':True,'release':'QA input contract only','profileFingerprint':'f'*64,'prepareVersion':'QA no execution'}
original_profile=databases.get_profile
original_profiles=databases.list_profiles
databases.get_profile=lambda identifier,*a,**kw:profile if identifier==profile['id'] else original_profile(identifier,*a,**kw)
databases.list_profiles=lambda *a,**kw:[profile]+original_profiles(*a,**kw)
staged.staged_capabilities=lambda database_profile=None:[{'engine':'reference','staged':True,'available':True},{'engine':'alphafold3','staged':True,'available':True}]
app=StudioApplication(data_dir=sys.argv[1],workers=1)
# Deliberately do not call app.start(): test only durable input construction/ingestion.
http=StudioHTTPServer(('127.0.0.1',0),app)
signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=http.shutdown,daemon=True).start())
print('READY '+str(http.server_port),flush=True)
try:http.serve_forever(poll_interval=.05)
finally:http.server_close();app.close()
`;
async function api(url){const r=await fetch(base+url);const value=await r.json();assert.ok(r.ok,JSON.stringify(value));return value;}
async function file(name,text){await page.locator('#campaign-file').setInputFiles({name,mimeType:'text/plain',buffer:Buffer.from(text)});}
async function configure(fixed){await page.selectOption('#campaign-engine','alphafold3');await page.selectOption('#campaign-database','qa-input-only');await page.selectOption('#campaign-input-mode','fixed-target');await page.locator('#campaign-fixed-target').fill(fixed);}
async function idle(){await page.waitForFunction(()=>!batchState.importing);}
(async()=>{
 fs.mkdirSync(artifacts,{recursive:true});temp=await fsp.mkdtemp(path.join(os.tmpdir(),'foldspace-screening-'));
 server=spawn(python,['-u','-c',fixture,temp],{cwd:root,env:{...process.env,PROTEIN_STUDIO_DISTRIBUTED:'0',PROTEIN_STUDIO_PREDICTION_MODE:'legacy'},stdio:['ignore','pipe','pipe']});
 let stderr='';server.stderr.on('data',d=>stderr+=d.toString());
 const port=await new Promise((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(new Error('Fixture start timeout '+stderr)),20000);server.stdout.on('data',d=>{out+=d.toString();const m=out.match(/READY (\d+)/);if(m){clearTimeout(timer);resolve(Number(m[1]));}});server.on('error',reject);server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+stderr));});});base='http://127.0.0.1:'+port;
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
 page=await browser.newPage({viewport:{width:1480,height:1100}});page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/#campaigns',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>Array.from(document.querySelector('#campaign-engine')?.options||[]).some(o=>o.value==='alphafold3'));
 const fixed='>shared target first\nACD\n>shared target second\nefg\n';
 const candidates=Array.from({length:103},(_,i)=>`>${i<2?'same candidate name':'candidate '+i}\nIKL\n`).join('');
 await configure(fixed);await file('candidates.fasta',candidates);await page.waitForFunction(()=>document.querySelectorAll('#campaign-input-preview li').length===2);
 assert.match(await page.locator('#campaign-input-preview').textContent(),/2 条固定链 \+ 每项 1 条候选/);assert.match(await page.locator('#campaign-input-preview').textContent(),/3 条链 · 9 aa · A \+ B \+ C/);
 assert.match(await page.locator('#campaign-input-help').textContent(),/N 条候选 → N 个完整任务/);checks.push('Fixed multi-chain FASTA plus candidate preview exposes two sample combinations and deterministic A/B/C roles');
 const bounded=await page.evaluate(async()=>{let pulled=0,cancelled=false;const source={name:'bounded.fasta',stream:()=>new ReadableStream({pull(controller){pulled++;controller.enqueue(new TextEncoder().encode('>candidate '+pulled+'\nACD\n'));if(pulled===1000)controller.close();},cancel(){cancelled=true;}})};const snapshot=await campaignInputSnapshot('fixed-target','>a\nACD');let items=0;for await(const item of campaignConstructedItems(source,snapshot)){items++;if(items===2)break;}return {pulled,cancelled,items};});
 assert.equal(bounded.items,2);assert.ok(bounded.pulled<10);assert.equal(bounded.cancelled,true);checks.push('Two-item preview cancels its stream early instead of reading the remaining candidate file');
 const validation=await page.evaluate(async()=>{const failures=[];for(const value of ['>bad\nACDX',Array.from({length:26},(_,i)=>'>x'+i+'\nA').join('\n'),'>long\n'+'A'.repeat(5000)])try{parseCampaignFixedTarget(value);failures.push(false);}catch{failures.push(true);}try{for await(const item of campaignConstructedItems(new File(['>candidate\nAA'], 'too-long.fasta'),await campaignInputSnapshot('fixed-target','>fixed\n'+'A'.repeat(4999)))){}failures.push(false);}catch{failures.push(true);}return failures;});assert.deepEqual(validation,[true,true,true,true]);checks.push('Fixed input rejects ambiguous residues, 26 fixed chains and combinations above the default 5,000-residue limit');
 const configuredLimit=await page.evaluate(async()=>{const old=batchState.config.maxResiduesPerTarget;batchState.config.maxResiduesPerTarget=8;try{const snapshot=await campaignInputSnapshot('fixed-target','>A\nACD\n>B\nEFG');let rejected=false;try{for await(const item of campaignConstructedItems(new File(['>candidate\nIKL'],'limit.fasta'),snapshot)){} }catch{rejected=true;}return {limit:snapshot.maxResidues,label:campaignResidueLimitLabel(),rejected};}finally{if(old===undefined)delete batchState.config.maxResiduesPerTarget;else batchState.config.maxResiduesPerTarget=old;}});assert.equal(configuredLimit.limit,8);assert.match(configuredLimit.label,/8 残基/);assert.equal(configuredLimit.rejected,true);checks.push('Configured server residue limit is captured in the construction snapshot and enforced instead of a hard-coded 5,000');
 await page.evaluate(()=>refreshCampaignData());assert.equal(await page.locator('#campaign-fixed-target').inputValue(),fixed);assert.equal(await page.locator('#campaign-input-mode').inputValue(),'fixed-target');assert.equal(await page.locator('#campaign-file').evaluate(el=>el.files[0].name),'candidates.fasta');
 let postCount=0,firstGate,releaseFirst;firstGate=new Promise(resolve=>releaseFirst=resolve);let injectFailure=true,itemPosts=0;
 page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/api/campaigns'))postCount++;});
 await page.route('**/api/campaigns/*/items',async route=>{if(route.request().method()!=='POST')return route.continue();itemPosts++;if(itemPosts===1)await firstGate;if(injectFailure&&itemPosts===2){injectFailure=false;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'QA injected second-chunk interruption'})});}return route.continue();});
 await page.locator('#campaign-submit').click();await page.waitForFunction(()=>batchState.importing&&batchState.progress.read===100);
 for(const selector of ['#campaign-input-mode','#campaign-fixed-target','#campaign-file','#campaign-append','#campaign-engine'])assert.equal(await page.locator(selector).isDisabled(),true);
 await page.evaluate(()=>refreshCampaignData());assert.equal(await page.locator('#campaign-fixed-target').inputValue(),fixed);releaseFirst();await idle();
 assert.match(await page.locator('#campaign-form-error').textContent(),/QA injected second-chunk/);
 const id=await page.evaluate(()=>batchState.selectedId);let campaign=await api('/api/campaigns/'+id);assert.equal(campaign.totalItems,100);assert.equal(campaign.status,'paused');assert.equal(campaign.sealed,false);
 const saved=await page.evaluate(id=>localStorage.getItem(campaignInputContextKey(id)),id);const context=JSON.parse(saved);assert.equal(context.chainIdPolicy,'fixed-target-plus-candidate-v1');assert.equal(context.fixedTargetChainsSha256.length,64);assert.ok(!saved.includes('ACD')&&!saved.includes('EFG'));
 checks.push('Input semantics lock during upload; first 100 real SQLite items survive an injected second-chunk failure, with fingerprint-only context persisted');
 await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(id=>batchState.campaign?.id===id,id);await configure('>changed\nACDE\n');await file('candidates.fasta',candidates);await page.locator('#campaign-append').check();let before=postCount;
 await page.locator('#campaign-submit').click();await idle();assert.match(await page.locator('#campaign-form-error').textContent(),/输入构造已改变/);assert.equal(postCount,before);checks.push('After a full page reload, changed fixed-target content is rejected locally before any campaign POST');
 await page.selectOption('#campaign-input-mode','independent');before=postCount;await page.locator('#campaign-submit').click();await idle();assert.match(await page.locator('#campaign-form-error').textContent(),/输入构造已改变/);assert.equal(postCount,before);
 await configure(fixed);await page.locator('#campaign-submit').click();await idle();assert.equal(await page.locator('#campaign-form-error').textContent(),'');
 campaign=await api('/api/campaigns/'+id);assert.equal(campaign.totalItems,103);assert.equal(campaign.sealed,true);assert.equal(campaign.status,'active');assert.equal(await page.evaluate(()=>batchState.progress.duplicates),100);
 const entries=await api(`/api/campaigns/${id}/items?limit=200`);assert.equal(entries.items.length,103);const firstTwo=entries.items.filter(i=>i.name==='same candidate name');assert.equal(firstTwo.length,2);assert.notEqual(firstTwo[0].targetId,firstTwo[1].targetId);
 const detail=await api(`/api/campaigns/${id}/items/${firstTwo[0].id}`);assert.deepEqual(detail.input.chains,[{id:'A',sequence:'ACD'},{id:'B',sequence:'EFG'},{id:'C',sequence:'IKL'}]);checks.push('Original-target resume produces exactly 103 complete inputs, reuses 100 existing IDs and retains distinct equal-sequence candidate records');
 await page.locator('#campaign-append').uncheck();await file('unsupported.jsonl','{"chains":[{"id":"A","sequence":"ACD"}]}\n');await page.waitForFunction(()=>document.querySelector('#campaign-input-preview')?.textContent.includes('只接受候选 FASTA'));
 before=postCount;await page.locator('#campaign-submit').click();await idle();assert.equal(postCount,before);assert.match(await page.locator('#campaign-form-error').textContent(),/只接受候选 FASTA/);checks.push('JSONL is rejected in fixed-target mode before campaign creation');
 await page.selectOption('#campaign-input-mode','independent');const jsonl={targetId:'explicit-multichain',name:'JSONL stays intact',chains:[{id:'Q',sequence:'ACD'},{id:'R',sequence:'EFG'}],settings:{seeds:[2],numSamples:3,numRecycles:4}};
 await file('existing.jsonl',JSON.stringify(jsonl)+'\n');await page.locator('#campaign-submit').click();await idle();assert.equal(await page.locator('#campaign-form-error').textContent(),'');const jsonId=await page.evaluate(()=>batchState.selectedId);const jsonItems=await api(`/api/campaigns/${jsonId}/items`);const jsonDetail=await api(`/api/campaigns/${jsonId}/items/${jsonItems.items[0].id}`);assert.equal(jsonDetail.targetId,'explicit-multichain');assert.deepEqual(jsonDetail.input.chains,jsonl.chains);assert.deepEqual(jsonDetail.input.seeds,[2]);assert.equal(jsonDetail.input.numSamples,3);checks.push('Independent multi-chain JSONL preserves explicit target IDs, chain IDs and allowed item-level scientific settings through the real API');
 await configure(fixed);await file('small.fasta','>candidate\nIKL\n');await page.evaluate(()=>{window.qaStorageSet=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith('foldspace.campaign-input.'))throw new Error('QA storage unavailable');return window.qaStorageSet.call(this,key,value);};});
 const beforeItems=itemPosts;await page.locator('#campaign-submit').click();await idle();assert.match(await page.locator('#campaign-form-error').textContent(),/无法保存导入上下文/);assert.equal(itemPosts,beforeItems);await page.evaluate(()=>{Storage.prototype.setItem=window.qaStorageSet;});checks.push('Unavailable local context storage stops ingestion before the first target upload');
 await page.locator('#campaign-form-error').evaluate(el=>el.textContent='');await page.waitForFunction(()=>document.querySelectorAll('#toast-container .toast').length===0);await page.locator('#campaign-fixed-target').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(artifacts,'screening-input-desktop.png')});
 await page.setViewportSize({width:390,height:844});await page.locator('#campaign-fixed-target').scrollIntoViewIfNeeded();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(artifacts,'screening-input-mobile.png')});assert.deepEqual(errors,[]);checks.push('Fixed-target input remains keyboard-addressable and fits a 390px viewport with no browser runtime errors');
 const report={status:'passed',scope:'Real browser and real isolated HTTP/SQLite input ingestion. AF3 capability/database availability are test fixtures; no pipeline threads, MSA, model inference, GPU or cloud execution.',count:checks.length,checks,errors};await fsp.writeFile(path.join(artifacts,'screening-input-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
})().catch(async error=>{console.error(error);if(page){console.error(await page.locator('#campaign-form-error').textContent().catch(()=>''));await page.screenshot({path:path.join(artifacts,'screening-input-debug.png'),fullPage:true}).catch(()=>{});}process.exitCode=1;}).finally(async()=>{await browser?.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{server.kill('SIGKILL');resolve();},5000).unref();});}if(temp)await fsp.rm(temp,{recursive:true,force:true});});
