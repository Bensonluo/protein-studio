/* Read-only live application verification; no model/GPU execution. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const path=require('node:path');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{let browser;const errors=[],checks=[];const base=process.env.STUDIO_URL||'http://127.0.0.1:8765',out=path.resolve(__dirname,'../artifacts');try{
const health=await (await fetch(base+'/api/health')).json();assert.equal(health.status,'ok');
const campaigns=await (await fetch(base+'/api/campaigns')).json();assert.ok(campaigns.every(c=>c.metrics.preparationReuse&&c.metrics.preparationReuse.targets===0));checks.push('Existing reference campaigns migrate and remain without fabricated AF3 chain statistics');
const config=await(await fetch(base+'/api/pipeline/config')).json();assert.ok(config.maxResiduesPerTarget>0);assert.ok(config.preparationResources);checks.push('Live configuration exposes the input limit and preparation admission snapshot');
const cluster=await(await fetch(base+'/api/cluster')).json();assert.ok(cluster.nodes.length>=2);checks.push('Two existing loopback reference workers reconnect after upgrade');
browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage({viewport:{width:1500,height:1050}});page.on('pageerror',e=>errors.push(e.message));
await page.goto(base+'/?ui=rich#pipeline',{waitUntil:'domcontentloaded'});await page.waitForSelector('.chain-architecture svg');await page.waitForTimeout(500);await page.evaluate(()=>scrollTo(0,0));
assert.ok((await page.locator('.chain-architecture .panel-heading').boundingBox()).height<80);assert.match(await page.locator('.architecture-flow').textContent(),/两层准备缓存/);assert.match(await page.locator('.chain-architecture').textContent(),/机制示意/);assert.match(await page.locator('.chain-architecture').textContent(),/N\+1/);checks.push('Live architecture explains exact-chain reuse, separate complex inference and the two cache layers');
await page.screenshot({path:out+'/pipeline-chain-architecture-live.png',fullPage:true});
await page.locator('.chain-architecture').screenshot({path:out+'/chain-architecture-live.png'});
await page.setViewportSize({width:390,height:844});assert.ok((await page.locator('.chain-architecture .panel-heading').boundingBox()).height<80);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.locator('.chain-architecture').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/pipeline-chain-mobile-live.png'});checks.push('390px architecture uses internal diagram scrolling without page overflow');
await page.setViewportSize({width:1500,height:1050});await page.locator('[data-view="campaigns"]').click();await page.waitForSelector('.preparation-budget');
await page.selectOption('#campaign-input-mode','fixed-target');await page.locator('#campaign-fixed-target').fill('>fixed target\nAG\n>fixed partner\nVC\n');await page.locator('#campaign-file').setInputFiles({name:'preview-only.fasta',mimeType:'text/plain',buffer:Buffer.from('>candidate one\nLK\n>candidate two\nFT\n')});await page.waitForFunction(()=>document.querySelectorAll('#campaign-input-preview li').length===2);await page.waitForTimeout(450);await page.evaluate(()=>scrollTo(0,0));
assert.match(await page.locator('#campaign-input-preview').textContent(),/A \+ B \+ C/);assert.match(await page.locator('.preparation-budget-note').textContent(),/不是实测利用率/);await page.screenshot({path:out+'/screening-preview-live.png',fullPage:true});
checks.push('Actual application previews two fixed-plus-candidate combinations without submitting or running prediction');
await page.setViewportSize({width:390,height:844});await page.locator('#campaign-input-preview').scrollIntoViewIfNeeded();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'/screening-preview-mobile-live.png'});checks.push('Actual fixed-target preview and admission panel fit the mobile layout');
assert.deepEqual(errors,[]);checks.push('No JavaScript runtime errors');
fs.writeFileSync(out+'/pipeline-polish-live-verification.json',JSON.stringify({date:new Date().toISOString(),scope:'Read-only live local application with two loopback reference workers; no real AF3 search/GPU/cloud execution',status:'passed',checks,errors},null,2)+'\n');console.log(JSON.stringify({checks,errors}));
}finally{await browser?.close();}})().catch(e=>{console.error(e);process.exitCode=1});
