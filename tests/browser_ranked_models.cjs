/* Ranked-model switcher contract: synthetic AF3-style multi-sample job and a
   single-model reference job, all API traffic intercepted on an isolated static
   server. No GPU, real prediction engine or user records involved. */
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),http=require('node:http'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'artifacts');
const checks=[],errors=[];let server,browser;
// Column-exact ATOM records: name 13-16, resn 18-20, chain 22, resi 23-26,
// x/y/z 31-54, occ 55-60, b-factor 61-66. 3Dmol reads these fixed columns, so
// the B factors (the per-residue pLDDT of a ranked sample) must land exactly.
const atom=(serial,resn,chain,resi,x,y,z,b)=>`ATOM  ${String(serial).padStart(5)}  CA  ${String(resn).padEnd(3)} ${chain}${String(resi).padStart(4)}    ${x.toFixed(3).padStart(8)}${y.toFixed(3).padStart(8)}${z.toFixed(3).padStart(8)}  1.00${b.toFixed(2).padStart(6)}           C`;
const pdbPrimary=[...Array(6)].map((_,i)=>atom(i+1,'ALA','A',i+1,1+i*1.2,2,3,88-i*3)).join('\n')+'\nEND\n';
const pdbModel2=[...Array(10)].map((_,i)=>atom(i+1,'GLY','A',i+1,1+i*1.1,3,4,[92,70,35,64,48,55,81,12,76,39][i])).join('\n')+'\nEND\n';
const jobs=[
  {id:'job-r',name:'公开参考回放',engine:'reference',status:'completed',updatedAt:'2026-10-24T10:00:00Z',chains:[{id:'A',sequence:'ACDEFG'}],result:{sequenceLength:6,meanPlddt:80.2}},
  {id:'job-m',name:'多采样模型任务',engine:'alphafold3',status:'completed',updatedAt:'2026-10-24T10:00:00Z',chains:[{id:'A',sequence:'ACDEFGHIKL'}],result:{sequenceLength:10,meanPlddt:81.0,iptm:0.85}},
  {id:'job-q',name:'排队中任务',engine:'alphafold3',status:'queued',updatedAt:'2026-10-24T10:00:00Z',chains:[{id:'A',sequence:'ACDEFG'}]},
];
const detail={
  'job-m':{...jobs.find(j=>j.id==='job-m'),result:{structureUrl:'/api/jobs/job-m/structure',structureFormat:'pdb',sequenceLength:10,meanPlddt:81.0,ptm:0.8,iptm:0.85,rankingScore:0.842,rankingMetric:'0.8 ipTM + 0.2 pTM',plddt:[88,85,82,79,76,73,70,67,64,61],residues:[],provenance:{mode:'prediction'},
    rankedModels:[
      {rank:1,name:'sample-0',structureUrl:'/api/jobs/job-m/artifact?name=sample-0.pdb',structureFormat:'pdb',meanPlddt:88.1,ptm:0.8,iptm:0.85,rankingScore:0.842},
      {rank:2,name:'sample-1',structureUrl:'/api/jobs/job-m/artifact?name=sample-1.pdb',structureFormat:'pdb',meanPlddt:71.4,ptm:0.6,iptm:0.62,rankingScore:0.610},
    ],artifacts:[{name:'scores.json',url:'/api/jobs/job-m/artifact?name=scores.json',size:2048}]}},
  'job-r':{...jobs.find(j=>j.id==='job-r'),result:{structureUrl:'/api/jobs/job-r/structure',structureFormat:'pdb',sequenceLength:6,meanPlddt:80.2,plddt:[],residues:[],provenance:{mode:'reference'},
    rankedModels:[{rank:1,name:'Polyubiquitin-B',structureUrl:'/api/jobs/job-r/artifact?name=afdb.pdb',structureFormat:'pdb',meanPlddt:80.2}],artifacts:[]}},
};
const json=(data,status=200)=>({status,contentType:'application/json',body:JSON.stringify(data)});
(async()=>{
  await fs.mkdir(out,{recursive:true});
  server=http.createServer(async(req,res)=>{try{const pathname=new URL(req.url,'http://localhost').pathname;const file=path.resolve(root,'web',pathname==='/'?'index.html':pathname.slice(1));if(!file.startsWith(path.join(root,'web')+path.sep))throw Error();const bytes=await fs.readFile(file);res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'text/javascript','.css':'text/css'})[path.extname(file)]||'application/octet-stream'});res.end(bytes);}catch{res.writeHead(404).end();}});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:1536,height:1080}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{const u=new URL(route.request().url());let payload;
    if(u.pathname==='/api/health')payload=json({status:'ok'});
    else if(u.pathname==='/api/system')payload=json({engines:[{id:'reference',available:true},{id:'alphafold3',available:true}],pipeline:{predictionMode:'local'}});
    else if(u.pathname==='/api/jobs')payload=json(jobs);
    else if(u.pathname==='/api/jobs/job-m/structure')payload={status:200,contentType:'text/plain',body:pdbPrimary};
    else if(u.pathname==='/api/jobs/job-r/structure')payload={status:200,contentType:'text/plain',body:pdbPrimary};
    else if(u.pathname==='/api/jobs/job-m/artifact')payload={status:200,contentType:'text/plain',body:u.searchParams.get('name')==='sample-1.pdb'?pdbModel2:pdbPrimary};
    else if(u.pathname==='/api/jobs/job-m/archive')payload={status:200,contentType:'application/zip',headers:{'Content-Disposition':'attachment; filename="job-job-m.zip"'},body:Buffer.from([0x50,0x4b,0x03,0x04,0x14,0x00,0x00,0x00,0x08,0x00])};
    else if(u.pathname==='/api/jobs/job-r/artifact')payload={status:200,contentType:'text/plain',body:pdbPrimary};
    else if(u.pathname.startsWith('/api/jobs/')){const record=detail[u.pathname.split('/')[3]];payload=record?json(record):json(null,404);}
    else if(u.pathname==='/api/examples/ubiquitin-afdb')payload=json({name:'Fixture example',chains:[{id:'A',sequence:'ACDEFGHIK'}]});
    else if(u.pathname==='/api/campaigns'||u.pathname==='/api/databases')payload=json([]);
    else if(u.pathname==='/api/pipeline/config')payload=json({capabilities:[],prepWorkers:1,gpuWorkers:1});
    else if(u.pathname==='/api/cluster')payload=json({nodes:[],batches:[],transfers:[],events:[]});
    else throw Error('Unmocked API '+u.pathname);
    await route.fulfill(payload);
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/#job/job-m`);
  await page.waitForSelector('#model-select');
  await page.waitForFunction(()=>state.viewer?.getModel()?.selectedAtoms?.({})?.length===6,null,{timeout:15000});
  assert.equal(await page.evaluate(()=>location.hash),'#job/job-m');
  assert.equal(await page.evaluate(()=>state.selectedId),'job-m');
  checks.push('A #job/{id} URL opens that job directly with the hash preserved');
  const options=await page.locator('#model-select option').allTextContents();
  assert.equal(options.length,2);
  assert.match(options[0],/排名 1 .*sample-0 .*pLDDT 88\.1 .*排名分 0\.842/);
  assert.match(options[1],/排名 2 .*sample-1 .*pLDDT 71\.4 .*排名分 0\.610/);
  assert.equal(await page.locator('#model-select').inputValue(),'0');
  const rank1Panel=await page.locator('.analysis-panel:not(.pae-panel)').textContent();
  assert.match(rank1Panel,/81\.0/);
  assert.doesNotMatch(rank1Panel,/B 因子/);
  checks.push('Model picker lists every ranked sample with its AF3-style rank, pLDDT and ranking score, rank 1 selected by default');
  const modelTable=page.locator('section.panel:has-text("模型与结果制品") tbody');
  const headers=await page.locator('section.panel:has-text("模型与结果制品") thead th').allTextContents();
  assert.ok(headers.includes('排名分'));
  const firstScores=await modelTable.locator('tr').nth(0).innerText();
  assert.match(firstScores,/0\.842/);
  assert.ok(await modelTable.locator('tr').nth(0).locator('[data-view-model][disabled]').count()===1);
  assert.ok(await modelTable.locator('tr').nth(1).locator('[data-view-model]:not([disabled])').count()===1);
  checks.push('Models table shows the per-sample ranking score and marks the model currently in the viewer');
  await page.selectOption('#model-select','1');
  await page.waitForFunction(()=>state.viewer?.getModel()?.selectedAtoms?.({})?.length===10,null,{timeout:15000});
  assert.equal(await page.locator('#model-select').inputValue(),'1');
  assert.match(await page.locator('.model-scope-note').textContent(),/正在查看其他排名/);
  await page.waitForFunction(()=>document.querySelector('.analysis-panel:not(.pae-panel)')?.textContent.includes('57.2'),null,{timeout:8000});
  assert.match(await page.locator('.analysis-panel:not(.pae-panel)').textContent(),/B 因子/);
  const href=await page.locator('.viewer-tabline a[download]').getAttribute('href');
  assert.match(decodeURIComponent(href),/sample-1\.pdb/);
  checks.push('Switching the picker reloads the selected sample geometry into the viewer and retargets the download link');
  await modelTable.locator('tr').nth(0).locator('[data-view-model]').click();
  await page.waitForFunction(()=>state.viewer?.getModel()?.selectedAtoms?.({})?.length===6,null,{timeout:15000});
  assert.equal(await page.locator('#model-select').inputValue(),'0');
  await page.waitForFunction(()=>{const panel=document.querySelector('.analysis-panel:not(.pae-panel)')?.textContent||'';return panel.includes('81.0')&&!panel.includes('B 因子');},null,{timeout:8000});
  checks.push('The confidence panel curve follows the selected sample (B-factor derived) and returns to the primary curve on rank 1');
  checks.push('The models table 三维查看 action switches the viewer back to rank 1');
  const archiveDownload=page.waitForEvent('download');
  await page.locator('a[href*="/archive"]').click();
  const archive=await archiveDownload;
  assert.match(archive.url(),/\/api\/jobs\/job-m\/archive$/);
  checks.push('下载全部 zip triggers a single-archive download of the job artifacts');
  await page.locator('[data-view="jobs"]').click();
  const multiCell=page.locator('tr[data-job-id="job-m"] td:nth-child(4)');
  assert.match(await multiCell.innerText(),/81\.0/);
  assert.match(await multiCell.innerText(),/ipTM 0\.85/);
  const chips=await multiCell.locator('.campaign-score-band').allTextContents();
  assert.equal(chips.length,2);
  assert.match(chips.join('|'),/70–90 置信/);
  assert.match(chips.join('|'),/≥0\.8 高/);
  const referenceCell=page.locator('tr[data-job-id="job-r"] td:nth-child(4)');
  assert.match(await referenceCell.innerText(),/80\.2/);
  assert.equal(await referenceCell.locator('.campaign-score-band').count(),1);
  const queuedCell=page.locator('tr[data-job-id="job-q"] td:nth-child(4)');
  assert.equal((await queuedCell.innerText()).trim(),'—');
  assert.equal(await queuedCell.locator('.campaign-score-band').count(),0);
  checks.push('Jobs table confidence column bands pLDDT (AF DB) and ipTM (EBI), muted dash for unscored jobs');
  assert.equal(await page.locator('tbody tr[data-job-id]').first().getAttribute('data-job-id'),'job-r');
  await page.locator('[data-jobs-sort]').click();
  await page.waitForFunction(()=>document.querySelector('tbody tr[data-job-id]')?.getAttribute('data-job-id')==='job-m',null,{timeout:5000});
  assert.match(await page.locator('#jobs-sort-confidence').textContent(),/置信度 ↓/);
  assert.equal(await page.locator('thead th[aria-sort="descending"]').count(),1);
  assert.deepEqual(await page.locator('tbody tr[data-job-id]').evaluateAll(rows=>rows.map(r=>r.getAttribute('data-job-id'))),['job-m','job-r','job-q']);
  await page.locator('[data-jobs-sort]').click();
  await page.waitForFunction(()=>document.querySelector('tbody tr[data-job-id]')?.getAttribute('data-job-id')==='job-r',null,{timeout:5000});
  checks.push('Confidence column header sorts completed jobs by mean pLDDT descending (BLAST-style), toggling back restores recency');
  await page.locator('[data-job-id="job-r"] .row-action').click();
  await page.waitForFunction(()=>document.querySelector('.job-detail-bar strong')?.textContent==='公开参考回放');
  await page.waitForFunction(()=>state.viewer?.getModel()?.selectedAtoms?.({})?.length===6,null,{timeout:15000});
  assert.equal(await page.locator('#model-select').count(),0);
  assert.equal(await page.locator('[data-view-model]').count(),0);
  assert.equal(await page.locator('a[href*="/archive"]').count(),0);
  checks.push('Single-model reference jobs keep the plain viewer without a picker or per-row view actions');
  await page.locator('[data-view="jobs"]').click();
  await page.locator('[data-job-id="job-m"] .row-action').click();
  await page.waitForFunction(()=>document.querySelector('#model-select')&&state.viewer?.getModel()?.selectedAtoms?.({})?.length===6,null,{timeout:15000});
  assert.equal(await page.locator('#model-select').inputValue(),'0');
  checks.push('Returning to a multi-sample job resets the viewer to rank 1 instead of keeping another job\'s stale selection');
  await page.screenshot({path:path.join(out,'ranked-models.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  const report={status:'passed',checks,count:checks.length,errors,note:'Synthetic ranked samples and mocked API; validates UI switching only, not model prediction or GPU performance.'};
  await fs.writeFile(path.join(out,'ranked-models-verification.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();if(server)await new Promise(r=>server.close(r));});
