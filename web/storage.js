'use strict';

const storageState={snapshot:null,entries:{items:[],total:0},kind:'',offset:0,loading:false,busy:false,error:'',timer:null,awaitedJobId:null,handledJobId:null,plan:null,planPage:{items:[],total:0},planOffset:0,planLoading:false,returnFocus:null,settings:{maxEntries:'100000',maxSeconds:'30',minAgeDays:'7',retentionDays:'7',action:'quarantine',whole:true,leaf:true,partial:true}};
const storageKinds={whole:'整目标准备缓存',leaf:'AF3 链准备缓存',partial:'未完成残留',quarantine:'隔离区',unknown:'未识别 / 异常'};
storageState.planRequest=0;
const storageActions={scan:'扫描',plan:'生成预览',quarantine:'移入隔离区',restore:'恢复缓存',purge:'永久删除'};
function storageBytes(value){const n=Number(value);if(value===null||value===undefined||!Number.isFinite(n)||n<0)return '—';if(n===0)return '0 B';const units=['B','KiB','MiB','GiB','TiB'],index=Math.min(Math.floor(Math.log(n)/Math.log(1024)),units.length-1);return `${(n/1024**index).toLocaleString('zh-CN',{maximumFractionDigits:index?2:0})} ${units[index]}`;}
function storageCount(value){return Number(value||0).toLocaleString('zh-CN');}
function storageTime(value){if(value===null||value===undefined||value==='')return '';const date=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(date.valueOf())?String(value):date.toLocaleString('zh-CN',{hour12:false});}
function storageReason(value){if(Array.isArray(value))return value.map(storageReason).filter(Boolean).join('；');return typeof value==='object'&&value?value.message||value.code||'受保护':String(value||'');}
function storageRunning(){return storageState.busy||storageState.snapshot?.job?.status==='running'||!!storageState.snapshot?.state?.activeOperation;}
function storageScanUsable(){const scan=storageState.snapshot?.state?.latestScan;return !!scan?.id&&!scan.truncated&&!scan.failClosed&&scan.inventoryComplete!==false&&!Number(scan.unreadableEntries||0)&&scan.status==='completed';}
function storageCapability(action){return storageState.snapshot?.capabilities?.[action]===true;}
function storageEntryName(item){return item.relativePath||item.id||'未提供路径';}
function storageOperationNote(job){
  if(!job)return '尚无本次服务运行中的操作记录。';
  const result=job.result||{},action=result.action||job.action;
  if(job.status==='failed')return job.error||'操作失败；查看服务返回的具体错误。';
  if(job.status==='cancelled')return '操作已取消；已完成的条目以服务记录为准。';
  if(job.status==='running')return job.cancelRequested?'已请求停止，等待当前安全边界。':'后台操作进行中；此页面仅读取已有进度。';
  if(job.action==='plan')return '预览已生成；尚未执行任何清理。';
  const partial=['partial','failed','cancelled'].includes(result.status)?'部分条目未完成，请查看操作诊断。':'';
  if(action==='quarantine')return partial+'已处理条目移入同一存储中的隔离区，磁盘空间尚未释放。';
  if(action==='restore')return partial+'恢复操作已结束，具体条目结果以服务记录为准。';
  if(action==='purge')return partial+'永久删除操作已结束；删除文件的逻辑长度不等于文件系统空闲空间增量。';
  return '操作已结束，可查看最近扫描与条目记录。';
}
function renderStorageControls(){
  const settings=storageState.settings;
  return `<section class="panel storage-controls"><div class="panel-heading"><h3>${icon('search')}按需检查与预览</h3><span class="subtle">PREVIEW FIRST</span></div><form id="storage-scan-form"><p>只扫描控制节点的准备缓存目录。打开本页或刷新状态均不会启动目录扫描。</p><div class="storage-input-grid"><label>文件 / 目录节点上限<input id="storage-max-entries" type="number" min="1" max="1000000" value="${escapeHTML(settings.maxEntries)}" required></label><label>时间预算 / 秒<input id="storage-max-seconds" type="number" min="1" max="300" value="${escapeHTML(settings.maxSeconds)}" required></label></div><button class="button button-primary" id="storage-scan-submit" type="submit">${icon('search')}开始有界扫描</button></form><form id="storage-plan-form"><div class="storage-control-heading">生成具体操作预览</div><label class="storage-field">操作<select id="storage-plan-action"><option value="quarantine" ${settings.action==='quarantine'?'selected':''}>隔离可清理缓存 · 可以恢复</option><option value="purge" ${settings.action==='purge'?'selected':''}>永久删除隔离区内容</option></select></label><div id="storage-quarantine-policy"><label class="storage-field">缓存至少保留 / 天<input id="storage-min-age" type="number" min="0" max="36500" step="0.01" value="${escapeHTML(settings.minAgeDays)}" required></label><fieldset class="storage-kind-checks"><legend>纳入预览的缓存类型</legend>${['whole','leaf','partial'].map(kind=>`<label><input id="storage-kind-${kind}" type="checkbox" ${settings[kind]?'checked':''}>${storageKinds[kind]}</label>`).join('')}</fieldset></div><div id="storage-purge-policy" hidden><label class="storage-field">隔离区至少保留 / 天<input id="storage-retention" type="number" min="0" max="36500" step="0.01" value="${escapeHTML(settings.retentionDays)}" required></label><p>仅预览隔离区中的到期条目。永久删除需要管理员启用，并在预览中单独确认。</p></div><p class="storage-plan-gate" id="storage-plan-gate"></p><button class="button button-secondary" id="storage-plan-submit" type="submit">${icon('layers')}生成清理预览</button></form><div class="storage-controls-footnote"><b>保护可继续执行的任务</b><span>活动任务、租约与失败可恢复引用由服务端检查；受保护条目不会进入清理候选。执行时重新校验。</span></div></section>`;
}
function renderStorage(){
  return `<section class="storage-intro"><div><span class="eyebrow">PREPARATION CACHE LIFECYCLE</span><h2>保留可复用的输入，清楚地回收空间。</h2><p>整目标缓存与共享链缓存分别记录。先看完整扫描与具体计划，再决定隔离、恢复或永久删除。</p></div><span>${icon('server')}</span></section><div class="storage-layout">${renderStorageControls()}<div id="storage-live" class="storage-live">${renderStorageLive()}</div></div>`;
}
function renderStorageLive(){
  const snapshot=storageState.snapshot,scan=snapshot?.state?.latestScan,disk=snapshot?.disk||{},job=snapshot?.job;
  const header=`<div class="storage-live-toolbar"><span>控制节点 · 准备缓存目录</span><button class="button button-secondary" id="storage-refresh">${icon('rotate')}刷新已有状态</button></div>`;
  if(!snapshot)return header+`<section class="panel storage-empty">${icon('server')}<h3>${storageState.error?'尚未读取存储状态':'正在读取已有状态'}</h3><p>此操作不会扫描缓存目录。</p>${storageState.error?`<p class="storage-error" role="alert">${escapeHTML(storageState.error)}</p>`:''}</section>`;
  const cards=`<div class="storage-summary"><section class="panel"><span>磁盘可用空间</span><strong>${storageBytes(disk.freeBytes)}</strong><small>文件系统总容量 ${storageBytes(disk.totalBytes)}</small></section><section class="panel"><span>已检查文件 / 目录节点</span><strong>${scan?storageCount(scan.scannedEntries??scan.completedEntries):'—'} <em>项</em></strong><small>${scan?scan.truncated?'部分扫描 · 不能视作全量':scan.status==='completed'?(scan.inventoryComplete===true?'完整扫描记录':'扫描已结束'):escapeHTML(scan.status||'状态未知'):'尚未扫描'}</small></section><section class="panel"><span>受保护条目</span><strong>${scan?storageCount(scan.protectedEntries):'—'} <em>项</em></strong><small>活动 / 可恢复引用等保护原因</small></section></div>`;
  const scanNote=!scan?'<p class="storage-scan-notice">尚无扫描记录。点击“开始有界扫描”后显示条目与逻辑文件大小。</p>':`<p class="storage-scan-notice ${scan.truncated||scan.failClosed||scan.inventoryComplete===false||Number(scan.unreadableEntries||0)>0?'incomplete':''}">${Number(scan.unreadableEntries||0)>0?`扫描存在 ${storageCount(scan.unreadableEntries)} 个不可读条目，目录清单与总量不完整；清理预览已停止。请查看诊断并修复读取问题后重新扫描。`:scan.truncated?'扫描达到预算，结果不完整。当前大小与数量仅覆盖已扫描部分；完整扫描前不生成清理计划。':scan.inventoryComplete===false?'扫描清单不完整，清理预览已停止；请查看诊断并重新扫描。':scan.failClosed?'扫描发现无法确认的保护状态，清理预览已停止；请先查看诊断。':`扫描 ${escapeHTML(scan.id)}${scan.finishedAt?' · '+escapeHTML(storageTime(scan.finishedAt)):''} · 文件逻辑长度 ${storageBytes(scan.logicalBytes)}`} ${scan.status==='running'?'扫描进行中。':''}</p>`;
  const diagnostics=(scan?.diagnostics||[]).length?`<details class="storage-diagnostics"><summary>${scan.diagnostics.length} 条扫描诊断</summary><ul>${scan.diagnostics.map(d=>`<li>${escapeHTML(storageReason(d))}</li>`).join('')}</ul></details>`:'';
  const kinds=`<div class="storage-kind-cards">${['whole','leaf','partial','quarantine'].map(kind=>{const data=scan?.byKind?.[kind]||{};return `<button data-storage-kind="${kind}" class="${storageState.kind===kind?'selected':''}" aria-pressed="${storageState.kind===kind}"><span>${storageKinds[kind]}</span><strong>${scan?storageBytes(data.logicalBytes??0):'—'}</strong><small>${scan?storageCount(data.entries):'—'} 项${kind==='quarantine'?' · 尚占磁盘':''}</small></button>`;}).join('')}</div>`;
  const operation=`<section class="panel storage-operation"><div><span class="storage-operation-label">最近操作</span><h3>${job?escapeHTML(job.action==='plan'?`${storageActions.plan}${job.result?.action?' · '+(storageActions[job.result.action]||job.result.action):''}`:storageActions[job.result?.action||job.action]||job.action):'暂无操作'}${job?`<i class="storage-job-state ${escapeHTML(job.status)}">${escapeHTML(({running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消'})[job.status]||job.status)}</i>`:''}</h3><p>${escapeHTML(storageOperationNote(job))}</p>${renderStorageOperationFacts(job)}</div>${job?.status==='running'?`<button class="button button-secondary" id="storage-cancel" ${job.cancelRequested?'disabled':''}>${job.cancelRequested?'已请求停止':'停止后台操作'}</button>`:job?.status==='completed'&&job.action==='plan'&&job.result?.id?'<button class="button button-secondary" id="storage-reopen-plan">查看具体预览</button>':''}</section>`;
  const table=renderStorageEntryTable(scan);
  return header+cards+scanNote+diagnostics+kinds+operation+table+(storageState.error?`<div class="storage-error" role="alert">${escapeHTML(storageState.error)}</div>`:'');
}
function renderStorageOperationFacts(job){
  const r=job?.result;if(!r||!['completed','failed','cancelled'].includes(job.status))return '';
  if(job.action==='plan')return '';
  const facts=[],action=r.action||job.action;
  const fields={quarantine:['movedEntries','已隔离条目','movedLogicalBytes','已隔离逻辑长度（未释放）'],restore:['restoredEntries','已恢复条目','restoredLogicalBytes','已恢复文件逻辑长度'],purge:['purgedEntries','已永久删除条目','purgedLogicalBytes','已删除文件逻辑长度']}[action];
  if(fields){
    const [countKey,countLabel,byteKey,byteLabel]=fields;
    if(Number.isFinite(r[countKey]))facts.push(`${countLabel} ${storageCount(r[countKey])}`);
    const bytes=Number.isFinite(r[byteKey])?r[byteKey]:r.logicalBytes;
    if(Number.isFinite(bytes))facts.push(`${byteLabel} ${storageBytes(bytes)}`);
  }
  for(const [field,label]of [['skippedEntries','跳过条目'],['failedEntries','失败条目']])if(Number.isFinite(r[field])&&r[field]>0)facts.push(`${label} ${storageCount(r[field])}`);
  const diagnostics=Array.isArray(r.diagnostics)?r.diagnostics:[];
  return (facts.length?`<p class="storage-operation-facts">${facts.map(escapeHTML).join(' · ')}</p>`:'')+(diagnostics.length?`<details class="storage-diagnostics"><summary>${diagnostics.length} 条操作诊断${diagnostics.length>20?' · 显示前 20 条':''}</summary><ul>${diagnostics.slice(0,20).map(d=>`<li>${escapeHTML(storageReason(d))}</li>`).join('')}</ul></details>`:'');
}
function renderStorageEntryTable(scan){
  const page=storageState.entries,items=page.items||[];
  return `<section class="panel storage-entry-panel"><div class="panel-heading"><h3>${icon('layers')}扫描条目</h3><select id="storage-kind-filter" aria-label="筛选存储类型"><option value="">全部类型</option>${Object.entries(storageKinds).map(([id,label])=>`<option value="${id}" ${storageState.kind===id?'selected':''}>${label}</option>`).join('')}</select></div><div class="storage-table-scroll"><table><thead><tr><th>缓存条目</th><th>逻辑长度</th><th>保护状态 / 操作</th></tr></thead><tbody>${items.length?items.map(item=>`<tr><td><span class="storage-entry-kind">${escapeHTML(storageKinds[item.kind]||item.kind)}</span><code>${escapeHTML(storageEntryName(item))}</code>${item.modifiedAt?`<small>${escapeHTML(storageTime(item.modifiedAt))}</small>`:''}</td><td class="storage-byte-cell">${storageBytes(item.logicalBytes)}</td><td><span class="storage-protection ${item.protected?'protected':''}">${item.protected?'受保护':'等待计划筛选'}</span>${item.protectionReasons?.length?`<p class="storage-protection-reason">${escapeHTML(storageReason(item.protectionReasons))}</p>`:''}${item.kind==='quarantine'&&item.quarantineId?`<button class="text-button" data-storage-restore="${escapeHTML(item.quarantineId)}" ${!storageCapability('restore')||storageRunning()||!storageScanUsable()?'disabled':''}>预览恢复 ↗</button>`:''}</td></tr>`).join(''):`<tr><td colspan="3" class="storage-table-empty">${scan?'当前筛选没有条目':'完成扫描后查看条目'}</td></tr>`}</tbody></table></div><div class="panel-footer"><span>${storageCount(page.total)} 项 · 每页 50 项${scan?.truncated?' · 当前不完整快照':''}</span><div class="task-actions"><button class="text-button" id="storage-prev" ${storageState.offset===0?'disabled':''}>← 上一页</button><button class="text-button" id="storage-next" ${!page.nextOffset&&storageState.offset+50>=Number(page.total||0)?'disabled':''}>下一页 →</button></div></div></section>`;
}
function updateStorageControls(){
  const running=storageRunning(),cap=storageState.snapshot?.capabilities||{},action=storageState.settings.action;
  const scan=$('#storage-scan-submit'),plan=$('#storage-plan-submit'),purge=$('#storage-plan-action option[value="purge"]');
  if(scan)scan.disabled=running||cap.scan!==true;if(purge)purge.disabled=cap.purge!==true;
  if(plan)plan.disabled=running||!storageScanUsable()||cap[action]!==true;
  const quarantine=$('#storage-quarantine-policy'),permanent=$('#storage-purge-policy');if(quarantine)quarantine.hidden=action==='purge';if(permanent)permanent.hidden=action!=='purge';
  const gate=$('#storage-plan-gate');if(gate)gate.textContent=running?'等待当前操作结束后生成下一份预览。':!storageScanUsable()?'需要一份完整、保护状态可确认的扫描记录。':action==='purge'&&cap.purge!==true?'管理员尚未启用永久删除；隔离内容继续保留。':'预览列出具体候选，生成预览本身不移动或删除文件。';
}
async function fetchStorageEntries(){
  const scan=storageState.snapshot?.state?.latestScan;if(!scan?.id){storageState.entries={items:[],total:0};return;}
  const query=new URLSearchParams({scanId:scan.id,limit:'50',offset:String(storageState.offset)});if(storageState.kind)query.set('kind',storageState.kind);
  storageState.entries=await api('/api/storage/entries?'+query);
}
function paintStorage(){
  if(state.view!=='storage')return;
  const live=$('#storage-live');if(!live)return;
  const active=document.activeElement,focusId=live.contains(active)?active.id:null,tableScroll=live.querySelector('.storage-table-scroll')?.scrollLeft||0;
  const diagnosticsOpen=live.querySelector('.storage-diagnostics')?.open;
  live.innerHTML=renderStorageLive();hydrateIcons(live);bindStorageLiveEvents();
  const table=live.querySelector('.storage-table-scroll');if(table)table.scrollLeft=tableScroll;
  if(diagnosticsOpen&&live.querySelector('.storage-diagnostics'))live.querySelector('.storage-diagnostics').open=true;
  if(focusId)document.getElementById(focusId)?.focus({preventScroll:true});updateStorageControls();
}
function stopStorageRefresh(){clearTimeout(storageState.timer);storageState.timer=null;storageState.planRequest++;storageState.planLoading=false;$('#storage-plan-dialog')?.close();}
async function refreshStorage(){
  clearTimeout(storageState.timer);if(storageState.loading)return;
  storageState.loading=true;
  try{
    const oldScan=storageState.snapshot?.state?.latestScan?.id;storageState.snapshot=await api('/api/storage');
    if(oldScan!==storageState.snapshot.state?.latestScan?.id)storageState.offset=0;
    await fetchStorageEntries();storageState.error='';
    const job=storageState.snapshot.job;
    if(job?.id===storageState.awaitedJobId&&job.status!=='running'&&storageState.handledJobId!==job.id){
      storageState.handledJobId=job.id;
      if(job.status==='failed')storageState.error=job.error||'后台操作失败';
      if(job.status==='completed'&&job.action==='plan'&&job.result?.id){storageState.plan=job.result;storageState.planOffset=0;if(state.view==='storage')await openStoragePlan();}
    }
  }catch(error){storageState.error=error.message;}
  finally{storageState.loading=false;paintStorage();if(state.view==='storage'&&(storageState.snapshot?.job?.status==='running'||storageState.snapshot?.state?.activeOperation))storageState.timer=setTimeout(refreshStorage,1500);}
}
async function startStorageJob(endpoint,payload){
  if(storageRunning())return;storageState.busy=true;storageState.error='';let postError='';updateStorageControls();
  try{const job=await api('/api/storage/'+endpoint,{method:'POST',body:JSON.stringify(payload)});storageState.awaitedJobId=job.id;storageState.handledJobId=null;if(storageState.snapshot)storageState.snapshot.job=job;}
  catch(error){postError=error.message;storageState.error=postError;}
  finally{storageState.busy=false;paintStorage();await refreshStorage();if(postError){storageState.error=postError;paintStorage();}}
}
function storagePolicy(action,extra={}){
  const scan=storageState.snapshot?.state?.latestScan;if(!storageScanUsable())throw new Error('请先完成可用于清理计划的完整扫描');
  const minAge=Number(storageState.settings.minAgeDays),retention=Number(storageState.settings.retentionDays);
  if(!Number.isFinite(minAge)||minAge<0||!Number.isFinite(retention)||retention<0)throw new Error('保留时间必须是非负天数');
  const kinds=action==='quarantine'?['whole','leaf','partial'].filter(kind=>storageState.settings[kind]):['quarantine'];
  if(!kinds.length)throw new Error('至少选择一种缓存类型');
  return {action,scanId:scan.id,minAgeSeconds:Math.round(minAge*86400),retentionSeconds:Math.round(retention*86400),kinds,...extra};
}
function bindStorageEvents(){
  const fields={'storage-max-entries':'maxEntries','storage-max-seconds':'maxSeconds','storage-min-age':'minAgeDays','storage-retention':'retentionDays'};
  for(const [id,key]of Object.entries(fields))$('#'+id)?.addEventListener('input',e=>{storageState.settings[key]=e.target.value;});
  for(const kind of ['whole','leaf','partial'])$('#storage-kind-'+kind)?.addEventListener('change',e=>{storageState.settings[kind]=e.target.checked;});
  $('#storage-plan-action')?.addEventListener('change',e=>{storageState.settings.action=e.target.value;updateStorageControls();});
  $('#storage-scan-form')?.addEventListener('submit',e=>{e.preventDefault();if(!storageCapability('scan'))return;startStorageJob('scan',{maxEntries:Number(storageState.settings.maxEntries),maxSeconds:Number(storageState.settings.maxSeconds)});});
  $('#storage-plan-form')?.addEventListener('submit',e=>{e.preventDefault();try{const action=storageState.settings.action;if(!storageCapability(action))throw new Error('该操作未由管理员启用');startStorageJob('plan',storagePolicy(action));}catch(error){storageState.error=error.message;paintStorage();}});
  bindStorageLiveEvents();updateStorageControls();refreshStorage();
}
function bindStorageLiveEvents(){
  $('#storage-refresh')?.addEventListener('click',refreshStorage);
  $('#storage-reopen-plan')?.addEventListener('click',()=>{storageState.plan=storageState.snapshot.job.result;storageState.planOffset=0;openStoragePlan();});
  $('#storage-kind-filter')?.addEventListener('change',e=>{storageState.kind=e.target.value;storageState.offset=0;refreshStorage();});
  document.querySelectorAll('[data-storage-kind]').forEach(button=>button.addEventListener('click',()=>{storageState.kind=storageState.kind===button.dataset.storageKind?'':button.dataset.storageKind;storageState.offset=0;refreshStorage();}));
  $('#storage-prev')?.addEventListener('click',()=>{storageState.offset=Math.max(0,storageState.offset-50);refreshStorage();});
  $('#storage-next')?.addEventListener('click',()=>{storageState.offset=storageState.entries.nextOffset??storageState.offset+50;refreshStorage();});
  $('#storage-cancel')?.addEventListener('click',async()=>{try{await api('/api/storage/cancel',{method:'POST',body:JSON.stringify({jobId:storageState.snapshot.job.id})});await refreshStorage();}catch(error){storageState.error=error.message;paintStorage();}});
  document.querySelectorAll('[data-storage-restore]').forEach(button=>button.addEventListener('click',()=>{try{startStorageJob('plan',storagePolicy('restore',{quarantineId:button.dataset.storageRestore}));}catch(error){storageState.error=error.message;paintStorage();}}));
}
function renderStoragePlan(){
  const plan=storageState.plan,page=storageState.planPage,purge=plan.action==='purge',running=storageRunning(),allowed=storageCapability(plan.action)&&!running&&Number(plan.count)>0&&!['applied','cancelled','failed','stale'].includes(plan.status);
  const explanation=plan.action==='quarantine'?'将这些缓存条目移入隔离区，保留恢复机会。文件仍在同一磁盘上，本步骤不释放空间。':plan.action==='restore'?'将选定隔离条目恢复到原缓存位置。若原位置已被占用，服务端会拒绝覆盖。':'永久删除隔离区中的这些条目，无法通过本页面恢复。下方大小是文件逻辑长度，实际磁盘空闲变化需看后续磁盘状态。';
  return `<header class="storage-plan-header"><div><span>REVIEW THE EXACT PLAN</span><h2 id="storage-plan-title">${escapeHTML(storageActions[plan.action]||plan.action)}预览</h2><p>${escapeHTML(plan.id)}</p></div><button class="icon-button" id="storage-plan-close" aria-label="关闭清理预览">${icon('x')}</button></header><div class="storage-plan-body"><div class="storage-plan-stats"><div><span>计划条目</span><strong>${storageCount(plan.count)}</strong></div><div><span>候选逻辑长度</span><strong>${storageBytes(plan.logicalBytes)}</strong></div><div><span>${plan.action==='purge'?'预计删除逻辑长度':'本步骤空间回收'}</span><strong>${plan.action==='purge'?storageBytes(plan.reclaimableBytes):'0 B'}</strong></div></div><p class="storage-plan-explanation">${explanation}</p><p class="storage-plan-reference">扫描 ${escapeHTML(plan.scanId||'未提供')} · ${escapeHTML(plan.status||'已生成')} · 执行时重新检查指纹和保护引用</p><div class="storage-table-scroll"><table><thead><tr><th>具体候选</th><th>逻辑长度</th></tr></thead><tbody>${(page.items||[]).map(item=>`<tr><td><span class="storage-entry-kind">${escapeHTML(storageKinds[item.kind]||item.kind||'缓存条目')}</span><code>${escapeHTML(storageEntryName(item))}</code>${item.protectionReasons?.length?`<p>${escapeHTML(storageReason(item.protectionReasons))}</p>`:''}</td><td class="storage-byte-cell">${storageBytes(item.logicalBytes)}</td></tr>`).join('')||'<tr><td colspan="2" class="storage-table-empty">没有可执行候选；受保护或未到期条目不会纳入。</td></tr>'}</tbody></table></div><div class="storage-plan-pagination"><span>第 ${Math.floor(storageState.planOffset/50)+1} / ${Math.max(1,Math.ceil(Number(page.total??plan.count)/50))} 页 · 每页 50 项</span><button class="text-button" id="storage-plan-prev" ${storageState.planOffset===0?'disabled':''}>← 上一页</button><button class="text-button" id="storage-plan-next" ${storageState.planOffset+50>=Number(page.total??plan.count)?'disabled':''}>下一页 →</button></div>${allowed?purge?`<label class="storage-purge-confirm" for="storage-purge-confirm">输入“永久删除 ${storageCount(plan.count)}”以确认这份具体计划<input id="storage-purge-confirm" autocomplete="off" spellcheck="false" placeholder="永久删除 ${storageCount(plan.count)}"></label>`:`<label class="storage-apply-confirm"><input id="storage-apply-confirm" type="checkbox">我已查看这份 ${storageCount(plan.count)} 项的${plan.action==='restore'?'恢复':'隔离'}计划</label>`:'<p class="storage-plan-gate">当前计划无可执行候选，或操作能力尚未启用 / 已执行。</p>'}<p id="storage-plan-error" class="storage-error" role="alert"></p></div><footer class="storage-plan-footer"><span>${plan.action==='quarantine'?'隔离可以恢复 · 尚未释放磁盘':plan.action==='purge'?'永久删除仅作用于这份已确认计划':'不覆盖已存在的缓存条目'}</span><button class="button ${purge?'button-danger':'button-primary'}" id="storage-plan-apply" disabled ${!allowed?'data-unavailable="true"':''}>${escapeHTML(storageActions[plan.action]||plan.action)} ${storageCount(plan.count)} 项</button></footer>`;
}
function updateStoragePlanApply(){
  const button=$('#storage-plan-apply');if(!button||button.dataset.unavailable)return;
  button.disabled=storageState.planLoading||storageRunning()||!storageCapability(storageState.plan?.action)||(storageState.plan?.action==='purge'?$('#storage-purge-confirm')?.value!==`永久删除 ${storageCount(storageState.plan.count)}`:!$('#storage-apply-confirm')?.checked);
}
function updateStoragePlanLoading(offset=storageState.planOffset){
  const loading=storageState.planLoading,dialog=$('#storage-plan-dialog'),page=storageState.planPage;
  dialog?.setAttribute('aria-busy',String(loading));
  const previous=$('#storage-plan-prev'),next=$('#storage-plan-next');
  if(previous)previous.disabled=loading||storageState.planOffset===0;
  if(next)next.disabled=loading||storageState.planOffset+50>=Number(page.total??storageState.plan?.count??0);
  for(const id of ['storage-apply-confirm','storage-purge-confirm']){const field=$('#'+id);if(field)field.disabled=loading;}
  let status=$('#storage-plan-loading');
  if(dialog&&!status){status=document.createElement('p');status.id='storage-plan-loading';status.className='storage-plan-loading';status.setAttribute('role','status');dialog.querySelector('.storage-plan-pagination')?.before(status);}
  if(status)status.textContent=loading?`正在加载第 ${Math.floor(offset/50)+1} 页，请稍候…`:'';
  const reopen=$('#storage-reopen-plan');if(reopen){reopen.disabled=loading;reopen.setAttribute('aria-busy',String(loading));}
  updateStoragePlanApply();
}
async function loadStoragePlanPage(offset=storageState.planOffset){
  const plan=storageState.plan;if(!plan||storageState.planLoading)return false;
  const request=++storageState.planRequest;storageState.planLoading=true;updateStoragePlanLoading(offset);
  try{
    const data=await api(`/api/storage/plans/${encodeURIComponent(plan.id)}?limit=50&offset=${offset}`);
    if(request!==storageState.planRequest||state.view!=='storage'||storageState.plan?.id!==plan.id)return false;
    // Commit the offset and rows together. Failed requests keep the old page.
    storageState.plan=data.plan||plan;storageState.planPage=data;storageState.planOffset=offset;return true;
  }catch(error){if(request!==storageState.planRequest||state.view!=='storage')return false;throw error;}
  finally{if(request===storageState.planRequest){storageState.planLoading=false;updateStoragePlanLoading();}}
}
async function openStoragePlan(){
  if(storageState.planLoading)return;
  storageState.returnFocus=document.activeElement;
  try{
    if(!await loadStoragePlanPage())return;let dialog=$('#storage-plan-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='storage-plan-dialog';dialog.className='storage-plan-dialog';dialog.setAttribute('aria-labelledby','storage-plan-title');document.body.appendChild(dialog);dialog.addEventListener('close',()=>{storageState.planRequest++;storageState.planLoading=false;updateStoragePlanLoading();if(state.view==='storage'){if(storageState.returnFocus?.isConnected)storageState.returnFocus.focus({preventScroll:true});else $('#storage-plan-submit')?.focus({preventScroll:true});}});}
    dialog.innerHTML=renderStoragePlan();hydrateIcons(dialog);bindStoragePlanEvents();if(!dialog.open)dialog.showModal();$('#storage-plan-close').focus({preventScroll:true});
  }catch(error){if(state.view==='storage'){storageState.error=error.message;paintStorage();}}
}
function bindStoragePlanEvents(){
  $('#storage-plan-close')?.addEventListener('click',()=>$('#storage-plan-dialog').close());
  $('#storage-purge-confirm')?.addEventListener('input',updateStoragePlanApply);$('#storage-apply-confirm')?.addEventListener('change',updateStoragePlanApply);
  const page=async delta=>{
    if(storageState.planLoading)return;
    const requestPage=Math.max(0,storageState.planOffset+delta),dialog=$('#storage-plan-dialog');
    const errorBox=$('#storage-plan-error');if(errorBox)errorBox.textContent='';
    try{
      if(!await loadStoragePlanPage(requestPage)||!dialog?.open)return;
      dialog.innerHTML=renderStoragePlan();hydrateIcons(dialog);bindStoragePlanEvents();
      const primary=$('#storage-plan-'+(delta>0?'next':'prev')),fallback=$('#storage-plan-'+(delta>0?'prev':'next'));
      (primary&&!primary.disabled?primary:fallback&&!fallback.disabled?fallback:$('#storage-plan-close'))?.focus();
    }catch(error){if(dialog?.open&&state.view==='storage'){$('#storage-plan-error').textContent=`加载失败：${error.message}。仍显示第 ${Math.floor(storageState.planOffset/50)+1} 页，可再次点击翻页重试。`;}}
  };
  $('#storage-plan-prev')?.addEventListener('click',()=>page(-50));$('#storage-plan-next')?.addEventListener('click',()=>page(50));
  $('#storage-plan-apply')?.addEventListener('click',async()=>{const button=$('#storage-plan-apply');if(button.disabled||storageRunning()||!storageCapability(storageState.plan.action))return;button.disabled=true;const {id,digest}=storageState.plan;$('#storage-plan-dialog').close();await startStorageJob('apply',{planId:id,digest,confirm:true});});
}
