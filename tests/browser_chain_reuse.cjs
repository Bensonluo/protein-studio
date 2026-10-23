/* Browser contract test with explicit API fixtures, not AF3/MSA/GPU execution.
   Run: PLAYWRIGHT_MODULE=/absolute/path/to/playwright node tests/browser_chain_reuse.cjs */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const http=require('node:http');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),checks=[],errors=[];
const artifacts=path.join(root,'artifacts');
const reuse={strategy:'af3-chain-reuse-v1',totalChains:3,uniqueChains:2,newChainSearches:1,chainCacheHits:1,duplicateChainCopies:1,wholeTargetCacheHit:false,entries:[{chainIds:['A','C'],sequenceSha256:'a'.repeat(64),cacheKey:'1'.repeat(64),cacheHit:true,residueCount:76},{chainIds:['B'],sequenceSha256:'b'.repeat(64),cacheKey:'2'.repeat(64),cacheHit:false,residueCount:120}]};
const baseItem={campaignId:'qa-chain-cache',sequenceLength:272,attempt:1,stage:'ready',input:{numSamples:5,numRecycles:10,seeds:[1],databaseProfile:{id:'qa-af3',release:'mock-contract',profileFingerprint:'f'.repeat(64)}},events:[{stage:'ready',message:'QA API fixture only',time:'2026-10-23T09:00:00Z'}]};
const items=[
 // Legacy cacheHit can retain an earlier attempt; current chain metadata wins.
 {...baseItem,id:'mixed',targetId:'mixed',name:'固定靶标 + 候选一',preparationReuse:reuse,cacheHit:true},
 {...baseItem,id:'whole',targetId:'whole',name:'相同组合再次准备',cacheHit:true,preparationReuse:{...reuse,newChainSearches:0,chainCacheHits:0,wholeTargetCacheHit:true}},
 {...baseItem,id:'legacy',targetId:'legacy',name:'旧整目标缓存',cacheHit:true},
 {...baseItem,id:'waiting',targetId:'waiting',name:'尚未准备',stage:'waiting_input',cacheHit:false}
];
const campaign={id:'qa-chain-cache',name:'QA · 链准备复用契约',engine:'alphafold3',status:'paused',totalItems:4,counts:{ready:3,waiting_input:1},settings:{seeds:[1],numSamples:5,numRecycles:10},metrics:{preparationReuse:{targets:2,totalChains:6,uniqueChainOccurrences:4,newChainSearches:1,chainCacheHits:1,duplicateChainCopies:2,wholeTargetCacheHits:1}}};
const emptyCampaign={...campaign,id:'qa-empty',name:'QA · 准备前',totalItems:0,counts:{},metrics:{}};
const placeholderJob={id:'initial',name:'QA UI fixture',engine:'reference',status:'queued',progress:0,chains:[]};
let browser,server;
(async()=>{
 fs.mkdirSync(artifacts,{recursive:true});
 server=http.createServer(async(req,res)=>{
  try{const url=new URL(req.url,'http://localhost'),relative=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname.slice(1));
   const file=path.resolve(root,'web',relative);if(!file.startsWith(path.join(root,'web')+path.sep)){res.writeHead(403).end();return;}
   const data=await fsp.readFile(file);res.writeHead(200,{'Content-Type':({'.html':'text/html','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream'});res.end(data);
  }catch{res.writeHead(404).end();}
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const base='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1500,height:1050}});page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',async route=>{
  const url=new URL(route.request().url()),parts=url.pathname.split('/').filter(Boolean);let value={};
  if(url.pathname==='/api/health')value={status:'ok'};
  else if(url.pathname==='/api/system')value={engines:[{id:'reference',available:true},{id:'alphafold3',available:true}]};
  else if(url.pathname==='/api/jobs')value=[placeholderJob];
  else if(url.pathname==='/api/jobs/initial')value=placeholderJob;
  else if(url.pathname==='/api/campaigns')value=[campaign,emptyCampaign];
  else if(parts[1]==='campaigns'&&parts[2]){
   if(parts[3]==='items'&&parts[4])value=items.find(i=>i.id===parts[4]);
   else if(parts[3]==='items')value={items:parts[2]===campaign.id?items.map(({preparationReuse,...item})=>({...item,...(preparationReuse?{preparationReuse:Object.fromEntries(Object.entries(preparationReuse).filter(([key])=>key!=='entries'))}:{})})):[],total:parts[2]===campaign.id?4:0};
   else value=parts[2]===campaign.id?campaign:emptyCampaign;
  }else if(url.pathname==='/api/databases')value=[];
  else if(url.pathname==='/api/pipeline/config')value={prepWorkers:2,gpuWorkers:1,validationWorkers:1,readyCap:64,predictionMode:'workers',capabilities:[{id:'reference',available:true},{id:'alphafold3',available:true}]};
  else if(url.pathname==='/api/cluster')value={enabled:true,planned:{localGpuCards:16},nodes:[],batches:[],transfers:[],events:[]};
  else throw new Error('Unexpected API request: '+url.pathname);
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)});
 });
 await page.goto(base+'/#campaigns',{waitUntil:'domcontentloaded'});await page.waitForSelector('.campaign-reuse-stats');
 for(const [key,n]of [['newChainSearches','1'],['chainCacheHits','1'],['duplicateChainCopies','2'],['wholeTargetCacheHits','1']])assert.equal(await page.locator(`[data-reuse-metric="${key}"] dd`).textContent(),n);
 const scope=await page.locator('.campaign-reuse-scope').textContent();assert.match(scope,/不是全批次唯一序列数/);assert.match(scope,/最近一次已发布准备记录/);assert.match(scope,/不是复制操作次数/);
 checks.push('Aggregate counters retain per-target occurrence units and recent published-record scope');
 const mixedRow=page.locator('tr').filter({has:page.locator('[data-inspect-target="mixed"]')}).first();
 const wholeRow=page.locator('tr').filter({has:page.locator('[data-inspect-target="whole"]')}).first();
 assert.match(await mixedRow.textContent(),/链缓存 1 \/ 2/);assert.match(await wholeRow.textContent(),/整目标缓存命中/);assert.doesNotMatch(await wholeRow.textContent(),/链缓存/);
 checks.push('Lightweight item list distinguishes leaf hits from complete-target hits without requiring entries');
 await page.locator('#campaign-name').fill('保留当前文件与表单');await page.locator('#campaign-file').setInputFiles({name:'candidates.fasta',mimeType:'text/plain',buffer:Buffer.from('>candidate\nACDEFG\n')});
 await page.locator('.campaign-target-link[data-inspect-target="mixed"]').click();await page.waitForSelector('.campaign-reuse-detail');
 assert.equal(await page.locator('.campaign-chain-entry').count(),2);assert.equal(await page.locator('.campaign-chain-entry.reused').count(),1);assert.equal(await page.locator('.campaign-chain-entry.searched').count(),1);
 await page.locator('.campaign-chain-entry summary').first().click();assert.ok(await page.locator('.campaign-chain-entry').first().evaluate(el=>el.open));
 assert.match(await page.locator('.campaign-chain-entry').first().textContent(),/链 A \/ C/);assert.match(await page.locator('.campaign-chain-entry').first().textContent(),/MSA 与模板搜索结果/);
 checks.push('Target detail exposes grouped chain IDs, sequence/cache fingerprints and actual MSA/template preparation scope');
 await page.locator('.campaign-reuse-detail').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(artifacts,'chain-reuse-detail-mock.png')});
 await page.evaluate(()=>refreshCampaignData());assert.equal(await page.locator('.campaign-chain-entry').first().evaluate(el=>el.open),true);
 await page.keyboard.press('Escape');assert.equal(await page.locator('#campaign-name').inputValue(),'保留当前文件与表单');assert.equal(await page.locator('#campaign-file').evaluate(el=>el.files[0].name),'candidates.fasta');
 checks.push('Polling leaves the open chain detail, selected import File and unsent form intact');
 await page.locator('.campaign-target-link[data-inspect-target="whole"]').click();await page.waitForSelector('[data-reuse-whole="true"]');
 assert.match(await page.locator('.campaign-reuse-detail-note').textContent(),/没有执行链缓存查询或新链搜索/);assert.match(await page.locator('.campaign-reuse-detail-note').textContent(),/首次组装时的历史记录/);
 assert.equal(await page.locator('.campaign-chain-entry.historical').count(),2);assert.equal(await page.locator('.campaign-chain-entry.reused,.campaign-chain-entry.searched').count(),0);
 assert.equal(await page.locator('.campaign-reuse-detail-counts dd').nth(2).textContent(),'0');assert.equal(await page.locator('.campaign-reuse-detail-counts dd').nth(3).textContent(),'0');
 assert.match(await page.locator('.campaign-chain-outcome').first().textContent(),/首次组装/);
 checks.push('Whole-target hits expose zero current leaf operations and label retained entry evidence as first-assembly history');
 await page.setViewportSize({width:390,height:844});await page.locator('.campaign-reuse-detail').scrollIntoViewIfNeeded();
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.ok(await page.locator('#campaign-inspector').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
 await page.screenshot({path:path.join(artifacts,'chain-reuse-mobile-mock.png')});checks.push('Chain fingerprints and historical outcomes remain usable in the 390px drawer without horizontal overflow');
 await page.keyboard.press('Escape');await page.setViewportSize({width:1500,height:1050});
 await page.locator('.campaign-target-link[data-inspect-target="legacy"]').click();await page.waitForSelector('.campaign-reuse-detail');assert.match(await page.locator('.campaign-reuse-detail').textContent(),/旧整目标缓存记录不补推链命中次数/);await page.keyboard.press('Escape');
 await page.selectOption('#campaign-select',emptyCampaign.id);await page.waitForSelector('.campaign-reuse-empty');assert.equal(await page.locator('.campaign-reuse-stats').count(),0);assert.match(await page.locator('.campaign-reuse-empty').textContent(),/AF3 准备完成后/);
 checks.push('Legacy whole-cache records and not-yet-prepared AF3 campaigns show missing statistics without fabricated zero-hit evidence');
 await page.selectOption('#campaign-select',campaign.id);await page.waitForSelector('.campaign-reuse-stats');await page.locator('.campaign-reuse-overview').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(artifacts,'chain-reuse-overview-mock.png')});
 assert.deepEqual(errors,[]);checks.push('No browser runtime errors');
 const report={status:'passed',scope:'Browser API fixture contract only; no MSA search, AlphaFold inference, GPU or cloud execution',checks,count:checks.length};
 await fsp.writeFile(path.join(artifacts,'chain-reuse-browser-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));});
