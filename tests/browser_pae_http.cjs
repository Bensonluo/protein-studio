/* Real browser -> real temporary HTTP service -> real generated PAE artifacts.
   The 256x256 asymmetric matrix is synthetic UI test input, not confidence for
   the accompanying public reference structure. No model or GPU is executed. */
const assert=require('node:assert/strict'),fs=require('node:fs'),fsp=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),output=path.join(root,'artifacts'),python=process.env.PYTHON_BIN||path.join(root,'.venv/bin/python');
let browser,server,temp,base,page,report;const checks=[],errors=[],requests=[];
const harness=`
import json,signal,sys,threading
from pathlib import Path
from backend.server import StudioApplication,StudioHTTPServer
app=StudioApplication(data_dir=sys.argv[1],workers=1)
values=[[round(r*0.1+c*0.001,3) for c in range(256)] for r in range(256)]
job=app.import_structure({'name':'Synthetic PAE HTTP test; no prediction','format':'pdb','source':'unknown',
 'structureText':Path('fixtures/1CRN.pdb').read_text(), 'confidence':{'pae':values}})
http=StudioHTTPServer(('127.0.0.1',0),app)
signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=http.shutdown,daemon=True).start())
print('READY '+json.dumps({'port':http.server_port,'jobId':job['id']}),flush=True)
try:http.serve_forever(poll_interval=.05)
finally:http.server_close();app.stop()
`;
async function json(url){const response=await fetch(base+url);assert.equal(response.status,200);return response.json();}
async function hover(y,x){const box=await page.locator('#pae-canvas').boundingBox(),size=await page.locator('#pae-canvas').evaluate(el=>[el.width,el.height]);await page.mouse.move(box.x+(x+.5)*box.width/size[0],box.y+(y+.5)*box.height/size[1]);return page.locator('#pae-tooltip').textContent();}
(async()=>{
 fs.mkdirSync(output,{recursive:true});temp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'foldspace-pae-http-fixture-')));
 const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!key.startsWith('PROTEIN_STUDIO_')));Object.assign(env,{PROTEIN_STUDIO_DISTRIBUTED:'0',PROTEIN_STUDIO_PREDICTION_MODE:'legacy'});
 let stderr='';server=spawn(python,['-u','-c',harness,temp],{cwd:root,env,stdio:['ignore','pipe','pipe']});server.stderr.on('data',chunk=>stderr+=chunk);
 const ready=await new Promise((resolve,reject)=>{let pending='';const timer=setTimeout(()=>reject(new Error('Temporary service timeout: '+stderr)),20000);server.stdout.on('data',chunk=>{pending+=chunk;const match=pending.match(/READY (.+)\n/);if(match){clearTimeout(timer);resolve(JSON.parse(match[1]));}});server.once('exit',code=>{clearTimeout(timer);reject(new Error('Temporary service exited '+code+': '+stderr));});});base=`http://127.0.0.1:${ready.port}`;
 const detail=await json('/api/jobs/'+ready.jobId),d=detail.result.paeArtifact;assert.equal(detail.result.pae,null);assert.equal(d.status,'available');assert.deepEqual([d.rows,d.cols],[256,256]);assert.ok(Buffer.byteLength(JSON.stringify(detail))<100000);checks.push('A real imported synthetic 256×256 PAE is externalized; job detail is below 100KB with pae=null and a descriptor');
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});page=await browser.newPage({viewport:{width:1450,height:1100}});page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{const url=new URL(request.url());if(url.pathname.includes('/api/'))requests.push({path:url.pathname+url.search,method:request.method(),type:request.resourceType()});});
 await page.goto(base+'/#studio',{waitUntil:'domcontentloaded'});await page.waitForSelector('#pae-load-preview');assert.equal(requests.filter(r=>r.path.includes('/pae?')).length,0);assert.equal(requests.filter(r=>r.method==='POST').length,0);checks.push('Opening the real job requests metadata and structure only; no PAE preview, original confidence download or new job submission occurs');
 await page.locator('#pae-load-preview').click();await page.waitForSelector('#pae-plot:not([hidden])');const previewRequest=requests.find(r=>r.path.includes('/pae?')),preview=await json(previewRequest.path);assert.equal(preview.method,'nearest-center');assert.ok(preview.shape[0]<=256);assert.match(await hover(2,3),new RegExp(`行 ${preview.rowIndices[2]+1} / 列 ${preview.colIndices[3]+1}`));assert.match(await hover(2,3),new RegExp(`${preview.values[2][3]} Å`));assert.equal(preview.values[2][3],Number((preview.rowIndices[2]*.1+preview.colIndices[3]*.001).toFixed(3)));checks.push('Explicit preview travels through the real HTTP endpoint and file-backed matrix reader with correct source indices and asymmetric values');
 await page.locator('#pae-row').fill('250');await page.locator('#pae-col').fill('252');await page.selectOption('#pae-window-size','128');await page.locator('#pae-load-window').click();await page.waitForFunction(()=>document.querySelector('#pae-mode-label').textContent==='局部原始矩阵');const tileRequest=requests.filter(r=>r.path.includes('/pae?')).at(-1),tile=await json(tileRequest.path);assert.deepEqual(tile.shape,[7,5]);assert.equal(tile.values[6][4],25.755);assert.match(await hover(6,4),/行 256 \/ 列 256 · 25.755 Å/);checks.push('Real exact-window request reads only the 7×5 edge tile and the browser preserves the final original value');
 const downloadEvent=page.waitForEvent('download');await page.locator('.pae-downloads a').first().click();const download=await downloadEvent,saved=path.join(temp,'downloaded-source.json');await download.saveAs(saved);const sourceBytes=await (await fetch(base+d.downloadUrl)).arrayBuffer();assert.deepEqual(fs.readFileSync(saved),Buffer.from(sourceBytes));const source=JSON.parse(fs.readFileSync(saved,'utf8'));assert.equal(source.pae[255][255],25.755);checks.push('Browser attachment bytes match the real matrix source artifact; full JSON is downloaded outside frontend application state');
 await page.locator('#pae-overview').click();await page.evaluate(()=>window.scrollTo(0,0));await page.waitForTimeout(450);await page.screenshot({path:path.join(output,'pae-http-synthetic-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.evaluate(()=>window.scrollTo(0,0));await page.waitForTimeout(450);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'pae-http-synthetic-mobile.png'),fullPage:true});assert.deepEqual(errors,[]);checks.push('Real-service view and controls remain usable at 390px with no browser runtime errors');
 report={status:'passed',scope:'Real browser, real temporary localhost HTTP service and real matrix artifacts. PAE values are synthetic test input, not confidence for the reference structure; no prediction or GPU execution.',count:checks.length,checks,detailBytes:Buffer.byteLength(JSON.stringify(detail)),sourceDownloadBytes:sourceBytes.byteLength,sourceDownloadSha256:crypto.createHash('sha256').update(Buffer.from(sourceBytes)).digest('hex'),previewShape:preview.shape,exactTileShape:tile.shape,requests,errors,temporaryDataRemoved:false};
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
 if(browser)await browser.close();if(server&&server.exitCode===null){server.kill('SIGTERM');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{if(server.exitCode===null)server.kill('SIGKILL');resolve();},5000).unref();});}
 if(temp)await fsp.rm(temp,{recursive:true,force:true});if(report){report.temporaryDataRemoved=!fs.existsSync(temp);await fsp.writeFile(path.join(output,'pae-http-browser-verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));}
});
