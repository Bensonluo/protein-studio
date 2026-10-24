/* Real temporary HTTP server and browser: no main service, GPU, deployment or external requests. */
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');let temporary,server,browser;const errors=[],requests=[];
(async()=>{
  temporary=await fs.mkdtemp(path.join(os.tmpdir(),'foldspace-base-path-'));
  const python=process.env.PYTHON_BIN||path.join(root,'.venv/bin/python');
  const program=`import sys\nfrom backend.server import StudioApplication,StudioHTTPServer\napp=StudioApplication(sys.argv[1],base_path='/foldspace')\ns=StudioHTTPServer(('127.0.0.1',0),app)\nprint(s.server_port,flush=True)\ntry:s.serve_forever(poll_interval=.05)\nfinally:s.server_close();app.close()\n`;
  server=spawn(python,['-B','-u','-c',program,temporary],{cwd:root,stdio:['ignore','pipe','pipe']});
  let stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
  const port=await new Promise((resolve,reject)=>{
    const reader=createInterface({input:server.stdout});const timer=setTimeout(()=>reject(Error('HTTP fixture startup timed out: '+stderr)),10000);
    reader.once('line',line=>{clearTimeout(timer);reader.close();resolve(Number(line));});
    server.once('exit',code=>{clearTimeout(timer);reject(Error('HTTP fixture exited '+code+': '+stderr));});
  });
  const origin='http://127.0.0.1:'+port;
  const seeded=await fetch(origin+'/foldspace/api/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    name:'Subpath browser fixture',source:'experimental',format:'pdb',structureText:await fs.readFile(path.join(root,'fixtures/1CRN.pdb'),'utf8'),
    confidence:{pae:Array.from({length:129},(_,i)=>Array.from({length:129},(_,j)=>(i+j)/10))}
  })});
  assert.equal(seeded.status,201);const job=await seeded.json();
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(request.url().startsWith(origin))requests.push(new URL(request.url()).pathname);});
  await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
  await page.goto(origin+'/foldspace/');
  await page.waitForFunction(()=>state.job?.status==='completed'&&Boolean(state.viewer));
  assert.equal(await page.evaluate(()=>appUrl('/api/health')),'/foldspace/api/health');
  assert.equal(await page.evaluate(()=>appUrl('/foldspace/api/health')),'/foldspace/api/health');
  assert.equal(await page.evaluate(()=>appUrl('https://example.org/api/health')),'https://example.org/api/health');
  assert.match(await page.locator('.viewer-panel a[download]').getAttribute('href'),/^\/foldspace\/api\/jobs\//);
  await page.locator('#pae-load-preview').click();await page.waitForSelector('#pae-plot:not([hidden])');
  assert.match(await page.locator('.pae-downloads a').first().getAttribute('href'),/^\/foldspace\/api\/jobs\//);
  assert.equal(await page.evaluate(()=>campaignExportUrl({id:'campaign-fixture'})),'/foldspace/api/campaigns/campaign-fixture/export');
  await page.locator('[data-view="cluster"]').click();await page.waitForSelector('#cluster-page');
  await page.waitForFunction(()=>!document.querySelector('#cluster-loading'));
  await page.locator('[data-view="sources"]').click();
  await page.waitForFunction(()=>state.sources.length>0);
  assert(requests.every(request=>request.startsWith('/foldspace/')),'All app requests must remain under the configured mount');
  for(const expected of ['/foldspace/vendor/3Dmol-min.js','/foldspace/api/jobs/'+job.id+'/structure','/foldspace/api/jobs/'+job.id+'/pae','/foldspace/sources.json','/foldspace/api/cluster'])assert(requests.includes(expected),expected);
  assert.deepEqual(errors,[]);
  const report={status:'passed',scope:'Actual isolated HTTP server, public experimental fixture and synthetic PAE; no prediction or deployment.',checks:[
    'Relative styles/scripts and 3Dmol load under /foldspace/',
    'Jobs API and structure fetch stay mounted; the 3D viewer renders',
    'Lazy PAE preview and download URLs stay mounted',
    'Campaign export, cluster fetch and source registry use the prefix',
    'No root-path or external requests; no browser errors'],requests:[...new Set(requests)],errors};
  await fs.mkdir(path.join(root,'artifacts'),{recursive:true});await fs.writeFile(path.join(root,'artifacts/base-path-browser.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  await browser?.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await new Promise(resolve=>server.once('exit',resolve));}
  if(temporary)await fs.rm(temporary,{recursive:true,force:true});
});
