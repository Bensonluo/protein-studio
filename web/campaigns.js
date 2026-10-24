'use strict';

const batchState={campaigns:[],databases:[],config:{},selectedId:localStorage.getItem('foldspace.campaign'),campaign:null,page:{items:[],total:0},offset:0,stage:'',sort:'seq',minIptm:null,loading:false,refreshQueued:false,requestView:null,publishedView:null,loaded:false,error:'',cluster:null,importing:false,progress:null};
const batchOperation={pending:null,error:null,confirmation:null,notice:null};
const batchInspection={campaign:null,item:null,itemId:null,generation:0,returnFocus:null};
const batchInput={mode:'independent',fixedText:'',preview:null,previewGeneration:0,previewTimer:null};
const pipelineStages=[['waiting_input','待准备','file'],['preparing','搜索 / 准备','search'],['ready','准备数据就绪','layers'],['predicting','GPU 预测','cpu'],['validating','结果验证','chart'],['completed','已交付','check']];
const batchStageNames=Object.fromEntries([...pipelineStages.map(s=>s.slice(0,2)),['failed','失败'],['cancelled','已取消']]);
const formatCount=value=>Number(value||0).toLocaleString('zh-CN');
function batchEngineCapabilities(){const value=batchState.config.capabilities||[];return Array.isArray(value)?value:Object.entries(value).map(([id,item])=>typeof item==='object'?{id,...item}:{id,available:!!item});}
function batchEngineAvailable(item){
  const global=item.available??item.configured??item.supported??item.staged??false,engine=item.id||item.engine;
  return !!global||batchState.databases.some(profile=>profile.engine===engine&&profile.ready&&profile.campaignCapability?.available===true);
}

function campaignViewKey(){return JSON.stringify([batchState.selectedId,batchState.stage,batchState.offset,batchState.sort,batchState.minIptm]);}
function campaignControlBlocked(){return !!batchOperation.pending||!!batchOperation.confirmation||batchState.campaign?.id!==batchState.selectedId;}
function setCampaignRefreshBusy(show){
  const monitor=$('#campaign-monitor');if(!monitor)return;
  monitor.setAttribute('aria-busy',String(batchState.loading));
  const status=monitor.querySelector('.campaign-refresh-state');if(status){status.hidden=!show;status.textContent=batchState.campaign?.id!==batchState.selectedId?'正在读取所选批次…':'正在更新目标列表…';}
  monitor.querySelectorAll('[data-campaign-action]').forEach(button=>{button.disabled=campaignControlBlocked();});
}
function renderCampaignMonitorRegion(){
  const monitor=$('#campaign-monitor');if(!monitor)return;
  const active=document.activeElement,focusId=monitor.contains(active)?active.id:null;
  const focusTarget=monitor.contains(active)?active.dataset?.inspectTarget:null;
  const focusAttribute=monitor.contains(active)?['data-stage-filter','data-score-filter','data-campaign-export','data-campaign-action'].find(key=>active.hasAttribute(key)):null;
  const focusValue=focusAttribute?active.getAttribute(focusAttribute):null;
  const focusInChips=!!active.closest?.('.campaign-filter-chips');
  const tableScroll=monitor.querySelector('.table-scroll')?.scrollLeft||0;
  monitor.innerHTML=renderCampaignMonitor();monitor.setAttribute('aria-busy',String(batchState.loading));hydrateIcons(monitor);bindCampaignMonitorEvents();
  const table=monitor.querySelector('.table-scroll');if(table)table.scrollLeft=tableScroll;
  const restore=focusId?document.getElementById(focusId):focusTarget?[...monitor.querySelectorAll('[data-inspect-target]')].find(el=>el.dataset.inspectTarget===focusTarget):focusAttribute?[...monitor.querySelectorAll(`[${focusAttribute}]`)].find(el=>el.getAttribute(focusAttribute)===focusValue&&!!el.closest('.campaign-filter-chips')===focusInChips):null;
  restore?.focus({preventScroll:true});
}
async function refreshCampaignData(force=false){
  if(batchState.loading){if(force||campaignViewKey()!==batchState.requestView)batchState.refreshQueued=true;setCampaignRefreshBusy(batchState.refreshQueued);return;}
  batchState.loading=true;batchState.requestView=campaignViewKey();setCampaignRefreshBusy(force||batchState.requestView!==batchState.publishedView);
  let requestedView=batchState.requestView;
  try{
    const [campaigns,databases,config,cluster]=await Promise.all([api('/api/campaigns'),api('/api/databases'),api('/api/pipeline/config'),api('/api/cluster').catch(()=>null)]);batchState.cluster=cluster;
    batchState.campaigns=Array.isArray(campaigns)?campaigns:campaigns.campaigns||[];
    batchState.databases=Array.isArray(databases)?databases:databases.profiles||[];batchState.config=config;
    if(!batchState.selectedId&&batchState.campaigns.length)batchState.selectedId=batchState.campaigns[0].id;
    requestedView=batchState.requestView=campaignViewKey();
    if(batchState.selectedId){
      const id=batchState.selectedId;
      const [campaign,page]=await Promise.all([api(`/api/campaigns/${encodeURIComponent(id)}`),api(`/api/campaigns/${encodeURIComponent(id)}/items?limit=25&offset=${batchState.offset}${batchState.stage?'&stage='+encodeURIComponent(batchState.stage):''}${batchState.sort&&batchState.sort!=='seq'?'&sort='+encodeURIComponent(batchState.sort):''}${batchState.minIptm!=null?'&min_iptm='+batchState.minIptm:''}`)]);
      if(requestedView===campaignViewKey()){batchState.campaign=campaign;batchState.page=page;batchState.publishedView=requestedView;}
      else batchState.refreshQueued=true;
    }
    batchState.loaded=true;batchState.error='';
  }catch(error){if(requestedView===campaignViewKey())batchState.error=error.message;else batchState.refreshQueued=true;}
  finally{batchState.loading=false;}
  if(batchState.refreshQueued){batchState.refreshQueued=false;return refreshCampaignData();}
  if(state.view==='databases')renderMain();
  if(state.view==='campaigns'){
    // Update the monitor alone so ongoing uploads keep their File and form values.
    if($('#campaign-monitor'))renderCampaignMonitorRegion();else renderMain();
    updateCampaignEngineOptions();
  }
}

function renderCampaigns(){
  return `<section class="batch-intro"><div><div class="eyebrow">DURABLE CAMPAIGN PIPELINE</div><h2>一份清单，多个阶段，持续交付。</h2><p>输入按小块写入任务账本。输入准备、GPU 预测与结果验证分别消费队列，完成一项就交付一项。</p></div><span class="batch-emblem">${icon('workflow')}</span></section>
  <div class="batch-layout"><aside class="panel batch-ingest"><div class="panel-heading"><h3>${icon('upload')}导入预测批次</h3><span class="subtle">STREAMING</span></div><form id="campaign-form" class="batch-form">
    <label class="field-label" for="campaign-name">批次名称</label><input id="campaign-name" required maxlength="120" value="蛋白质结构筛选批次">
    <label class="field-label" for="campaign-engine">执行引擎</label><select id="campaign-engine"><option value="reference">公开参考回放</option></select><p id="campaign-engine-note" class="batch-helper">读取当前引擎能力…</p>
    <label class="field-label" for="campaign-database">数据库配置</label><select id="campaign-database"><option value="">参考回放无需数据库</option></select><div id="campaign-search-strategy" class="campaign-search-strategy"></div>
    <label class="field-label" for="campaign-placement">执行位置</label><select id="campaign-placement"><option value="hybrid">本地 + 阿里云共同处理</option><option value="local">仅本地节点</option><option value="aliyun">仅阿里云节点</option></select>
    <label class="field-label" for="campaign-input-mode">输入构造</label><select id="campaign-input-mode"><option value="independent" ${batchInput.mode==='independent'?'selected':''}>独立目标 · FASTA / JSONL</option><option value="fixed-target" ${batchInput.mode==='fixed-target'?'selected':''}>固定靶标筛选 · 靶标 + 每条候选</option></select>
    <div id="campaign-fixed-fields" class="campaign-fixed-fields" ${batchInput.mode==='fixed-target'?'':'hidden'}><label class="field-label" for="campaign-fixed-target">固定靶标 FASTA</label><textarea id="campaign-fixed-target" rows="7" spellcheck="false" placeholder=">target_chain_1&#10;ACDEFGHIKLMNPQRSTVWY">${escapeHTML(batchInput.fixedText)}</textarea><p class="batch-helper">可包含 1–25 条固定链，依次编号 A、B…；每条候选使用下一个链 ID。使用标准 20 种氨基酸；<span id="campaign-fixed-limit">${campaignResidueLimitLabel()}</span></p></div>
    <label class="field-label" for="campaign-file" id="campaign-file-label">${batchInput.mode==='fixed-target'?'候选 FASTA 文件':'多记录 FASTA / JSONL 文件'}</label><input type="file" id="campaign-file" accept="${batchInput.mode==='fixed-target'?'.fa,.fasta,.faa,.fas':'.fa,.fasta,.faa,.fas,.jsonl,.ndjson'}" required><p class="batch-helper" id="campaign-input-help">${campaignInputHelp()}</p><div id="campaign-input-preview" class="campaign-input-preview" aria-live="polite">${renderCampaignInputPreview()}</div>
    <div class="parameter-grid"><div><label class="field-label" for="campaign-seeds">随机种子</label><input id="campaign-seeds" value="1" required></div><div><label class="field-label" for="campaign-samples">每种子采样数</label><input id="campaign-samples" type="number" value="1" min="1" max="20"></div></div>
    <label class="batch-check"><input type="checkbox" id="campaign-stream">边导入，边执行</label><label class="batch-check"><input type="checkbox" id="campaign-append">继续导入当前未封存批次</label><p class="batch-helper">默认完整导入、封存清单后启动。中断后保留已接收记录，重新选择同一文件继续导入可幂等去重。</p>
    <div id="campaign-import-progress" class="batch-import-progress" aria-live="polite">${renderImportProgress()}</div><p id="campaign-form-error" class="form-error" role="alert"></p><button class="button button-primary" type="submit" id="campaign-submit" ${batchState.importing?'disabled':''}>${icon('play')}导入并启动管线</button>
  </form></aside><div id="campaign-monitor" class="batch-monitor">${renderCampaignMonitor()}</div></div>`;
}

function renderImportProgress(){const p=batchState.progress;return p?`<strong>${formatCount(p.accepted)} 项已确认接收</strong><span>${formatCount(p.read)} 条已读取 · ${formatCount(p.duplicates)} 项已存在</span><small>${escapeHTML(p.message||'正在逐块写入任务账本…')}</small>`:'';}

function campaignDiagnostic(c,stage=''){
  const counts=c.counts||{},workerMode=batchState.config.predictionMode==='workers';
  const messages={
    waiting_input:['输入已持久保存',workerMode?'等待兼容的在线节点与 CPU 准备额度；检查引擎、位置、采样与运行版本。暂无匹配节点时保持待准备，已有缓存继续保留。':'等待 CPU 准备槽位。检查批次是否暂停，以及准备中 + 就绪数量是否触及全局高水位。'],
    preparing:['正在生成模型输入','查看搜索库预检、CPU / 内存与磁盘读取；已完成的准备缓存可供相同输入复用。'],
    ready:['准备完成，等待预测','核对引擎、采样设置、执行位置与节点输入长度限制；队列就绪不代表已经占用 GPU。'],
    predicting:['预测阶段执行中',workerMode?'在混合云页面检查节点心跳、当前批次和回传记录；同一常驻进程连续处理目标。':'由本地预测槽执行运行器；查看目标事件与运行器输出，区分启动、编译和模型计算。'],
    validating:['结果正在交付前检查','结果已进入独立验证队列；检查结构序列、产物路径与发布错误。'],
    completed:['结构已验证并发布','可打开结构视图，或导出包含结构链接的 JSONL 清单。'],
    failed:['从失败阶段定位问题','先打开目标详情，查看 failedStage、原始错误与最近事件；修复配置或资源问题后再重试。'],
    cancelled:['剩余执行已取消','保留已交付结果和任务记录；取消后的批次不再恢复执行。']
  };
  if(stage)return messages[stage]||messages.waiting_input;
  if(c.status==='paused')return ['批次已暂停取新任务','输入与阶段记录会保留。准备好数据库和执行资源后，点击“继续执行”；已领取的工作可能仍在收尾。'];
  if(counts.failed)return [`${formatCount(counts.failed)} 项需要检查`, '按“失败项”筛选并展开目标详情，直接定位错误发生在哪个阶段。已成功交付的结构仍然可用。'];
  if(workerMode&&counts.ready){
    const eligible=(batchState.cluster?.nodes||[]).filter(n=>n.status==='online'&&n.engine===c.engine&&((c.placement||'hybrid')==='hybrid'||n.location===c.placement));
    if(!eligible.length)return ['就绪队列等待匹配节点','当前心跳窗口没有匹配引擎与执行位置的在线节点。打开混合云执行页检查节点注册与模型配置。'];
  }
  if(c.isComplete)return ['本批次执行已结束','通过下方目标列表检查交付情况；清单导出保留状态、输入指纹和结构下载链接。'];
  return ['从积压最多的阶段开始排查','点击阶段卡片查看对应目标。阶段数量来自任务账本；交付率以当前已入库目标为分母。'];
}
function campaignExportUrl(c,stage='',scoreQuery=null){
  const params=new URLSearchParams();
  if(stage)params.set('stage',stage);
  for(const [key,value] of Object.entries(scoreQuery||{}))if(value!==null&&value!==undefined&&value!=='')params.set(key,value);
  const qs=params.toString();
  return appUrl(`/api/campaigns/${encodeURIComponent(c.id)}/export${qs?'?'+qs:''}`);
}
function campaignScoreBand(iptm){return iptm==null?null:iptm>=0.8?'high':iptm>=0.6?'gray':'low';}
function renderCampaignInspectScores(item){
  const scores=item.resultScores;
  if(!scores)return '';
  const iptmBand=campaignScoreBand(scores.iptm);
  const iptmLabel=iptmBand==='high'?'高置信 ≥0.8':iptmBand==='gray'?'灰区 0.6–0.8':'<0.6 可能失败';
  const plddt=scores.meanPlddt==null?null:scores.meanPlddt>=90?['high','≥90 很高']:scores.meanPlddt>=70?['gray','70–90 置信']:scores.meanPlddt>=50?['low','50–70 低']:['low','<50 很低'];
  const rows=[];
  if(scores.rankingScore!=null)rows.push(['排名分 ranking score',`<strong class="campaign-score-value">${formatScore(scores.rankingScore)}</strong>`,'AF3 官方排名口径，越高越好，可因惩罚项超出 0–1']);
  if(scores.iptm!=null)rows.push(['ipTM',`${formatScore(scores.iptm)}${iptmBand?` <span class="campaign-score-band ${iptmBand}">${iptmLabel}</span>`:''}`,'预测界面 TM 分数 0–1；EBI 阈值 ≥0.8 高置信、0.6–0.8 灰区']);
  if(scores.ptm!=null)rows.push(['pTM',formatScore(scores.ptm),'预测拓扑匹配分数 0–1']);
  if(scores.meanPlddt!=null)rows.push(['平均 pLDDT',`${formatScore(scores.meanPlddt)} <span class="campaign-score-band ${plddt[0]}">${plddt[1]}</span>`,'残基级置信均值 0–100；AF DB 四带 ≥90/70–90/50–70/<50']);
  return `<section class="campaign-inspect-section campaign-inspect-scores" data-score-section="true"><div class="campaign-inspect-section-title"><h3>置信度评分</h3><span>已发布结果的账本分数</span></div><dl class="campaign-inspect-facts">${rows.map(([label,value,title])=>`<div><dt title="${title}">${label}</dt><dd>${value}</dd></div>`).join('')}</dl>${scores.rankingMetric?`<p class="campaign-inspect-hint">排名口径：${escapeHTML(scores.rankingMetric)}</p>`:''}<p class="campaign-inspect-hint">ipTM 分带按 EBI 界面置信阈值；平均 pLDDT 按 AlphaFold DB 四带。分数为预测置信的元数据，不代表实验测定精度。</p></section>`;
}
function campaignBandRow(metric,ariaText,entries){
  const segs=entries.filter(entry=>entry.count>0);
  if(!segs.length)return '';
  const bar=segs.map(entry=>`<span class="campaign-band-seg ${entry.cls}" style="flex-grow:${entry.count}" title="${entry.title} × ${formatCount(entry.count)}"></span>`).join('');
  return `<div class="campaign-band-row"><span class="campaign-band-metric">${metric}</span><div class="campaign-band-bar" role="img" aria-label="${ariaText}：${segs.map(entry=>entry.label+' '+formatCount(entry.count)).join('、')}">${bar}</div></div>`;
}
function renderCampaignBandSummary(c){
  const bands=c.metrics?.scoreBands;
  if(!bands||!(Number(c.metrics?.scoredTargets)>0))return '';
  const iptm=[{cls:'s1',label:'ipTM 高 ≥0.8',count:bands.iptm.high,title:'ipTM ≥ 0.8（EBI 高置信）'},
    {cls:'s2',label:'灰区 0.6–0.8',count:bands.iptm.gray,title:'EBI 界面置信灰区'},
    {cls:'s3',label:'低 <0.6',count:bands.iptm.low,title:'ipTM < 0.6 可能失败（EBI）'}];
  const plddt=[{cls:'s1',label:'pLDDT ≥90',count:bands.plddt.veryHigh,title:'平均 pLDDT ≥ 90（AF DB 很高）'},
    {cls:'s2',label:'70–90',count:bands.plddt.confident,title:'平均 pLDDT 70 – 90（AF DB 置信）'},
    {cls:'s3',label:'50–70',count:bands.plddt.low,title:'平均 pLDDT 50 – 70（AF DB 低）'},
    {cls:'s4',label:'<50',count:bands.plddt.veryLow,title:'平均 pLDDT < 50（AF DB 很低，常为无序）'}];
  const legend=entries=>entries.some(entry=>entry.count>0)?`<div class="campaign-band-legend">${entries.filter(entry=>entry.count>0).map(entry=>`<span class="campaign-score-band" title="${entry.title} × ${formatCount(entry.count)}"><i class="campaign-band-swatch ${entry.cls}"></i>${entry.label} × ${formatCount(entry.count)}</span>`).join('')}</div>`:'';
  const rows=campaignBandRow('ipTM','ipTM 界面置信分布',iptm)+campaignBandRow('pLDDT','平均 pLDDT 置信分布',plddt);
  if(!rows)return '';
  return `<div class="campaign-score-bands" data-band-summary="true"><span class="campaign-bands-label" title="按账本已评分交付项统计；历史未浏览项在首次列出或导出后计入">置信度分布</span>${rows}${legend(iptm)}${legend(plddt)}</div>`;
}
function formatScore(value){return value==null?'—':value>1?value.toFixed(1):value.toFixed(3);}
function renderCampaignScoreCell(item){
  const scores=item.resultScores;
  if(!scores)return '<span class="subtle">—</span>';
  const band=campaignScoreBand(scores.iptm),ranking=scores.rankingScore!=null?`<b class="campaign-score-value">${formatScore(scores.rankingScore)}</b>`:'';
  const detail=[['ipTM',scores.iptm],['pTM',scores.ptm],['pLDDT',scores.meanPlddt]].filter(([,value])=>value!=null)
    .map(([label,value])=>`${label} ${formatScore(value)}`).join(' · ');
  const metric=scores.rankingMetric?`排名口径：${scores.rankingMetric}。`:'';
  const hint=band?'EBI ipTM 阈值：≥0.8 高置信 / 0.6–0.8 灰区 / <0.6 可能失败。':'';
  return `<div class="campaign-score-cell" title="${escapeHTML(metric+hint+'未评分的旧结果可能缺少部分指标。')}">${ranking}<span class="campaign-score-detail">${detail||'未提供置信指标'}</span>${band?`<span class="campaign-score-band ${band}">${{high:'高置信',gray:'灰区',low:'低置信'}[band]}</span>`:''}</div>`;
}
function campaignPreparationReuse(item){
  const reuse=item.preparationReuse;
  return reuse?.strategy==='af3-chain-reuse-v1'?reuse:null;
}
function renderCampaignCachePills(item){
  const reuse=campaignPreparationReuse(item),whole=reuse?reuse.wholeTargetCacheHit:item.cacheHit;
  if(whole)return '<span class="campaign-cache-pill">整目标缓存命中</span>';
  if(!reuse)return '';
  return `<span class="campaign-cache-pill campaign-chain-pill" title="本目标准备时的链缓存命中数 / 目标内去重链数">链缓存 ${formatCount(reuse.chainCacheHits)} / ${formatCount(reuse.uniqueChains)}</span>${reuse.duplicateChainCopies?`<span class="campaign-cache-pill campaign-chain-copy">相同链副本 ${formatCount(reuse.duplicateChainCopies)}</span>`:''}`;
}
function renderPreparationReuseOverview(c){
  const reuse=c.metrics?.preparationReuse,hasData=Number(reuse?.targets)>0;
  if(c.engine!=='alphafold3'&&!hasData)return '';
  const cards=hasData?`<dl class="campaign-reuse-stats">${[
    ['newChainSearches','新链搜索','实际需要新建链准备缓存'],
    ['chainCacheHits','链缓存命中','准备时复用已有链缓存'],
    ['duplicateChainCopies','相同链副本','同一目标内的相同序列链'],
    ['wholeTargetCacheHits','整目标命中','直接复用已组装的准备包']
  ].map(([key,label,note])=>`<div data-reuse-metric="${key}"><dt>${label}</dt><dd>${formatCount(reuse[key])}</dd><small>${note}</small></div>`).join('')}</dl>`:'<p class="campaign-reuse-empty">AF3 准备完成后显示链级统计；较早的整目标缓存记录可能没有这些字段。</p>';
  const scope=hasData?`<p class="campaign-reuse-scope">逐目标最近一次已发布准备记录汇总：${formatCount(reuse.targets)} 个目标、${formatCount(reuse.totalChains)} 条链；逐目标去重链数之和 ${formatCount(reuse.uniqueChainOccurrences)}，不是全批次唯一序列数。整目标命中不重复计入本次链搜索与链命中；相同链副本是组装数量，不是复制操作次数。</p>`:'';
  return `<section class="campaign-reuse-overview" aria-label="AF3 准备输入复用"><div class="campaign-reuse-heading"><span>${icon('layers')}</span><div><span class="campaign-reuse-eyebrow">REUSE THE SHARED CHAIN</span><h4>固定靶标复用，候选分别预测。</h4></div>${hasData?`<span class="campaign-reuse-count">${formatCount(reuse.targets)} 个目标有记录</span>`:'<span class="campaign-reuse-count">等待准备记录</span>'}</div>${cards}<div class="campaign-reuse-mechanism"><span><b>公共靶标链</b>MSA + 模板搜索结果</span><i aria-hidden="true">→</i><span><b>匹配链缓存</b>序列 · 数据库版本 · 搜索设置</span><i aria-hidden="true">→</i><span><b>分别组装候选组合</b>每个目标继续独立预测</span></div>${scope}<p class="campaign-reuse-explanation">序列和数据库、搜索、模板配置相同的自动搜索蛋白链可复用准备结果；复用的是模型输入，各候选的结构仍需分别计算。</p></section>`;
}
function renderPreparationReuseDetails(item,c){
  const reuse=campaignPreparationReuse(item);
  if(!reuse)return c.engine==='alphafold3'?'<section class="campaign-inspect-section campaign-reuse-detail"><h3>链级准备复用</h3><p class="campaign-inspect-hint">AF3 准备完成后显示；本目标目前没有链级复用记录。旧整目标缓存记录不补推链命中次数。</p></section>':'';
  const whole=reuse.wholeTargetCacheHit===true,entries=Array.isArray(reuse.entries)?reuse.entries:[];
  const status=whole?'整目标准备包命中':'按链准备后组装';
  const note=whole?'本次直接复用整目标准备包，没有执行链缓存查询或新链搜索。下方逐链明细是首次组装时的历史记录，不是本次查询结果。':'下方记录本目标准备时各条唯一蛋白链的查询结果；同序列的多个链 ID 共用一份链准备结果。';
  const rows=entries.map(entry=>{
    const ids=Array.isArray(entry.chainIds)?entry.chainIds.join(' / '):String(entry.chainIds||'未提供');
    const label=(whole?'首次组装：':'')+(entry.cacheHit?'链缓存命中':'新链搜索');
    return `<details class="campaign-chain-entry ${whole?'historical':entry.cacheHit?'reused':'searched'}"><summary><span class="campaign-chain-id">链 ${escapeHTML(ids)}</span><span class="campaign-chain-length">${formatCount(entry.residueCount)} aa</span><span class="campaign-chain-outcome">${label}</span></summary><dl class="campaign-inspect-facts">${campaignFact('序列 SHA-256',entry.sequenceSha256)}${campaignFact('链准备缓存键',entry.cacheKey)}${campaignFact('复用内容','该链的 MSA 与模板搜索结果')}</dl>${window.renderMSAChainQuality?.(entry,whole)||''}</details>`;
  }).join('');
  return `<section class="campaign-inspect-section campaign-reuse-detail" data-reuse-whole="${whole}"><div class="campaign-inspect-section-title"><h3>链级准备复用</h3><span class="campaign-reuse-detail-status">${status}</span></div><p class="campaign-reuse-detail-note">${note}</p><dl class="campaign-reuse-detail-counts">${campaignFact('目标链数',reuse.totalChains)}${campaignFact('目标内去重链数',reuse.uniqueChains)}${campaignFact('本次新链搜索',reuse.newChainSearches)}${campaignFact('本次链缓存命中',reuse.chainCacheHits)}${campaignFact('同目标相同链副本',reuse.duplicateChainCopies)}</dl><div class="campaign-chain-entries">${rows||'<p class="campaign-inspect-hint">未返回逐链记录。</p>'}</div><p class="campaign-inspect-hint">缓存包含该链的 MSA 与模板搜索结果，匹配序列、数据库版本及搜索 / 模板设置。各目标重新组装输入并执行结构预测，不复用其他候选的结构结果。</p></section>`;
}
const campaignActionNames={pause:'暂停取新任务',resume:'继续执行',cancel:'取消未完成目标','retry-failed':'重试失败项'};
function renderCampaignOperation(c){
  if(batchOperation.confirmation?.campaignId===c.id){
    const remaining=pipelineStages.slice(0,-1).reduce((n,[stage])=>n+Number(c.counts?.[stage]||0),0);
    return `<section class="campaign-cancel-confirm" role="group" aria-labelledby="campaign-cancel-title"><h4 id="campaign-cancel-title">取消「${escapeHTML(c.name)}」的未完成目标？</h4><p>${formatCount(remaining)} 项未完成目标将取消，${formatCount(c.counts?.completed)} 项已交付结果与历史记录保留。此批次取消后无法恢复执行。</p><div><button class="button button-secondary" id="campaign-cancel-back">保留批次</button><button class="button button-danger" id="campaign-cancel-apply" data-campaign-id="${escapeHTML(c.id)}">确认取消未完成目标</button></div></section>`;
  }
  const pending=batchOperation.pending;
  if(pending?.campaignId===c.id)return `<div class="campaign-operation-feedback" role="status" aria-live="polite">正在${escapeHTML(campaignActionNames[pending.action])}「${escapeHTML(c.name)}」…</div>`;
  const error=batchOperation.error;
  if(error?.campaignId===c.id)return `<div class="campaign-operation-error" role="alert"><div><strong>${escapeHTML(campaignActionNames[error.action])}请求未确认</strong><p>${escapeHTML(error.message)}</p><small>请按刷新后的批次状态继续操作。</small></div><button class="text-button" id="campaign-dismiss-operation-error" aria-label="关闭操作错误提示">关闭提示</button></div>`;
  if(batchOperation.notice?.campaignId===c.id)return `<div class="campaign-operation-feedback completed" role="status">${escapeHTML(batchOperation.notice.message)}</div>`;
  return '';
}
async function runCampaignAction(campaignId,action,confirmed=false){
  if(batchOperation.pending||!campaignActionNames[action])return;
  const c=batchState.campaign;
  if(!c||c.id!==campaignId||batchState.selectedId!==campaignId)return;
  if(action==='cancel'&&!confirmed){batchOperation.confirmation={campaignId};batchOperation.error=null;batchOperation.notice=null;renderCampaignMonitorRegion();$('#campaign-cancel-back')?.focus({preventScroll:true});return;}
  batchOperation.confirmation=null;batchOperation.error=null;batchOperation.notice=null;batchOperation.pending={campaignId,action};renderCampaignMonitorRegion();
  try{
    await api(`/api/campaigns/${encodeURIComponent(campaignId)}/${action}`,{method:'POST',body:'{}'});
    batchOperation.notice={campaignId,message:`${campaignActionNames[action]}请求已提交`};
  }catch(error){batchOperation.error={campaignId,action,message:error.message};}
  finally{
    await refreshCampaignData(true);batchOperation.pending=null;
    if(state.view==='campaigns'){
      const needsFocus=document.activeElement===document.body;
      renderCampaignMonitorRegion();
      if(needsFocus){const next=action==='pause'?'resume':action==='resume'?'pause':action;const control=document.querySelector(`[data-campaign-action="${next}"]:not(:disabled)`)||$('#campaign-select');control?.focus({preventScroll:true});}
    }
  }
}
function dismissCampaignCancellation(){batchOperation.confirmation=null;renderCampaignMonitorRegion();document.querySelector('[data-campaign-action="cancel"]')?.focus({preventScroll:true});}
function renderCampaignMonitor(){
  const c=batchState.campaign,counts=c?.counts||{},config=batchState.config;
  const selector=`<div class="toolbar batch-selector"><select id="campaign-select" aria-label="选择预测批次"><option value="" ${batchState.campaigns.length?'disabled':''}>${batchState.loaded?'暂无批次，请先导入':'正在读取批次…'}</option>${batchState.campaigns.map(item=>`<option value="${escapeHTML(item.id)}" ${item.id===batchState.selectedId?'selected':''}>${escapeHTML(item.name)} · ${formatCount(item.totalItems)} 项</option>`).join('')}</select><button class="button button-secondary" id="refresh-campaign">${icon('rotate')}刷新</button></div><p class="campaign-refresh-state" role="status" hidden>正在更新目标列表…</p>${batchState.error?`<div class="campaign-refresh-error" role="alert">${escapeHTML(batchState.error)}${c?.id&&c.id!==batchState.selectedId?`<p>仍显示上次读取的「${escapeHTML(c.name)}」。所选批次读取成功后可继续操作。</p>`:''}</div>`:''}`;
  if(!c)return selector+`<section class="panel batch-empty">${icon('workflow')}<h3>等待一份批次清单</h3><p>导入文件后，各阶段的积压、执行和完成数量会在这里实时更新。</p></section>`;
  const total=c.totalItems||0,done=counts.completed||0,failed=counts.failed||0,cancelled=counts.cancelled||0;
  const backlog=pipelineStages.slice(0,-1).reduce((n,[stage])=>n+Number(counts[stage]||0),0);
  const percent=total?done/total*100:0;
  const statusText=c.isComplete?(failed?'已结束，有失败项':'已完成'):({active:'执行中',paused:'已暂停',cancelled:'已取消',completed:'已完成'}[c.status]||c.status);
  const stats=`<div class="batch-stats campaign-overview"><div><small>批次目标</small><strong>${formatCount(total)}</strong><span>${c.status==='cancelled'?'批次已取消':c.sealed?'清单已封存':'清单开放 · 分母随导入增加'}</span></div><div><small>已交付结构</small><strong>${formatCount(done)} <em>${percent.toFixed(1)}%</em></strong><span>已验证并发布 / 当前入库目标</span></div><div><small>管线内待交付</small><strong>${formatCount(backlog)}</strong><span>失败 ${formatCount(failed)} · 取消 ${formatCount(cancelled)} 另计</span></div></div><div class="campaign-delivery" aria-label="结构交付率 ${percent.toFixed(1)}%"><span style="width:${percent.toFixed(2)}%"></span></div>${renderCampaignBandSummary(c)}`;
  const workerMode=config.predictionMode==='workers',online=(batchState.cluster?.nodes||[]).filter(n=>n.status==='online');
  const residentInfo=workerMode?`<div class="campaign-execution-note"><span>${icon('server')}<b>常驻节点</b> · ${{local:'仅本地',aliyun:'仅阿里云',hybrid:'本地与阿里云'}[c.placement||'hybrid']} · ${online.length} 个心跳在线${online.some(n=>n.demo||n.mode==='fixture')?'（含回放演示）':''}</span><button class="text-button" id="campaign-cluster-link">节点与传输 ↗</button></div>`:'';
  const pools=`<div class="batch-pools"><span>${icon('search')}准备槽位 <b>${config.prepWorkers??'—'}</b></span><span>${icon('cpu')}${workerMode?'常驻在线节点':'预测执行槽'} <b>${workerMode?online.length:config.gpuWorkers??'—'}</b></span><span>${icon('chart')}验证执行槽 <b>${config.validationWorkers??'—'}</b></span><span>${icon('layers')}全局就绪高水位 <b>${formatCount(config.readyCap)}</b></span></div>`;
  const stages=`<div class="batch-stage-flow">${pipelineStages.map(([id,label,ic],i)=>`<button class="batch-stage ${batchState.stage===id?'selected':''} ${Number(counts[id])>0?'has-items':''}" data-stage-filter="${id}" aria-pressed="${batchState.stage===id}"><span class="stage-order">0${i+1} ${icon(ic)}</span><strong>${formatCount(counts[id])}</strong><span>${label}</span></button>`).join('')}</div>`;
  const actions=`<div class="batch-title-row"><div><h3>${escapeHTML(c.name)}</h3><span class="batch-status ${escapeHTML(c.status)}">● ${escapeHTML(statusText)}</span><span class="model-badge">${escapeHTML(engineNames[c.engine]||c.engine)}</span></div><div class="task-actions">${failed&&c.status!=='cancelled'?'<button class="button button-soft" data-campaign-action="retry-failed" data-campaign-id="'+escapeHTML(c.id)+'" '+(campaignControlBlocked()?'disabled':'')+'>重试失败项</button>':''}${!c.isComplete&&c.status==='active'?'<button class="button button-secondary" data-campaign-action="pause" data-campaign-id="'+escapeHTML(c.id)+'" '+(campaignControlBlocked()?'disabled':'')+'>暂停取新任务</button>':''}${!c.isComplete&&c.status==='paused'?'<button class="button button-soft" data-campaign-action="resume" data-campaign-id="'+escapeHTML(c.id)+'" '+(campaignControlBlocked()?'disabled':'')+'>继续执行</button>':''}${!c.isComplete&&['active','paused'].includes(c.status)?'<button class="button button-danger" data-campaign-action="cancel" data-campaign-id="'+escapeHTML(c.id)+'" '+(campaignControlBlocked()?'disabled':'')+'>取消剩余任务</button>':''}</div></div>`;
  const [diagnosisTitle,diagnosisText]=campaignDiagnostic(c,batchState.stage);
  const diagnosis=`<div class="campaign-diagnosis"><span>${icon(batchState.stage==='failed'?'shield':'search')}</span><div><strong>${escapeHTML(diagnosisTitle)}</strong><p>${escapeHTML(diagnosisText)}</p></div></div>`;
  const quickFilters=`<div class="campaign-filter-chips" aria-label="快速筛选">${[['','全部',total],['ready','等待预测',counts.ready||0],['failed','失败项',failed],['completed','已交付',done]].map(([id,label,n])=>`<button data-stage-filter="${id}" class="${batchState.stage===id?'active':''}" aria-pressed="${batchState.stage===id}">${label}<b>${formatCount(n)}</b></button>`).join('')}${Number(c.metrics?.iptmScoredTargets)>0?[[0.8,'ipTM ≥ 0.8'],[0.6,'ipTM ≥ 0.6']].map(([value,label])=>`<button data-score-filter="${value}" class="${batchState.minIptm===value?'active':''}" aria-pressed="${batchState.minIptm===value}" title="EBI 阈值：≥0.8 高置信，0.6–0.8 灰区">${label}</button>`).join(''):''}</div>`;
  const deliveredScoreQuery={sort:batchState.sort!=='seq'?batchState.sort:null,min_iptm:batchState.minIptm};
  const exports=`<div class="campaign-export"><span>${icon('download')}导出清单</span>${[['','全部'],['completed','已交付'],['failed','失败项']].map(([id,label])=>`<a class="text-button" href="${campaignExportUrl(c,id,id==='completed'?deliveredScoreQuery:null)}" download data-campaign-export="${id||'all'}">${label} ↗</a>`).join('')}<small>JSONL · 元数据、结构链接与置信度分数 · 已交付导出跟随当前排序与阈值 · 一致快照</small></div>`;
  const pagination=`<div class="panel-footer"><span>共 ${formatCount(batchState.page.total)} 项 · 第 ${Math.floor(batchState.offset/25)+1} 页 · 每页 25 项</span><div class="task-actions"><button class="text-button" id="campaign-prev" ${batchState.offset===0?'disabled':''}>← 上一页</button><button class="text-button" id="campaign-next" ${batchState.offset+25>=batchState.page.total?'disabled':''}>下一页 →</button></div></div>`;
  const pageCurrent=!batchState.publishedView||batchState.publishedView===campaignViewKey();
  const rows=(pageCurrent?batchState.page.items||[]:[]).map(item=>`<tr><td><button class="campaign-target-link" data-inspect-target="${escapeHTML(item.id)}" aria-label="检查目标 ${escapeHTML(item.name||item.targetId)}">${escapeHTML(item.name||item.targetId)}</button><span class="job-subline mono">${escapeHTML(item.targetId)}</span></td><td><span class="batch-item-state ${escapeHTML(item.stage)}">${escapeHTML(batchStageNames[item.stage]||item.stage)}</span>${renderCampaignCachePills(item)}${window.renderMSAProgressPill?.(item)||''}</td><td class="campaign-score-td">${renderCampaignScoreCell(item)}</td><td class="mono">${formatCount(item.sequenceLength)} aa</td><td class="mono">${item.attempt??0}</td><td><div class="campaign-row-actions"><button class="text-button" data-inspect-target="${escapeHTML(item.id)}">详情</button>${item.stage==='completed'?`<button class="text-button" data-campaign-item="${escapeHTML(item.id)}">查看结构 ↗</button>`:''}</div>${item.error?`<span class="batch-item-note campaign-error-preview" title="${escapeHTML(item.error)}">${escapeHTML(item.error)}</span>`:''}</td></tr>`).join('');
  const scoreSort=`<select id="campaign-score-sort" aria-label="按置信度排序"><option value="seq" ${batchState.sort==='seq'?'selected':''}>默认顺序</option><option value="ranking" ${batchState.sort==='ranking'?'selected':''}>排名分 降序</option><option value="iptm" ${batchState.sort==='iptm'?'selected':''}>ipTM 降序</option><option value="ptm" ${batchState.sort==='ptm'?'selected':''}>pTM 降序</option><option value="plddt" ${batchState.sort==='plddt'?'selected':''}>平均 pLDDT 降序</option></select>`;
  return selector+`<section class="panel batch-dashboard">${actions}${renderCampaignOperation(c)}${stats}${pools}${typeof renderPreparationResourcesPanel==='function'?renderPreparationResourcesPanel(config):''}${stages}<p class="batch-footnote">本批次准备中 + 就绪 ${formatCount((counts.preparing||0)+(counts.ready||0))} 项；高水位由所有批次共享。参考回放不执行真实 MSA 或 GPU 计算。</p>${residentInfo}${diagnosis}${c.engine!=='reference'?window.renderMSASearchStrategy?.(c.databaseProfile,'campaign')||'':''}${window.renderMSAOverview?.(c)||''}${renderPreparationReuseOverview(c)}</section><section class="panel batch-items"><div class="panel-heading"><h3>${icon('layers')}目标与阶段</h3><div class="campaign-list-controls">${scoreSort}<select id="campaign-stage-filter" aria-label="筛选管线阶段"><option value="">全部阶段</option>${Object.entries(batchStageNames).map(([id,label])=>`<option value="${id}" ${batchState.stage===id?'selected':''}>${label}</option>`).join('')}</select></div></div>${quickFilters}<div class="table-scroll"><table><thead><tr><th>预测目标</th><th>阶段</th><th title="排名分为 AF3 官方 ranking score 口径（越高越好）；ipTM/pTM 为 0–1 界面与拓扑置信；平均 pLDDT 为 0–100 局部置信">置信度</th><th>长度</th><th title="阶段领取及恢复累计计数，不是模型采样次数">阶段领取次数</th><th>检查 / 结构</th></tr></thead><tbody>${rows||`<tr><td colspan="6" class="campaign-table-empty">${pageCurrent?'当前筛选没有目标':batchState.error?'所选列表读取失败，点击刷新重试。':'正在读取所选目标列表…'}</td></tr>`}</tbody></table></div>${pagination}${exports}</section>`;
}

function campaignStageId(value){return ({prepare:'preparing',preparation:'preparing',prepared:'ready',predict:'predicting',prediction:'predicting',validate:'validating',validation:'validating'})[value]||value;}
function campaignTime(value){if(!value)return '未记录';const d=new Date(value);return Number.isNaN(d.valueOf())?String(value):d.toLocaleString('zh-CN',{hour12:false});}
function campaignDuration(value){return Number.isFinite(Number(value))&&value!==null&&value!==undefined?`${Number(value).toFixed(2)} s`:'未记录';}
function campaignFact(label,value){return `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value===undefined||value===null||value===''?'未提供':String(value))}</dd></div>`;}
function campaignLeaseEvidence(item){
  const matches=[];for(const b of batchState.cluster?.batches||[])for(const m of b.members||[])if(m.itemId===item.id&&m.campaignId===item.campaignId)matches.push({batch:b,member:m});
  return matches.find(v=>v.member.leaseActive)||matches[0]||null;
}
function renderCampaignInspection(item,c){
  const p=item.result?.provenance||{},input=item.input||{},events=item.events||[],timings=item.timings||{};
  const evidence=campaignLeaseEvidence(item),db=input.databaseProfile||c.databaseProfile||{};
  const failedStage=campaignStageId(item.failedStage),visited=new Set(events.map(e=>campaignStageId(e.stage)));visited.add(item.stage);
  const rail=`<ol class="campaign-inspect-rail">${pipelineStages.map(([id,label],i)=>`<li class="${id===item.stage?'current':visited.has(id)?'observed':''} ${failedStage===id?'failed':''}"><i>${failedStage===id?'!':id===item.stage?'●':visited.has(id)?'✓':i+1}</i><span>${label}${failedStage===id?'<b>此阶段失败</b>':''}</span></li>`).join('')}</ol>`;
  const [diagTitle,diagText]=campaignDiagnostic(c,item.stage);
  const reuse=campaignPreparationReuse(item),wholeHit=reuse?reuse.wholeTargetCacheHit:item.cacheHit;
  const cacheText=wholeHit?'命中已完成准备缓存':['waiting_input','preparing'].includes(item.stage)?'尚无缓存命中记录':'本次未记录整目标缓存命中';
  const workerId=p.workerId||evidence?.batch.nodeId,node=(batchState.cluster?.nodes||[]).find(n=>n.id===workerId);
  const origin=p.workerLocation||p.location||node?.location;
  const locationLabel=origin?({local:'本地',aliyun:'阿里云位置标签'}[origin]||origin):'未提供';
  const resultButton=item.stage==='completed'?`<button class="button button-primary" id="campaign-inspect-structure">${icon('layers')}打开结构视图 ↗</button>`:'';
  const directDownload=item.stage==='completed'&&item.resultAvailability?.status!=='legacy-large-result'?`<a class="button button-secondary" href="${escapeHTML(appUrl(`/api/campaigns/${encodeURIComponent(c.id)}/items/${encodeURIComponent(item.id)}/structure`))}" download>${icon('download')}下载结构</a>`:'';
  const execution=workerId?`<dl class="campaign-inspect-facts">${campaignFact('执行节点',workerId)}${campaignFact('位置',locationLabel)}${campaignFact('批次 ID',p.batchId||evidence?.batch.id)}${campaignFact('常驻 PID',p.residentPid??node?.pid)}${campaignFact('权重加载次数',p.modelLoads??node?.modelLoads)}${campaignFact('执行模式',p.executionMode||p.workerMode||node?.mode)}</dl>${(p.demo||node?.demo||node?.mode==='fixture')?'<p class="campaign-inspect-hint">该节点为公开结构回放演示，位置标签不代表实际云 GPU。</p>':''}`:`<p class="campaign-inspect-hint">${item.stage==='waiting_input'?'尚未分配执行节点。':p.sourceLabel?escapeHTML(p.sourceLabel):'当前目标记录未提供远程节点信息。'}${p.pipelineMode==='staged'?' 本目标使用分阶段管线。':''}</p>`;
  const lease=evidence?`<dl class="campaign-inspect-facts">${campaignFact('当前窗口租约',evidence.member.leaseActive?'活跃':'非活跃 / 已结束')}${campaignFact('批次状态',evidence.batch.status)}${campaignFact('回传状态',evidence.member.deliveryState||evidence.member.status)}${campaignFact('关联批次',evidence.batch.id)}</dl><p class="campaign-inspect-hint">来自最近批次窗口；到期时间不在此 API 中返回。</p>`:'<p class="campaign-inspect-hint">最近批次窗口未提供此目标的租约记录。本地 legacy 执行也不会产生远程批次租约。</p>';
  return `<header class="campaign-inspect-header"><div><span class="campaign-inspect-eyebrow">TARGET INSPECTOR</span><h2 id="campaign-inspect-title">${escapeHTML(item.name||item.targetId)}</h2><p class="mono">${escapeHTML(item.targetId)}</p></div><button class="icon-button" id="campaign-inspect-close" aria-label="关闭目标详情">${icon('x')}</button></header><div class="campaign-inspect-body"><div class="campaign-inspect-status"><span class="batch-item-state ${escapeHTML(item.stage)}">${escapeHTML(batchStageNames[item.stage]||item.stage)}</span><span>${escapeHTML(engineNames[c.engine]||c.engine)} · ${formatCount(item.sequenceLength)} aa</span><button class="text-button" id="campaign-inspect-refresh">${icon('rotate')}刷新此目标</button></div>${rail}<p class="campaign-inspect-hint">高亮来自当前阶段与保留的事件记录；未记录的阶段不补写时间。</p><div class="campaign-diagnosis"><span>${icon('search')}</span><div><strong>${escapeHTML(diagTitle)}</strong><p>${escapeHTML(diagText)}</p></div></div>${item.error?`<section class="campaign-inspect-error"><h3>执行错误 · ${escapeHTML(batchStageNames[failedStage]||item.failedStage||'阶段未记录')}</h3><pre>${escapeHTML(item.error)}</pre></section>`:''}<div class="campaign-inspect-actions">${resultButton}${directDownload}</div>${renderCampaignInspectScores(item)}<section class="campaign-inspect-section"><h3>输入与阶段记录</h3><dl class="campaign-inspect-facts">${campaignFact('阶段领取 / 恢复计数',item.attempt??0)}${campaignFact('整目标准备缓存',cacheText)}${campaignFact('准备阶段累计',campaignDuration(timings.preparationSeconds))}${campaignFact('预测阶段累计',campaignDuration(timings.predictionSeconds))}${campaignFact('结果验证累计',campaignDuration(timings.validationSeconds))}${campaignFact('种子',(input.seeds||c.settings?.seeds||[]).join(', '))}${campaignFact('每种子采样 / 模型数',input.numSamples??c.settings?.numSamples)}${campaignFact('Recycles',input.numRecycles??c.settings?.numRecycles)}${campaignFact('录入时间',campaignTime(item.createdAt))}${campaignFact('最后更新',campaignTime(item.updatedAt))}</dl><p class="campaign-inspect-hint">领取计数按管线阶段累积，不能当作模型失败重试次数。耗时仅为已记录阶段累计，预测阶段还可能含启动、编译和文件处理。</p></section>${window.renderMSAProgress?.(item,c)||''}${c.engine!=='reference'?window.renderMSASearchStrategy?.(db,'target')||'':''}${renderPreparationReuseDetails(item,c)}${window.renderPAEInspectorMetadata?.(item.result)||''}${window.renderResultAvailability?.(item)||''}<section class="campaign-inspect-section"><h3>执行来源</h3>${execution}</section><section class="campaign-inspect-section"><h3>租约与交接</h3>${lease}</section><details class="campaign-inspect-identifiers"><summary>输入指纹、数据库与准备缓存键</summary><dl class="campaign-inspect-facts">${campaignFact('输入 SHA-256',item.inputHash)}${campaignFact('数据库配置',db.id)}${campaignFact('数据库 release',db.release)}${campaignFact('数据库指纹',db.profileFingerprint)}${campaignFact('准备缓存键',p.preparationCacheKey)}${campaignFact('传输 SHA-256',p.transportSha256)}${campaignFact('目标 ID',item.id)}${campaignFact('批次 ID',c.id)}</dl></details><section class="campaign-inspect-section"><div class="campaign-inspect-section-title"><h3>阶段事件</h3><span>${events.length} 条保留记录 · 最新在上</span></div><ol class="campaign-inspect-events">${[...events].reverse().map(e=>`<li><i></i><div><strong>${escapeHTML(batchStageNames[e.stage]||e.stage||'事件')}</strong><p>${escapeHTML(e.message||'')}</p><time>${escapeHTML(campaignTime(e.time))}</time></div></li>`).join('')||'<li class="campaign-inspect-no-events">尚无阶段事件。</li>'}</ol></section></div><footer class="campaign-inspect-footer"><span>读取于 ${escapeHTML(new Date().toLocaleTimeString('zh-CN',{hour12:false}))} · 手动刷新详情</span><button class="button button-secondary" id="campaign-inspect-done">关闭</button></footer>`;
}
function closeCampaignInspection(){const dialog=document.getElementById('campaign-inspector');if(dialog?.open)dialog.close();}
async function loadCampaignInspection(){
  const dialog=document.getElementById('campaign-inspector'),c=batchInspection.campaign,id=batchInspection.itemId,generation=++batchInspection.generation;if(!dialog||!c||!id)return;
  const body=dialog.querySelector('.campaign-inspect-body'),scroll=body?.scrollTop||0;
  const refresh=dialog.querySelector('#campaign-inspect-refresh'),refreshFocused=refresh===document.activeElement;if(refresh)refresh.disabled=true;
  try{
    const item=await api(`/api/campaigns/${encodeURIComponent(c.id)}/items/${encodeURIComponent(id)}`);
    if(generation!==batchInspection.generation||!dialog.open)return;
    batchInspection.item=item;dialog.innerHTML=renderCampaignInspection(item,c);hydrateIcons(dialog);bindCampaignInspection();
    dialog.querySelector('.campaign-inspect-body').scrollTop=scroll;
    if(!body)dialog.querySelector('#campaign-inspect-close').focus({preventScroll:true});
    else if(refreshFocused)dialog.querySelector('#campaign-inspect-refresh').focus({preventScroll:true});
  }catch(error){
    if(generation!==batchInspection.generation||!dialog.open)return;
    if(body){toast(error.message,true);if(refresh)refresh.disabled=false;}
    else{dialog.innerHTML=`<div class="campaign-inspect-loading"><h2 id="campaign-inspect-title">读取目标失败</h2><p>${escapeHTML(error.message)}</p><button class="button button-secondary" id="campaign-inspect-close">关闭</button></div>`;bindCampaignInspection();}
  }
}
function bindCampaignInspection(){
  $('#campaign-inspect-close')?.addEventListener('click',closeCampaignInspection);$('#campaign-inspect-done')?.addEventListener('click',closeCampaignInspection);
  $('#campaign-inspect-refresh')?.addEventListener('click',loadCampaignInspection);
  $('#campaign-inspect-structure')?.addEventListener('click',()=>{const item=batchInspection.item,c=batchInspection.campaign;closeCampaignInspection();showCampaignStructure(item,c);});
}
async function inspectCampaignTarget(campaignId,itemId){
  try{
    const c=batchState.campaign?.id===campaignId?batchState.campaign:await api(`/api/campaigns/${encodeURIComponent(campaignId)}`);
    batchInspection.returnFocus=document.activeElement;batchInspection.campaign=c;batchInspection.itemId=itemId;batchInspection.item=null;
    if(batchState.selectedId!==campaignId){batchState.selectedId=campaignId;batchState.campaign=c;batchState.offset=0;batchState.stage='';localStorage.setItem('foldspace.campaign',campaignId);}
    if(state.view!=='campaigns')navigate('campaigns');else refreshCampaignData();
    let dialog=document.getElementById('campaign-inspector');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='campaign-inspector';dialog.className='campaign-inspector';dialog.setAttribute('aria-labelledby','campaign-inspect-title');document.body.appendChild(dialog);dialog.addEventListener('close',()=>{batchInspection.generation++;const trigger=batchInspection.returnFocus;if(trigger?.isConnected)trigger.focus({preventScroll:true});else document.getElementById('campaign-stage-filter')?.focus({preventScroll:true});});}
    dialog.innerHTML='<div class="campaign-inspect-loading"><h2 id="campaign-inspect-title">读取目标记录…</h2><p>正在读取阶段事件、执行来源与准备缓存。</p><button class="button button-secondary" id="campaign-inspect-close">关闭</button></div>';bindCampaignInspection();
    if(!dialog.open)dialog.showModal();await loadCampaignInspection();
  }catch(error){toast(error.message,true);}
}

function renderDatabases(){
  const profiles=batchState.databases;
  return `<section class="batch-intro"><div><div class="eyebrow">SEQUENCE → MSA → STRUCTURE</div><h2>搜索库与结构结果库，各有用途。</h2><p>序列库提供同源序列，模板库提供已有结构模板。AlphaFold DB / PDB 的参考结构回放，不能替代一次新的 MSA 搜索。</p></div><span class="batch-emblem">${icon('search')}</span></section><section class="panel database-explainer"><div><span class="database-tag">01 · 本地文件与索引</span><h3>搜索运行器直接读取数据库</h3><p>将已下载的库挂载到搜索节点的只读路径，运行器使用对应路径参数。本地 NVMe、共享存储或云端同区副本承担实际读取；搜索设备由注册策略确定。</p></div><div><span class="database-tag">02 · 版本与缓存</span><h3>数据库版本进入准备缓存键</h3><p>相同序列与搜索设置可复用已完成的 MSA / 特征；更新库版本、模板策略或搜索版本会生成新的缓存键。</p></div><div><span class="database-tag">03 · 预检与执行</span><h3>看到路径状态，再启动真实搜索</h3><p>预检检查声明的文件、目录与索引是否存在且可读。检索能否正确运行仍需用对应引擎执行样本确认。</p></div></section><div class="database-toolbar"><h3>已注册数据库配置 <span class="count-pill">${profiles.length}</span></h3><button class="button button-secondary" id="refresh-databases">${icon('rotate')}重新预检</button></div>${profiles.length?`<div class="database-grid">${profiles.map(profile=>renderDatabaseProfile(profile)).join('')}</div>`:`<section class="panel batch-empty">${icon('server')}<h3>${batchState.loaded?'尚未注册真实搜索数据库':'正在读取数据库配置'}</h3><p>通过 <code>PROTEIN_STUDIO_DATABASE_CONFIG</code> 指定管理员配置。项目提供路径与索引声明示例，不包含实际数据库。</p></section>`}<section class="panel config-note"><strong>数据库配置入口</strong><p><code>config/databases.example.json</code> 定义引擎、release、root、序列 / 模板资产和必要索引；<code>config/staged-engines.example.json</code> 定义准备与预测命令。配置完成后重启服务，将数据库配置 ID 绑定到批次。</p><p>AF3 使用 UniRef90、MGnify、BFD small、UniProt 等序列资产及 PDB 模板；AF2、ColabFold 的具体库与索引按各自官方运行器配置。详见 <code>docs/database-and-batch-pipeline.md</code>。</p></section>${batchState.error?`<div class="error-banner">${escapeHTML(batchState.error)}</div>`:''}`;
}

function renderDatabaseProfile(profile){
  const assets=profile.assets||[],diagnostics=profile.diagnostics||[];
  return `<section class="panel database-card"><div class="environment-top"><h3>${escapeHTML(profile.name||profile.id)}</h3><span class="status-pill ${profile.ready?'completed':'failed'}"><i></i>${profile.ready?'路径预检通过':'需要配置'}</span></div><p class="database-meta">${escapeHTML(engineNames[profile.engine]||profile.engine)} · ${escapeHTML(profile.release||'未声明版本')}</p><code class="database-root">${escapeHTML(profile.root||(profile.engine==='reference'?'无需数据库':'未设置根目录'))}</code>${window.renderMSASearchStrategy?.(profile,'database')||''}<div class="database-assets">${assets.map(asset=>`<div><span class="database-asset-kind">${escapeHTML(asset.kind==='template'?'模板':asset.kind==='sequence'?'序列':asset.kind||'资产')}</span><strong>${escapeHTML(asset.name||asset.id||'asset')}</strong><code>${escapeHTML(asset.resolvedPath||asset.path||'')}</code></div>`).join('')}</div>${diagnostics.length?`<details class="database-issues"><summary>${diagnostics.length} 项预检提示 · 展开查看</summary><ul class="database-diagnostics">${diagnostics.map(d=>`<li>${escapeHTML(typeof d==='string'?d:d.message||JSON.stringify(d))}</li>`).join('')}</ul></details>`:''}<details class="database-fingerprint"><summary>版本指纹与搜索参数</summary><pre>${escapeHTML(JSON.stringify({id:profile.id,profileFingerprint:profile.profileFingerprint,searchVersion:profile.searchVersion,prepareVersion:profile.prepareVersion,preparationStrategy:profile.preparationStrategy,searchParameters:profile.searchParameters,templateParameters:profile.templateParameters},null,2))}</pre></details></section>`;
}

function updateCampaignEngineOptions(){
  const limitLabel=$('#campaign-fixed-limit');if(limitLabel)limitLabel.textContent=campaignResidueLimitLabel();
  const select=$('#campaign-engine');if(!select)return;
  const capabilities=batchEngineCapabilities(),selected=select.value||'reference';
  select.innerHTML=(capabilities.length?capabilities:[{id:'reference',available:true}]).map(item=>`<option value="${escapeHTML(item.id||item.engine)}" ${!batchEngineAvailable(item)?'disabled':''}>${escapeHTML(engineNames[item.id||item.engine]||item.name||item.id)}${batchEngineAvailable(item)?'':' · 未就绪'}</option>`).join('');
  if([...select.options].some(o=>o.value===selected&&!o.disabled))select.value=selected;
  updateCampaignDatabaseOptions();
}
function updateCampaignDatabaseOptions(){
  const engine=$('#campaign-engine')?.value,select=$('#campaign-database');if(!select)return;
  const selected=select.value,profiles=batchState.databases.filter(p=>p.engine===engine),capability=batchEngineCapabilities().find(c=>(c.id||c.engine)===engine);
  select.innerHTML=engine==='reference'?'<option value="">参考回放无需数据库</option>':`<option value="">请选择预检通过的配置</option>`+profiles.map(p=>{const allowed=p.ready&&p.campaignCapability?.available!==false;return `<option value="${escapeHTML(p.id)}" ${allowed?'':'disabled'}>${escapeHTML(p.name||p.id)} · ${escapeHTML(p.release||'')} ${allowed?'':p.ready?'· 批次入口待配置':'· 路径未就绪'}</option>`;}).join('');
  if([...select.options].some(o=>o.value===selected&&!o.disabled))select.value=selected;
  $('#campaign-engine-note').textContent=engine==='reference'?'公开结果回放，用于验证任务流转；不执行 MSA 或新结构预测。':capability?.reason||capability?.description||'输入准备与 GPU 预测分别执行；需要已安装的权重、数据库及运行器。';
  updateCampaignSearchStrategy();
}

function updateCampaignSearchStrategy(){
  const root=$('#campaign-search-strategy');if(!root)return;
  const engine=$('#campaign-engine')?.value,selected=$('#campaign-database')?.value;
  const profile=batchState.databases.find(p=>p.id===selected&&p.engine===engine);
  const markup=engine==='reference'?'':profile?(window.renderMSASearchStrategy?.(profile,'selection')||''):'<p class="batch-helper">选择数据库后查看本次导入将绑定的搜索策略。</p>';
  if(root._strategyMarkup!==markup){root.innerHTML=markup;root._strategyMarkup=markup;hydrateIcons(root);}
}

function campaignInputHelp(){
  return batchInput.mode==='fixed-target'?'N 条候选 → N 个完整任务；每项包含全部固定链 + 1 条候选。AF3 可复用相同固定链的准备结果。此模式只接受 FASTA。':'FASTA：每条记录是独立目标。多链复合物使用 JSONL，每行包含 targetId、chains。每 100 项上传一块，不把整个文件读入内存。';
}
function campaignResidueLimit(){const value=Number(batchState.config?.maxResiduesPerTarget);return Number.isSafeInteger(value)&&value>0?value:5000;}
function campaignResidueLimitLabel(){const value=Number(batchState.config?.maxResiduesPerTarget),configured=Number.isSafeInteger(value)&&value>0;return `${configured?'服务当前每目标上限':'未获取服务上限，按默认值预检'} ${formatCount(campaignResidueLimit())} 残基。`;}
function parseCampaignFixedTarget(text,maxResidues=campaignResidueLimit()){
  const records=[];let name=null,sequence='';
  const add=()=>{if(name===null)return;if(!sequence)throw new Error(`固定靶标 ${name} 缺少序列`);if(!/^[ACDEFGHIKLMNPQRSTVWY]+$/.test(sequence))throw new Error(`固定靶标 ${name} 仅接受标准 20 种氨基酸`);records.push({id:String.fromCharCode(65+records.length),sequence});if(records.length>25)throw new Error('最多 25 条固定链，需为候选保留第 26 个链 ID');};
  for(const raw of String(text).replace(/^\uFEFF/,'').split(/\r?\n/)){
    const line=raw.trim();if(!line)continue;
    if(line.startsWith('>')){add();name=line.slice(1).trim();sequence='';if(!name)throw new Error('固定靶标 FASTA 标题不能为空');}
    else{if(name===null)throw new Error('固定靶标需要以 >标题 开始的 FASTA');sequence+=line.replace(/\s+/g,'').toUpperCase();}
  }
  add();if(!records.length)throw new Error('请先填写固定靶标 FASTA');
  if(records.reduce((n,c)=>n+c.sequence.length,0)>=maxResidues)throw new Error(`固定链合计须少于 ${formatCount(maxResidues)} 残基，为候选留出空间；上限按当前服务配置预检`);
  return records;
}
async function campaignInputSnapshot(mode,fixedText){
  if(!['independent','fixed-target'].includes(mode))throw new Error('未知的输入构造模式');
  const maxResidues=campaignResidueLimit(),fixedChains=mode==='fixed-target'?parseCampaignFixedTarget(fixedText,maxResidues):[];
  const bytes=new TextEncoder().encode(JSON.stringify(fixedChains)),digest=await crypto.subtle.digest('SHA-256',bytes);
  return {mode,fixedChains,maxResidues,context:{version:1,mode,chainIdPolicy:mode==='fixed-target'?'fixed-target-plus-candidate-v1':'independent-v1',fixedTargetChainsSha256:[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('')}};
}
function campaignInputContextKey(id){return `foldspace.campaign-input.${id}`;}
function checkCampaignInputContext(id,context){
  let saved;try{const raw=localStorage.getItem(campaignInputContextKey(id));saved=raw?JSON.parse(raw):null;}catch{throw new Error('无法读取此批次的导入上下文，请恢复浏览器存储后再继续');}
  if(!saved){if(context.mode==='fixed-target')throw new Error('此批次没有本浏览器保存的固定靶标指纹，请在原浏览器恢复，或新建筛选批次');return;}
  if(saved.version!==context.version||saved.mode!==context.mode||saved.chainIdPolicy!==context.chainIdPolicy||saved.fixedTargetChainsSha256!==context.fixedTargetChainsSha256)throw new Error('输入构造已改变：请恢复原输入模式和固定靶标后继续。更换靶标请新建批次，不能追加到当前批次');
}
function saveCampaignInputContext(id,context){
  try{const serialized=JSON.stringify(context);localStorage.setItem(campaignInputContextKey(id),serialized);if(localStorage.getItem(campaignInputContextKey(id))!==serialized)throw new Error('not persisted');}
  catch{throw new Error('无法保存导入上下文，已停止上传。请允许本浏览器的本地存储后重试，以便中断续传时核对固定靶标');}
}
async function* campaignConstructedItems(file,snapshot){
  if(snapshot.mode==='fixed-target'&&/\.(jsonl|ndjson)$/i.test(file.name))throw new Error('固定靶标筛选只接受候选 FASTA；已有多链 JSONL 请使用独立目标模式');
  const fixedLength=snapshot.fixedChains.reduce((n,c)=>n+c.sequence.length,0);
  for await(const item of campaignFileItems(file)){
    if(snapshot.mode==='independent'){yield item;continue;}
    const candidate=item.chains[0].sequence;
    if(!/^[ACDEFGHIKLMNPQRSTVWY]+$/.test(candidate))throw new Error(`候选 ${item.name} 仅接受标准 20 种氨基酸`);
    if(candidate.length+fixedLength>snapshot.maxResidues)throw new Error(`候选 ${item.name} 与固定链合计 ${formatCount(candidate.length+fixedLength)} 残基，超过当前预检上限 ${formatCount(snapshot.maxResidues)}`);
    yield {...item,chains:[...snapshot.fixedChains.map(c=>({...c})),{id:String.fromCharCode(65+snapshot.fixedChains.length),sequence:candidate}]};
  }
}
function renderCampaignInputPreview(){
  const preview=batchInput.preview;
  if(!preview)return batchInput.mode==='fixed-target'?'<span class="campaign-preview-kicker">FIXED TARGET × CANDIDATES</span><p>填写固定靶标并选择候选文件，预览前两项组合。</p>':'';
  if(preview.error)return `<p class="campaign-preview-error">${escapeHTML(preview.error)}</p>`;
  if(preview.loading)return '<p>正在流式读取前两项…</p>';
  return `<span class="campaign-preview-kicker">${preview.mode==='fixed-target'?'FIXED TARGET × CANDIDATES':'INPUT PREVIEW'}</span>${preview.mode==='fixed-target'?`<strong>${preview.fixedCount} 条固定链 + 每项 1 条候选</strong><span>固定链 ${formatCount(preview.fixedResidues)} aa · 候选链 ID ${escapeHTML(preview.candidateId)}<br>完整目标上限 ${formatCount(preview.maxResidues)} aa</span>`:''}${preview.items.length?`<ol>${preview.items.map(item=>`<li><b>${escapeHTML(item.name||item.targetId)}</b><span>${item.chains.length} 条链 · ${formatCount(item.chains.reduce((n,c)=>n+String(c.sequence||'').length,0))} aa · ${escapeHTML(item.chains.map(c=>c.id).join(' + '))}</span></li>`).join('')}</ol><small>仅预览前 ${preview.items.length} 项；任务总数在流式导入时累计。每 100 项上传一块。</small>`:'<p>选择候选文件后预览组合；不会一次读入完整文件。</p>'}`;
}
async function previewCampaignInput(){
  const generation=++batchInput.previewGeneration,file=$('#campaign-file')?.files[0],mode=batchInput.mode,fixedText=batchInput.fixedText;
  const paint=()=>{const el=$('#campaign-input-preview');if(el)el.innerHTML=renderCampaignInputPreview();};
  try{
    if(mode==='independent'&&!file){batchInput.preview=null;paint();return;}
    batchInput.preview={loading:true};paint();
    const snapshot=await campaignInputSnapshot(mode,fixedText),items=[];
    if(file)for await(const item of campaignConstructedItems(file,snapshot)){items.push(item);if(items.length===2)break;}
    if(generation!==batchInput.previewGeneration)return;
    batchInput.preview={mode,items,maxResidues:snapshot.maxResidues,fixedCount:snapshot.fixedChains.length,fixedResidues:snapshot.fixedChains.reduce((n,c)=>n+c.sequence.length,0),candidateId:String.fromCharCode(65+snapshot.fixedChains.length)};paint();
  }catch(error){if(generation!==batchInput.previewGeneration)return;batchInput.preview={error:error.message};paint();}
}
function syncCampaignInputMode(){
  const fixed=batchInput.mode==='fixed-target';$('#campaign-fixed-fields').hidden=!fixed;
  $('#campaign-file-label').textContent=fixed?'候选 FASTA 文件':'多记录 FASTA / JSONL 文件';
  $('#campaign-file').accept=fixed?'.fa,.fasta,.faa,.fas':'.fa,.fasta,.faa,.fas,.jsonl,.ndjson';
  $('#campaign-input-help').textContent=campaignInputHelp();
}
function lockCampaignInputForm(locked){
  document.querySelectorAll('#campaign-form input,#campaign-form select,#campaign-form textarea,#campaign-submit').forEach(el=>{el.disabled=locked;});
}
function bindCampaignViewEvents(){
  $('#refresh-databases')?.addEventListener('click',()=>refreshCampaignData());
  if(state.view!=='campaigns')return;
  updateCampaignEngineOptions();bindCampaignMonitorEvents();
  $('#campaign-engine')?.addEventListener('change',()=>{updateCampaignDatabaseOptions();$('#campaign-samples').value=$('#campaign-engine').value==='alphafold3'?'5':'1';});
  $('#campaign-database')?.addEventListener('change',updateCampaignSearchStrategy);
  $('#campaign-input-mode')?.addEventListener('change',e=>{batchInput.mode=e.target.value;batchInput.preview=null;syncCampaignInputMode();previewCampaignInput();});
  $('#campaign-fixed-target')?.addEventListener('input',e=>{batchInput.fixedText=e.target.value;clearTimeout(batchInput.previewTimer);batchInput.previewTimer=setTimeout(previewCampaignInput,250);});
  $('#campaign-file')?.addEventListener('change',()=>previewCampaignInput());
  $('#campaign-form')?.addEventListener('submit',submitCampaignFile);
  if(batchState.importing)lockCampaignInputForm(true);
}
function bindCampaignMonitorEvents(){
  $('#campaign-cluster-link')?.addEventListener('click',()=>navigate('cluster'));
  $('#refresh-campaign')?.addEventListener('click',()=>refreshCampaignData(true));
  $('#campaign-select')?.addEventListener('change',async e=>{batchState.selectedId=e.target.value;batchState.offset=0;batchOperation.confirmation=null;localStorage.setItem('foldspace.campaign',batchState.selectedId);await refreshCampaignData();});
  $('#campaign-stage-filter')?.addEventListener('change',async e=>{batchState.stage=e.target.value;batchState.offset=0;await refreshCampaignData();});
  $('#campaign-score-sort')?.addEventListener('change',async e=>{batchState.sort=e.target.value;batchState.offset=0;await refreshCampaignData();});
  document.querySelectorAll('[data-score-filter]').forEach(el=>el.addEventListener('click',async()=>{const value=Number(el.dataset.scoreFilter);batchState.minIptm=batchState.minIptm===value?null:value;batchState.offset=0;await refreshCampaignData();}));
  document.querySelectorAll('[data-stage-filter]').forEach(el=>el.addEventListener('click',async()=>{batchState.stage=batchState.stage===el.dataset.stageFilter?'':el.dataset.stageFilter;batchState.offset=0;await refreshCampaignData();}));
  $('#campaign-prev')?.addEventListener('click',async()=>{batchState.offset=Math.max(0,batchState.offset-25);await refreshCampaignData();});
  $('#campaign-next')?.addEventListener('click',async()=>{batchState.offset+=25;await refreshCampaignData();});
  document.querySelectorAll('[data-campaign-action]').forEach(el=>el.addEventListener('click',()=>runCampaignAction(el.dataset.campaignId,el.dataset.campaignAction)));
  $('#campaign-cancel-back')?.addEventListener('click',dismissCampaignCancellation);
  $('#campaign-cancel-apply')?.addEventListener('click',e=>runCampaignAction(e.currentTarget.dataset.campaignId,'cancel',true));
  $('.campaign-cancel-confirm')?.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();dismissCampaignCancellation();}});
  $('#campaign-dismiss-operation-error')?.addEventListener('click',()=>{batchOperation.error=null;renderCampaignMonitorRegion();});
  document.querySelectorAll('[data-campaign-item]').forEach(el=>el.addEventListener('click',()=>selectCampaignItem(el.dataset.campaignItem)));
  document.querySelectorAll('[data-inspect-target]').forEach(el=>el.addEventListener('click',()=>inspectCampaignTarget(batchState.selectedId,el.dataset.inspectTarget)));
}

function showCampaignStructure(item,c){
  state.selectedId=null;state.job={...item,...c.settings,name:item.name||item.targetId,id:item.id,engine:c.engine,status:item.stage==='completed'?'completed':item.stage,stage:item.stage,progress:item.stage==='completed'?100:0,chains:item.chains||item.input?.chains||[],result:item.result,events:item.events||[]};navigate('studio');
}
async function selectCampaignItem(id){
  try{const c=batchState.campaign,item=await api(`/api/campaigns/${c.id}/items/${id}`);showCampaignStructure(item,c);}catch(e){toast(e.message,true);}
}

async function* fileLines(file){
  const reader=file.stream().getReader(),decoder=new TextDecoder();let pending='';
  try{for(;;){const {done,value}=await reader.read();pending+=decoder.decode(value||new Uint8Array(),{stream:!done});let pos;while((pos=pending.indexOf('\n'))>=0){yield pending.slice(0,pos).replace(/\r$/,'');pending=pending.slice(pos+1);}if(pending.length>2*1024*1024)throw new Error('单行超过 2 MiB，请按行拆分序列');if(done)break;}if(pending)yield pending.replace(/\r$/,'');}finally{await reader.cancel();reader.releaseLock();}
}
async function stableTargetId(index,name){const bytes=new TextEncoder().encode(`${index}\0${name}`),digest=await crypto.subtle.digest('SHA-256',bytes);return 't-'+[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('').slice(0,24);}
async function* campaignFileItems(file){
  let index=0;
  if(/\.(jsonl|ndjson)$/i.test(file.name)){for await(const line of fileLines(file)){if(!line.trim())continue;index++;let item;try{item=JSON.parse(line);}catch{throw new Error(`JSONL 第 ${index} 项不是有效 JSON`);}if(!item||typeof item!=='object'||Array.isArray(item)||!Array.isArray(item.chains))throw new Error(`JSONL 第 ${index} 项需要 chains 数组`);yield {...item,targetId:item.targetId||await stableTargetId(index,item.name||`target_${index}`)};}if(!index)throw new Error('文件没有有效目标');return;}
  let name=null,sequence='';
  for await(const line of fileLines(file)){
    if(!line.trim())continue;
    if(line.startsWith('>')){if(name!==null){index++;if(!sequence)throw new Error(`FASTA ${name} 缺少序列`);yield {targetId:await stableTargetId(index,name),name:name.slice(0,120),chains:[{id:'A',sequence}]};}name=line.slice(1).trim();if(!name)throw new Error('FASTA 标题不能为空');sequence='';}
    else{if(name===null)throw new Error('FASTA 必须以 >标题 开始');sequence+=line.replace(/\s+/g,'').toUpperCase();if(sequence.length>100000)throw new Error(`FASTA ${name} 超过 100000 残基`);}
  }
  if(name!==null){index++;if(!sequence)throw new Error(`FASTA ${name} 缺少序列`);yield {targetId:await stableTargetId(index,name),name:name.slice(0,120),chains:[{id:'A',sequence}]};}
  if(!index)throw new Error('文件没有有效目标');
}

async function submitCampaignFile(event){
  event.preventDefault();if(batchState.importing)return;
  const file=$('#campaign-file').files[0],button=$('#campaign-submit'),errorEl=$('#campaign-form-error');if(!file)return;
  const append=$('#campaign-append').checked,stream=$('#campaign-stream').checked;
  const form={mode:$('#campaign-input-mode').value,fixedText:$('#campaign-fixed-target').value,name:$('#campaign-name').value,engine:$('#campaign-engine').value,databaseProfile:$('#campaign-database').value,placement:$('#campaign-placement').value,seeds:$('#campaign-seeds').value,samples:Number($('#campaign-samples').value),campaign:batchState.campaign};
  batchState.importing=true;lockCampaignInputForm(true);errorEl.textContent='';batchState.progress={read:0,accepted:0,duplicates:0,message:'检查输入构造…'};let id=null;
  const update=()=>{const el=$('#campaign-import-progress');if(el)el.innerHTML=renderImportProgress();};
  update();
  try{
    const snapshot=await campaignInputSnapshot(form.mode,form.fixedText);
    if(snapshot.mode==='fixed-target'&&(append?form.campaign?.engine:form.engine)==='reference')throw new Error('固定靶标组合需要配置真实预测引擎；公开参考回放不能计算新组合');
    if(snapshot.mode==='fixed-target'&&/\.(jsonl|ndjson)$/i.test(file.name))throw new Error('固定靶标筛选只接受候选 FASTA；已有多链 JSONL 请使用独立目标模式');
    if(append){const c=form.campaign;if(!c||c.sealed||c.status==='cancelled')throw new Error('请选择一个未封存、未取消的批次继续导入');checkCampaignInputContext(c.id,snapshot.context);id=c.id;}
    else{
      const seeds=form.seeds.split(/[,\s]+/).filter(Boolean).map(Number);if(!seeds.length||seeds.some(n=>!Number.isSafeInteger(n)||n<0))throw new Error('种子需要是非负整数');
      const {engine,databaseProfile}=form;if(engine!=='reference'&&!databaseProfile)throw new Error('真实预测需要选择预检通过的数据库配置');
      const campaign=await api('/api/campaigns',{method:'POST',body:JSON.stringify({name:form.name,engine,placement:form.placement,databaseProfile:databaseProfile||'reference',settings:{seeds,numSamples:form.samples,numRecycles:engine==='alphafold3'?10:3,example:'ubiquitin-afdb'},executionProfile:{},status:'paused'})});id=campaign.id;batchState.campaign=campaign;
    }
    saveCampaignInputContext(id,snapshot.context);
    batchState.selectedId=id;batchState.offset=0;localStorage.setItem('foldspace.campaign',id);
    if(stream)await api(`/api/campaigns/${id}/resume`,{method:'POST',body:'{}'});
    let chunk=[];
    const flush=async()=>{if(!chunk.length)return;const result=await api(`/api/campaigns/${id}/items`,{method:'POST',body:JSON.stringify({items:chunk})});batchState.progress.accepted+=result.count??chunk.length;batchState.progress.duplicates+=result.duplicates||0;batchState.progress.message='已确认记录保存在 SQLite，正在继续读取…';chunk=[];update();};
    for await(const item of campaignConstructedItems(file,snapshot)){chunk.push(item);batchState.progress.read++;if(chunk.length===100)await flush();}
    await flush();await api(`/api/campaigns/${id}/seal`,{method:'POST',body:'{}'});await api(`/api/campaigns/${id}/resume`,{method:'POST',body:'{}'});
    batchState.progress.message='输入清单已封存，管线已启动。';toast(`已接收 ${formatCount(batchState.progress.accepted)} 项，开始跟踪批次`);
  }catch(error){errorEl.textContent=error.message;batchState.progress.message='导入中断，已确认记录保留；选择同一文件继续未封存批次。';if(id)try{await api(`/api/campaigns/${id}/pause`,{method:'POST',body:'{}'});}catch{} }
  finally{batchState.importing=false;lockCampaignInputForm(false);update();await refreshCampaignData();}
}
