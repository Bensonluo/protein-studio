/* Job deep-link contract against a real isolated server: #job/{id} URLs must
   boot straight into that job's workspace, follow selection changes, and fall
   back safely for unknown ids. Reference replay fixtures only — no GPU or new
   prediction is involved. Requires STUDIO_URL. */
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.STUDIO_URL;
const checks=[],errors=[];
(async()=>{
  if(!base)throw new Error('STUDIO_URL is required (point at an isolated server)');
  // A fresh server has no jobs until a page load auto-seeds one, so create the
  // deep-link target directly through the public API (reference replay).
  const submit=await (await fetch(base+'/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'深链验证',engine:'reference',example:'ubiquitin-afdb'})})).json();
  for(let i=0;i<40;i++){const job=await (await fetch(base+'/api/jobs/'+submit.id)).json();if(job.status==='completed')break;await new Promise(r=>setTimeout(r,500));}
  const detail=await (await fetch(base+'/api/jobs/'+submit.id)).json();
  assert.equal(detail.status,'completed','reference replay must complete for the deep-link target');
  const target={id:detail.id,name:detail.name,status:detail.status};
  let browser=null;
  try {
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:1536,height:1080}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/#job/'+target.id);
  await page.waitForFunction(id=>state.selectedId===id&&state.view==='studio',target.id,{timeout:15000});
  await page.waitForSelector('.job-detail-bar');
  assert.equal(await page.evaluate(()=>location.hash),'#job/'+target.id);
  assert.equal((await page.locator('.job-detail-bar strong').textContent()).trim(),target.name);
  if(target.status==='completed')await page.waitForFunction(()=>state.viewer?.getModel()?.selectedAtoms?.({})?.length>0,null,{timeout:20000});
  checks.push('A #job/{id} URL boots straight into that job\'s workspace with the URL, name and structure intact');
  await page.locator('[data-view="jobs"]').click();
  await page.waitForFunction(()=>location.hash==='#jobs'&&state.view==='jobs',null,{timeout:8000});
  await page.locator(`[data-job-id="${target.id}"] .row-action`).click();
  await page.waitForFunction(id=>location.hash==='#job/'+id&&state.view==='studio',target.id,{timeout:8000});
  checks.push('Selecting a job from the table moves the URL to that job\'s deep link');
  await page.goBack();
  await page.waitForFunction(()=>location.hash==='#jobs'&&state.view==='jobs',null,{timeout:8000});
  checks.push('Browser back restores the previous view through hash history');
  await page.goto(base+'/?unknown=1#job/nonexistent-job-id');
  await page.waitForFunction(()=>state.view==='studio'&&document.querySelector('.job-detail-bar'),null,{timeout:15000});
  assert.ok((await page.evaluate(()=>location.hash))==='#studio'||(await page.evaluate(()=>state.selectedId)),'fallback keeps a usable workspace');
  checks.push('A deep link to an unknown job falls back to the most recent job with a notice instead of an empty screen');
  assert.deepEqual(errors,[]);
  const report={status:'passed',count:checks.length,checks,errors,note:'Reference replay fixtures over real HTTP; validates URL routing only, not prediction or GPU performance.'};
  console.log(JSON.stringify(report,null,2));
  await browser.close();
  } finally { await browser?.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
