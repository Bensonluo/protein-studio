'use strict';

// Observed preparation states and raw A3M summaries. This module never searches
// a database, reads sequences, estimates GPU use, or infers an unrecorded phase.
(()=>{
  const states={searching:'搜索与模板准备中',waiting_shared:'等待共享链',assembling:'组装输入中',prepared:'准备已完成'};
  const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
  const count=value=>number(value)?value.toLocaleString('zh-CN'):'未记录';
  const ratio=value=>number(value)&&value<=1;
  const percent=value=>ratio(value)?`${(value*100).toLocaleString('zh-CN',{maximumFractionDigits:1})}%`:'—';
  const esc=value=>escapeHTML(value);
  function time(value){if(value===null||value===undefined||value==='')return '未记录';const date=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(date.valueOf())?'未记录':date.toLocaleString('zh-CN',{hour12:false});}
  function fact(label,value){return `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`;}
  const strategyLabels={searchBackend:{'mmseqs2-colabfold':'MMseqs2 · ColabFold 搜索流程'},searchDevice:{cpu:'CPU',gpu:'GPU'},pairedCandidateSource:{none:'显式空候选', 'native-uniprot':'AF3 原生 UniProt 搜索'},templateStrategy:{none:'显式空模板', 'native-af3':'AF3 原生模板搜索'},databaseFormat:{'colabfold-expandable':'ColabFold expandable 数据库','colabfold-gpu':'ColabFold GPU 数据库'}};
  function declared(strategy,key){const value=strategy[key];return typeof value==='string'&&value?strategyLabels[key]?.[value]||value:'未声明';}
  function searchStrategy(profile,context='database'){
    if(profile?.engine==='reference')return '';
    const strategy=profile&&typeof profile==='object'?profile.preparationStrategy:null;
    const source={database:'注册配置',selection:'本次导入将绑定',campaign:'批次绑定快照',target:'目标绑定快照'}[context]||'注册配置';
    if(!strategy||typeof strategy!=='object')return `<section class="msa-strategy msa-strategy-unknown" data-msa-strategy-context="${esc(context)}"><span class="msa-strategy-source">${source}</span><p>此配置尚未记录搜索后端与准备策略。</p></section>`;
    const state=strategy.status==='preflight-ready'?'路径与命令预检通过':strategy.status==='unavailable'?'待配置':'预检状态未记录';
    const capability=profile.campaignCapability;
    const reasons=Array.isArray(capability?.diagnostics)?capability.diagnostics.slice(0,3).map(d=>typeof d==='string'?d:d?.message||'配置需检查').join('；'):'';
    const admission=capability?.available===false?`<p class="msa-strategy-admission pending">批次入口待配置${reasons?'：'+esc(reasons):''}</p>`:capability?.available===true?'<p class="msa-strategy-admission">批次入口可用 · 已检查所选配置的准备命令与资源声明</p>':'';
    const hint=strategy.searchDevice==='gpu'?'MMseqs2 搜索阶段领取专用 GPU；释放后，CPU 继续完成后续准备。':strategy.searchDevice==='cpu'?'搜索与后续准备在 CPU 执行；完成后进入结构预测队列。':'按注册运行器生成完整预测输入。';
    const searchScope=strategy.searchBackend==='mmseqs2-colabfold'?'MMseqs2 生成未配对 MSA。 ':'';
    const ablation=strategy.pairedCandidateSource==='none'||strategy.templateStrategy==='none'?' 显式空候选或空模板属于输入消融，需结合结构质量选用。':'';
    const pairing=strategy.pairedCandidateSource==='native-uniprot'?'原始 UniProt 候选在完整目标特征处理时完成跨链配对。':strategy.pairedCandidateSource==='none'?'本策略将 paired MSA 显式设为空。':'';
    return `<section class="msa-strategy" data-msa-strategy-context="${esc(context)}" data-msa-strategy-id="${esc(strategy.id||'')}"><div class="msa-strategy-heading"><span class="msa-strategy-source">${source}</span><span class="msa-strategy-status ${strategy.status==='preflight-ready'?'ready':'pending'}">${state}</span></div><div class="msa-strategy-backend"><strong>${esc(declared(strategy,'searchBackend'))}</strong><span class="msa-strategy-device">${esc(declared(strategy,'searchDevice'))}</span></div><dl class="msa-strategy-facts">${fact('原始配对候选来源',declared(strategy,'pairedCandidateSource'))}${fact('模板策略',declared(strategy,'templateStrategy'))}${fact('数据库格式',declared(strategy,'databaseFormat'))}${fact('运行器配置',strategy.runnerConfigured===true?'命令已配置':strategy.runnerConfigured===false?'待配置':'未记录')}</dl>${admission}<p class="msa-strategy-note">${searchScope}${hint}${ablation}${pairing?' '+pairing:''}</p><details class="msa-strategy-identity"><summary>策略与版本</summary><dl class="msa-strategy-facts">${fact('策略 ID',strategy.id||'未声明')}${fact('搜索版本',profile.searchVersion||'未声明')}${fact('准备版本',profile.prepareVersion||'未声明')}${fact('预检范围','元数据、资产与命令路径')}</dl></details></section>`;
  }
  function progressTitle(p){return p.state==='waiting_shared'&&p.waitReason==='search_gpu'?'等待搜索 GPU':states[p.state];}
  function overview(c){
    if(c.engine==='reference')return '<section class="msa-reference-note" aria-label="MSA 观测"><span>'+icon('book')+'</span><div><strong>公开参考回放</strong><p>本批次不执行 MSA 搜索；没有可展示的搜索耗时或 A3M 深度记录。</p></div></section>';
    const counts=c.preparationStateCounts,known=counts&&['searching','waitingShared','assembling'].every(key=>number(counts[key]));
    return `<section class="msa-overview" aria-label="MSA 准备态势"><div class="msa-overview-heading"><div><span class="msa-eyebrow">PREPARATION · OBSERVED STATE</span><h4>共享链先准备，其他目标继续流动。</h4></div><span class="msa-count-unit">当前目标数</span></div>${known?`<dl class="msa-state-cards">${[['searching','搜索与模板准备','search'],['waitingShared','等待准备依赖','clock'],['assembling','组装输入','layers']].map(([key,label,ic])=>`<div data-msa-state="${key}"><dt>${icon(ic)}${label}</dt><dd>${count(counts[key])}</dd></div>`).join('')}</dl>`:'<p class="msa-not-recorded">当前接口尚无 MSA 子阶段观测记录。</p>'}<p class="msa-overview-note">${c.status==='paused'?'批次已暂停，等待记录保留。 ':''}当前目标状态用于观察准备瓶颈；下方展示已发布搜索与复用记录。</p></section>`;
  }
  function progress(item,c){
    if(c.engine==='reference')return '';
    const p=item.preparationProgress;if(!p||!states[p.state])return '';
    const waiting=p.state==='waiting_shared',gpuWait=waiting&&p.waitReason==='search_gpu',ids=Array.isArray(p.chainIds)?p.chainIds.join(' / '):null;
    const description=gpuWait?'专用搜索 GPU 正忙；本目标已释放准备槽位，资源可用后重新领取。':{searching:'运行器正在按绑定策略搜索 MSA 并准备模板。',waiting_shared:'共享链正在准备；本目标已释放准备槽位，其他任务继续执行。',assembling:'正在将已准备链组装为本目标的完整模型输入。',prepared:'本目标的准备输入已发布；结构预测与验证继续由后续阶段完成。'}[p.state];
    return `<section class="campaign-inspect-section msa-progress ${waiting?'waiting':''}" data-msa-progress="${p.state}" ${p.waitReason?`data-msa-wait-reason="${esc(p.waitReason)}"`:''}><div class="msa-progress-heading"><span>${icon(waiting?'clock':p.state==='prepared'?'check':'search')}</span><h3>${progressTitle(p)}</h3></div><p>${description}</p><dl class="msa-progress-facts">${fact('状态记录时间',time(p.updatedAt))}${ids?fact('当前链',ids):''}${waiting&&p.retryAfter!=null?fact('下次可尝试领取',time(p.retryAfter)):''}${number(p.deferCount)?fact(gpuWait?'资源等待暂缓次数':'共享依赖暂缓次数',count(p.deferCount)):''}</dl>${p.chainKey?`<details class="msa-source"><summary>链准备缓存键</summary><code>${esc(p.chainKey)}</code></details>`:''}</section>`;
  }
  function pill(item){const p=item.preparationProgress;return p&&states[p.state]&&p.state!=='prepared'?`<span class="msa-progress-pill ${esc(p.state)}">${progressTitle(p)}</span>`:'';}
  function coverageGraphic(q){
    const v=q.coverage||{},marks=['p10','p50','p90','mean'].filter(key=>ratio(v[key]));
    if(!marks.length)return '<div class="msa-coverage-empty">没有非 query 行，或尚无覆盖率统计。</div>';
    const x=value=>12+276*value;
    const band=ratio(v.p10)&&ratio(v.p90)?`<line x1="${x(v.p10)}" x2="${x(v.p90)}" y1="19" y2="19" class="msa-quantile-band"/>`:'';
    const shapes=marks.map(key=>key==='mean'?`<path data-msa-marker="mean" d="M${x(v[key])} 10l6 9-6 9-6-9Z" class="msa-mean-marker"/>`:`<line data-msa-marker="${key}" x1="${x(v[key])}" x2="${x(v[key])}" y1="${key==='p50'?9:13}" y2="${key==='p50'?29:25}" class="msa-quantile-marker ${key==='p50'?'median':''}"/>`).join('');
    return `<svg class="msa-coverage-plot" viewBox="0 0 300 38" preserveAspectRatio="none" role="img" aria-label="候选行对 query 的覆盖率：${marks.map(key=>key.toUpperCase()+' '+percent(v[key])).join('；')}"><line x1="12" x2="288" y1="19" y2="19" class="msa-coverage-baseline"/>${band}${shapes}</svg><div class="msa-coverage-axis"><span>0%</span><span>竖线 P10 / P50 / P90 · 菱形 均值</span><span>100%</span></div>`;
  }
  function quality(q,key){
    const title=key==='paired'?'原始配对候选':'未配对 MSA';
    if(!q||q.schemaVersion!==1)return `<section class="msa-quality-card" data-msa-role="${key}"><div class="msa-quality-heading"><h4>${title}</h4><span>未统计</span></div><p class="msa-quality-note">此链缓存没有该类 MSA 摘要。</p></section>`;
    const status={'available':'已有摘要','query-only':'仅 query','explicit-empty':'声明为空'}[q.status]||'状态未记录',v=q.coverage||{},hasHits=number(q.hitRows)&&q.hitRows>0;
    const notice=q.status==='explicit-empty'?'该字段声明为空，包含 0 条 A3M 记录。':q.status==='query-only'?'仅含 query 行，候选行覆盖率未定义。':'候选行保留重复，深度按原始记录计数。';
    return `<section class="msa-quality-card" data-msa-role="${key}"><div class="msa-quality-heading"><h4>${title}</h4><span data-msa-status="${esc(q.status)}">${status}</span></div><dl class="msa-depth-stats">${fact('原始行数 · 含 query',count(q.rawRows))}${fact('非 query 行 · 保留重复',count(q.hitRows))}${fact('Query 长度',count(q.queryLength))}${fact('观测对齐列数',count(q.alignedQueryLength))}</dl><p class="msa-quality-note">${notice}${key==='paired'?' 原始候选在完整目标特征处理时完成跨链配对。':''}</p><div class="msa-coverage-heading">候选行对 query 的覆盖率</div>${hasHits?coverageGraphic(q):'<div class="msa-coverage-empty">无非 query 行 · 覆盖率未定义</div>'}<dl class="msa-coverage-stats">${[['mean','均值'],['p10','P10'],['p50','P50'],['p90','P90']].map(([field,label])=>fact(label,hasHits?percent(v[field]):'—')).join('')}</dl><p class="msa-coverage-definition">每条非 query 行的非 gap 对齐位置数 ÷ query 长度；小写插入不占对齐列。覆盖率按对齐位置计，分位数使用 nearest-rank。</p><details class="msa-qc-more"><summary>Gap、插入与制品来源</summary><dl class="msa-progress-facts">${fact('Gap 比例',hasHits?percent(q.gapFraction):'—')}${fact('全覆盖候选行',hasHits?count(v.fullRows):'—')}${fact('零覆盖候选行',hasHits?count(v.zeroRows):'—')}${fact('插入残基数',count(q.insertedResidues))}${fact('非标准对齐残基数',count(q.nonStandardAlignedResidues))}${fact('Query 行存在',q.queryPresent===true?'是':q.queryPresent===false?'否':'未记录')}${fact('来源 UTF-8 字节数',count(q.sourceUtf8Bytes))}</dl>${q.sourceSha256?`<div class="msa-source-hash"><span>来源 SHA-256</span><code>${esc(q.sourceSha256)}</code></div>`:''}</details></section>`;
  }
  function chain(entry,whole){
    const data=entry.msaQuality,seconds=entry.searchProcessWallSeconds,hasQuality=data?.schemaVersion===1;
    const label=number(seconds)?`${seconds.toLocaleString('zh-CN',{maximumFractionDigits:3})} s`:'未记录';
    return `<div class="msa-chain-quality"><div class="msa-generation-time"><span>${icon('clock')}此链缓存首次生成 · 搜索与模板程序墙钟</span><strong>${label}</strong></div><p class="msa-history-note">${whole?'本次复用整目标准备包，展示初次组装引用的链制品。':entry.cacheHit?'本次链缓存命中。':''}记录链制品创建时的搜索与模板程序耗时；命中时直接复用该制品。</p>${hasQuality?`<div class="msa-quality-grid">${quality(data.unpaired,'unpaired')}${quality(data.paired,'paired')}</div>`:'<p class="msa-not-recorded">该链缓存尚无 MSA 质量摘要。</p>'}</div>`;
  }
  window.renderMSAOverview=overview;window.renderMSAProgress=progress;window.renderMSAProgressPill=pill;window.renderMSAChainQuality=chain;window.renderMSASearchStrategy=searchStrategy;
})();
