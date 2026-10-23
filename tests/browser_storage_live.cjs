/* Live read-only UI verification. A scan must already have been explicitly run;
   this test never starts a scan, creates a plan, or changes a cache artifact. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const base=process.env.STUDIO_URL||'http://127.0.0.1:8765';
  const artifacts=path.resolve(__dirname,'../artifacts');
  const errors=[],checks=[],posts=[];let browser;
  try{
    const before=await(await fetch(base+'/api/storage')).json();
    const scan=before.state.latestScan;
    assert.equal(scan.status,'completed');
    assert.equal(scan.inventoryComplete,true);
    assert.equal(before.capabilities.purge,false);
    checks.push('An explicit bounded scan has completed; inventory scope and purge-disabled capability come from the real controller');
    browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
    const page=await browser.newPage({viewport:{width:1500,height:1100}});
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.method()==='POST')posts.push(request.url());});
    await page.goto(base+'/?ui=rich#storage',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>storageState.snapshot?.state?.latestScan?.status==='completed');
    await page.waitForTimeout(450);await page.evaluate(()=>scrollTo(0,0));
    assert.equal(await page.locator('.storage-kind-cards button').count(),4);
    assert.match(await page.locator('.storage-scan-notice').textContent(),/2026/);
    assert.match(await page.locator('[data-storage-kind="quarantine"]').textContent(),/尚占磁盘/);
    // This Chrome/Playwright combination reports option.isDisabled() as false
    // despite the native disabled property; inspect the actual option state.
    assert.equal(await page.locator('#storage-plan-action option[value="purge"]').evaluate(option=>option.disabled),true);
    checks.push('Real inventory displays both cache layers, quarantine occupancy, epoch-second timestamps and the disabled permanent-deletion option');
    const entries=await(await fetch(base+'/api/storage/entries?scanId='+scan.id+'&limit=50&offset=0')).json();
    assert.equal(await page.locator('.storage-entry-panel tbody tr').count(),Math.max(entries.items.length,1));
    checks.push('Displayed first page agrees with the real SQLite inventory page');
    await page.screenshot({path:artifacts+'/storage-overview-live.png',fullPage:true});
    await page.locator('[data-storage-kind="leaf"]').click();
    await page.waitForFunction(()=>storageState.kind==='leaf'&&!storageState.loading);
    assert.equal(await page.locator('#storage-kind-filter').inputValue(),'leaf');
    checks.push('Cache-type selection queries the real catalog without traversing or changing cache artifacts');
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(450);await page.evaluate(()=>scrollTo(0,0));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:artifacts+'/storage-mobile-live.png',fullPage:true});
    checks.push('Live storage controls and tables fit a 390px viewport without page overflow');
    await page.locator('[data-view="pipeline"]').click();
    assert.equal(await page.evaluate(()=>storageState.timer),null);
    const after=await(await fetch(base+'/api/storage')).json();
    assert.equal(after.state.latestScan.id,scan.id);
    assert.deepEqual(posts,[]);assert.deepEqual(errors,[]);
    checks.push('Browsing, filtering and navigation start no POST operation, keep the same scan, stop polling on exit and produce no JavaScript errors');
    const report={date:new Date().toISOString(),status:'passed',scope:'Actual loopback controller and existing reference data; GET-only browser verification after one explicit inventory scan. No quarantine, restore, purge, model or cloud execution.',checks,errors,posts,scanId:scan.id};
    fs.writeFileSync(artifacts+'/storage-live-verification.json',JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report,null,2));
  }finally{await browser?.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
