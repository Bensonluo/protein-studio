'use strict';

const icons = {
  molecule:'<circle cx="6" cy="7" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="m8 8 3 7m3 0 3-7M9 7h6"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  workflow:'<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h6a3 3 0 0 1 3 3v6M6 9v9h9"/>',
  book:'<path d="M12 5v15M3 4c4-1 6 0 9 1 3-1 5-2 9-1v15c-4-1-6 0-9 1-3-1-5-2-9-1V4Z"/>',
  server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
  chevrons:'<path d="m8 8 4-4 4 4m-8 8 4 4 4-4"/>',help:'<circle cx="12" cy="12" r="9"/><path d="M9.8 8.4a2.4 2.4 0 0 1 4.6 1c0 1.6-2.4 2-2.4 3.6m0 3h.01"/>',
  upload:'<path d="M12 16V3m-4 4 4-4 4 4M4 15v5h16v-5"/>',plus:'<path d="M12 4v16M4 12h16"/>',x:'<path d="m6 6 12 12M6 18 18 6"/>',
  play:'<path d="m8 4 12 8-12 8V4Z"/>',pause:'<path d="M8 5v14M16 5v14"/>',download:'<path d="M12 3v13m-4-4 4 4 4-4M4 17v4h16v-4"/>',
  check:'<path d="m5 12 4 4L19 6"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  chain:'<path d="m9 15 6-6M8 12l-2 2a4 4 0 0 0 6 6l2-2m-2-12 2-2a4 4 0 0 1 6 6l-2 2"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4"/>',
  rotate:'<path d="M20 8a8 8 0 1 0 .5 7M20 3v5h-5"/>',camera:'<path d="M4 7h4l2-3h4l2 3h4v13H4V7Z"/><circle cx="12" cy="13" r="3"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',chart:'<path d="M3 3v18h18M6 15l4-5 4 3 6-8"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  file:'<path d="M5 3h9l5 5v13H5V3Zm9 0v6h5M8 13h8M8 17h6"/>',code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
  sparkles:'<path d="m12 3 2 6 6 3-6 2-2 7-2-7-6-2 6-3 2-6ZM21 2v4m-2-2h4"/>',arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',trash:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',cpu:'<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4M19 9h4m-4 6h4"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.molecule}</svg>`;
const $ = selector => document.querySelector(selector);
const escapeHTML = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'studio', jobs:[], selectedId:null, job:null, system:{engines:[]}, engines:[], example:null, selectedEngine:'reference', viewer:null, style:'cartoon', color:'confidence', spinning:false, light:false, selectedResidue:null, search:'', filter:'all', pending:false, autoSeeded:false, sources:[], error:null, engineDrafts:{}, parametersEngine:null, submitting:false, selectionRequest:0, modelIndex:0, modelJobId:null, jobsSort:'recent', lowConf:0, polling:false};
const statusLabels = {completed:'已完成',running:'运行中',queued:'排队中',failed:'失败',cancelled:'已取消'};
const engineNames = {reference:'参考结构',alphafold2:'AlphaFold 2',alphafold3:'AlphaFold 3',colabfold:'ColabFold',import:'结构导入'};
const views = {studio:['结构工作台','从序列，到结构。','连接预测、计算与发现，探索蛋白质的三维世界。'],jobs:['任务队列','每一次探索，都有迹可循。','管理预测任务，查看执行进展、参数与交付结果。'],pipeline:['预测管线','让科学计算，流动起来。','从输入校验到结果交付，连接每一个计算阶段。'],sources:['研究与依据','站在开放科学的肩膀上。','采用经过核查的官方实现，让技术选择与结果来源可追溯。'],environment:['运行环境','为真实计算，做好准备。','检查引擎可用状态，将工作台连接到你的 GPU 与预测环境。']};
views.campaigns=['批次管线','让十万次探索，持续流动。','流式导入、分阶段执行、有限缓冲与逐项交付，跟踪整个批次。'];
views.databases=['序列与模板库','让每一次搜索，有源可查。','检查序列库、模板库及索引的实际路径、版本与可读状态。'];
views.cluster=['混合云执行','让本地与云端，接力计算。','查看真实节点、常驻执行批次与可校验的数据传输。'];
views.storage=['存储与缓存','让复用持续，让空间可控。','查看准备缓存占用、受保护引用和具体回收计划。'];

const appBasePath = document.querySelector('meta[name="protein-studio-base-path"]')?.content || '';
function appUrl(path) {
  if (typeof path !== 'string') throw new TypeError('Application URL must be a string');
  return appBasePath && (path.startsWith('/api/') || path.startsWith('/worker/') || path === '/sources.json') ? appBasePath + path : path;
}

async function api(path, options={}) {
  const response = await fetch(appUrl(path), {...options, headers:{'Content-Type':'application/json',...options.headers}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : data.message || `请求失败 (${response.status})`);
  return data;
}
function toast(message, error=false) { const el=document.createElement('div');el.className=`toast${error?' error':''}`;el.textContent=message;$('#toast-container').append(el);setTimeout(()=>el.remove(),5500); }
function hydrateIcons(root=document) {root.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));}
function timeLabel(value) {if(!value)return '—';const d=new Date(typeof value==='number' && value<1e12?value*1000:value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
function statusPill(status) {return `<span class="status-pill ${escapeHTML(status)}"><i></i>${statusLabels[status]||escapeHTML(status)}</span>`;}
function getResidues(result=state.job?.result) {return Array.isArray(result?.residues)?result.residues:[];}
function getPlddt(result=state.job?.result) {if(Array.isArray(result?.plddt)&&result.plddt.length&&result.plddt.some(Number.isFinite))return result.plddt;const rs=getResidues(result);return rs.length&&rs.some(r=>Number.isFinite(r.plddt))?rs.map(r=>r.plddt):[];}
function viewerModels(job=state.job) {const models=job?.result?.rankedModels||[];if(state.modelJobId!==job?.id){state.modelJobId=job?.id;state.modelIndex=0;}return models;}
function viewerModelInfo(job=state.job) {const models=viewerModels(job);const index=Number.isInteger(state.modelIndex)&&state.modelIndex>0&&state.modelIndex<models.length?state.modelIndex:0;const model=index>0?models[index]:null;const url=model?.structureUrl||job?.result?.structureUrl||`/api/jobs/${encodeURIComponent(job?.id)}/structure`;return {models,index,model,url,format:String(model?.structureFormat||job?.result?.structureFormat||job?.result?.format||'pdb').toLowerCase()};}
function renderViewerTabline(job) {
  if(job.status!=='completed')return '<span class="subtle">INTERACTIVE VIEW</span>';
  const {models,index,url}=viewerModelInfo(job);
  const download=`<a class="text-button" href="${escapeHTML(appUrl(url))}" download>${icon('download')} 下载结构</a>`;
  if(models.length<2)return download;
  const options=models.map((model,i)=>{const metrics=[];if(Number.isFinite(model.meanPlddt))metrics.push(`pLDDT ${model.meanPlddt.toFixed(1)}`);if(Number.isFinite(model.rankingScore))metrics.push(`排名分 ${model.rankingScore.toFixed(3)}`);const label=`排名 ${model.rank??i+1} · ${model.name||'sample-'+i}${metrics.length?' · '+metrics.join(' · '):''}`;return `<option value="${i}" ${i===index?'selected':''}>${escapeHTML(label)}</option>`;}).join('');
  return `<select class="color-select model-select" id="model-select" aria-label="切换排名模型">${options}</select><span class="subtle model-scope-note" title="其他排名的曲线与着色按结构文件 B 因子（pLDDT 惯例）导出；PAE 仍对应排名 1 主结果">${index>0?'正在查看其他排名 · 曲线/着色按此采样，PAE 仍为主结果':`${models.length} 个采样模型`}</span>`+download;
}
function switchViewerModel(index) {
  const models=viewerModels();
  if(!Number.isInteger(index)||models.length<2||index<0||index>=models.length||state.modelIndex===index)return;
  state.modelIndex=index;state.modelCurve=null;renderMain();
}
function mean(values) {const v=values.filter(Number.isFinite);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null;}
function confidenceColor(value) {return value>=90?'#65a5e8':value>=70?'#66d0d8':value>=50?'#e7ce68':'#e59263';}
function summaryMetric(label,value,unit,iconName,note='',accent=false) {return `<div class="summary-card ${accent?'accent':''}"><div class="summary-label">${icon(iconName)}${label}</div><div class="summary-value">${escapeHTML(value)}<small>${escapeHTML(unit)}</small></div>${note?`<span class="summary-note">${escapeHTML(note)}</span>`:''}</div>`;}

function renderMain() {
  window.stopPAEPanel?.();
  if(state.view!=='cluster'&&window.stopClusterRefresh)window.stopClusterRefresh();
  if(state.view!=='storage'&&window.stopStorageRefresh)window.stopStorageRefresh();
  const [title, heading, subtitle]=views[state.view];
  $('#breadcrumb-current').textContent=title;$('#page-title').innerHTML=escapeHTML(heading.slice(0,-1))+'<span class="heading-dot">。</span>';$('#page-subtitle').textContent=subtitle;
  $('#new-job-button').innerHTML=icon('plus')+(['campaigns','cluster'].includes(state.view)?'新建批次':'新建预测');
  document.querySelectorAll('.nav-item').forEach(el=>{const active=el.dataset.view===state.view;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  if (state.viewer) {try{state.viewer.spin(false);state.viewer.clear();}catch{}state.viewer=null;}
  const root=$('#view-container');
  if(state.view==='studio') root.innerHTML=renderStudio();
  if(state.view==='jobs') root.innerHTML=renderJobs();
  if(state.view==='pipeline') root.innerHTML=renderPipeline();
  if(state.view==='sources') root.innerHTML=renderSources();
  if(state.view==='environment') root.innerHTML=renderEnvironment();
  if(state.view==='campaigns') root.innerHTML=renderCampaigns();
  if(state.view==='databases') root.innerHTML=renderDatabases();
  if(state.view==='cluster') root.innerHTML=window.renderCluster?window.renderCluster():'<div class="panel empty-page">正在载入节点执行界面…</div>';
  if(state.view==='storage') root.innerHTML=window.renderStorage?window.renderStorage():'<div class="panel empty-page">正在载入缓存生命周期界面…</div>';
  hydrateIcons(root);bindViewEvents();
  if(state.view==='studio' && state.job?.status==='completed' && state.job?.result) {loadStructure();drawPAE();}
}

function renderStudio() {
  const job=state.job, result=job?.result||{}, confidence=getPlddt(result), avg=result.meanPlddt ?? mean(confidence), residues=getResidues(result);
  const length=result.sequenceLength||residues.length||job?.chains?.reduce((n,c)=>n+(c.sequence?.length||0),0)||0;
  const chains=job?.chains?.length||new Set(residues.map(r=>r.chain)).size||0;
  const complete=state.jobs.filter(j=>j.status==='completed').length;
  const summary=`<div class="summary-grid">${summaryMetric('序列长度',length||'—','residues','chain',`${chains} 条蛋白链`)}${summaryMetric('平均置信度',avg!=null?Number(avg).toFixed(1):'—',avg!=null?'pLDDT':'等待预测指标','target',avg>=90?'高置信度':'',true)}${summaryMetric('工作空间任务',String(state.jobs.length),'experiments','layers',`${complete} 个已完成`)}${summaryMetric('当前运行引擎',job?engineNames[job.engine]||job.engine:'就绪','','cpu',job?.engine==='reference'?'公开结果回放':'')}</div>`;
  if(!job) return summary+`<div class="panel empty-page"><div class="loader"></div><h2>${state.error?'暂时未连接到服务':'正在准备结构工作台'}</h2><p>${escapeHTML(state.error||'首次启动将载入有来源的公开结构，展示完整分析体验。')}</p><button class="button button-primary" data-new-job>新建预测</button></div>`;
  const name=escapeHTML(job.name), source=result.provenance||{};
  const detail=`<div class="job-detail-bar"><div><strong>${name}</strong>${statusPill(job.status)}<span class="model-badge" style="margin-left:8px">${escapeHTML(engineNames[job.engine]||job.engine)}</span></div><div class="task-actions">${job.status==='running'||job.status==='queued'?`<button class="text-button" data-cancel="${job.id}">取消任务</button>`:''}${job.status==='failed'||job.status==='cancelled'?`<button class="text-button" data-retry="${job.id}">重新运行</button>`:''}<button class="text-button" id="export-job">导出任务 ${icon('download')}</button></div></div>`;
  if(job.resultAvailability?.status==='legacy-large-result')return summary+detail+window.renderResultAvailability(job);
  const viewerPanel=`<div class="panel viewer-panel"><div class="panel-heading"><h2>${icon('molecule')}三维结构<span class="structure-name">${name}</span></h2><div class="viewer-tabline">${renderViewerTabline(job)}</div></div><div class="viewer-container ${state.light?'light-viewer':''}" id="viewer-container"><div class="viewer-grid"></div><div id="molecule-viewer" class="viewer-canvas"></div><div class="viewer-loading" id="viewer-loading">${job.status==='completed'?'<div class="loader"></div><span>构建三维视图</span>':`<div class="${job.status==='running'?'loader':''}"></div><span>${escapeHTML(job.error||statusLabels[job.status]||'等待计算结果')}</span>`}</div><div class="viewer-topline"><span class="viewer-badge"><i></i>${job.engine==='reference'?'公开参考结构':job.engine==='import'?'导入结构':'预测结构'}</span><span class="viewer-chip">${length} RESIDUES · ${chains} CHAIN${chains>1?'S':''}</span></div><div class="viewer-actions"><button class="viewer-action ${state.spinning?'active':''}" id="rotate-view" title="自动旋转" aria-label="自动旋转">${icon('rotate')}</button><button class="viewer-action" id="reset-view" title="重置视角" aria-label="重置视角">${icon('target')}</button><button class="viewer-action" id="light-view" title="切换背景" aria-label="切换背景">${icon('sun')}</button><button class="viewer-action" id="snapshot-view" title="导出图片" aria-label="导出三维图片">${icon('camera')}</button><button class="viewer-action" id="fullscreen-view" title="全屏查看" aria-label="全屏查看">${icon('expand')}</button></div><div class="viewer-overlay-bottom"><svg class="axis" viewBox="0 0 40 40"><path d="M13 27V6M13 27h23M13 27 3 36" stroke-width="1" stroke="#8aa6a7"/><text x="10" y="5" font-size="6" fill="#a2c4b1">Y</text><text x="35" y="25" font-size="6" fill="#adc391">X</text><text x="1" y="39" font-size="6" fill="#7fa1b9">Z</text></svg><span class="viewer-instructions">拖动旋转 · 滚轮缩放 · 点击残基</span></div></div><div class="viewer-toolbar"><div class="segmented" aria-label="分子显示方式"><button data-style="cartoon" class="${state.style==='cartoon'?'active':''}">Cartoon</button><button data-style="stick" class="${state.style==='stick'?'active':''}">Ball & stick</button><button data-style="surface" class="${state.style==='surface'?'active':''}">Surface</button></div><select class="color-select" id="color-mode" aria-label="结构着色"><option value="confidence" ${state.color==='confidence'?'selected':''}>${confidence.length?'按 pLDDT 着色':'按链着色'}</option><option value="spectrum" ${state.color==='spectrum'?'selected':''}>N → C 光谱</option><option value="chain" ${state.color==='chain'?'selected':''}>按链着色</option></select><button class="text-button lowconf-toggle" id="lowconf-toggle" title="低置信区域显示（AF DB FAQ / PDBe-KB / PyMOL 惯例）：pLDDT<50 常为无结构带状、不应解读；PDBe-KB 查看器默认 <70 低不透明度；PyMOL 惯例隐藏 b<50。仅 Cartoon / Ball & stick 生效">低置信显示：${state.lowConf===1?'淡化 <70':state.lowConf===2?'隐藏 <50':'关'}</button></div><div class="viewer-legend">${confidence.length?`<div class="legend-stops"><span><i style="background:#65a5e8"></i>≥90</span><span><i style="background:#66d0d8"></i>70–90</span><span><i style="background:#e7ce68"></i>50–70</span><span><i style="background:#e59263"></i>&lt;50</span><span>pLDDT</span><span title="缺失置信度或无法精确对应残基"><i style="background:#8e9ca6"></i>未匹配</span></div>`:'<span>颜色表示链或序列位置</span>'}<span class="viewer-origin">${safeSourceLink(source)}</span></div></div>`;
  return summary+detail+(result.warnings?.length?`<div class="error-banner">${result.warnings.map(escapeHTML).join('<br>')}</div>`:'')+`<div class="workspace-grid"><div class="left-column">${viewerPanel}${renderSequence(job,result)}${renderStageProgress(job)}${renderResultArtifacts(job)}</div><aside class="right-column">${renderConfidence(result)}${renderChainConfidence(job,result)}${renderPAE(result)}<div class="provenance-card"><div class="provenance-label">${icon('shield')}来源与可复现性</div><p>${escapeHTML(source.mode==='reference'?'已发布的公开结构及分析数据，用于体验工作流。提交新的真实预测需配置对应引擎。':source.mode==='import'?'已导入的结构文件。结构本身不会自动产生模型置信度。':'输入序列、参数、执行日志与结果随任务保存在本地。')}<br>${escapeHTML(source.sourceLabel||source.model||engineNames[job.engine]||'')}</p></div></aside></div><div class="lower-grid">${renderJobsTable(state.jobs.slice(0,4),'最近任务',true)}</div>`;
}

function safeSourceLink(provenance) {const url=provenance.sourceUrl;return typeof url==='string'&&/^https?:\/\//.test(url)?`<a href="${escapeHTML(url)}" target="_blank" rel="noreferrer">${escapeHTML(provenance.sourceLabel||'原始来源')} ↗</a>`:escapeHTML(provenance.sourceLabel||'本地结果');}
function renderChainConfidence(job,result) {
  // AF3 Server convention (EBI interpreting-results guide / AF3 output.md):
  // multichain predictions carry per-chain pTM and per-chain average interface
  // ipTM (chain_iptm). Averages can mask asymmetric interfaces, so the note
  // points at the PAE view instead of over-trusting the mean. Interface chips
  // add ⟨PAE⟩_int per chain pair (Zhu et al. 2023): the mean over inter-chain
  // residue pairs with both directed PAE blocks averaged.
  const chains=job?.chains||[], ptm=result?.chainPtm, iptm=result?.chainIptm;
  const iface=Array.isArray(result?.interfacePae)?result.interfacePae.filter(p=>p&&typeof p.a==='string'&&typeof p.b==='string'&&Number.isFinite(p.mean)):[];
  const hasTable=chains.length>=2&&(Array.isArray(ptm)||Array.isArray(iptm));
  if(!hasTable&&!iface.length)return '';
  const rows=hasTable?chains.map((c,i)=>{const p=ptm?.[i],t=iptm?.[i],band=typeof campaignScoreBand==='function'?campaignScoreBand(t):null;
    return `<tr><td class="mono">${escapeHTML(c.id||String.fromCharCode(65+i))}</td><td>${Number.isFinite(p)?p.toFixed(2):'—'}</td><td>${band?`<span class="campaign-score-band ${band}">ipTM ${t.toFixed(2)}</span>`:Number.isFinite(t)?t.toFixed(2):'—'}</td></tr>`;}).join(''):'';
  const table=hasTable?`<div class="table-scroll"><table><thead><tr><th>链</th><th>pTM</th><th>平均界面 ipTM</th></tr></thead><tbody>${rows}</tbody></table></div>`:'';
  const chips=iface.length?`<div class="pae-interface-chips">${iface.map(pair=>`<span class="pae-interface-chip" title="⟨PAE⟩_int：链对间残基对的 PAE 双向块均值（Zhu et al. 2023 口径）；PAE 非对称，均值已抹平方向差异，具体方向以 PAE 矩阵为准">${escapeHTML(pair.a)}↔${escapeHTML(pair.b)} 界面 PAE ${pair.mean.toFixed(1)} Å</span>`).join('')}</div>`:'';
  return `<section class="panel analysis-panel chain-conf-panel"><div class="panel-heading"><h3>${icon('shield')}链间界面置信度</h3><span class="subtle">CHAIN CONFIDENCE</span></div>${table}${chips}<div class="analysis-title-note">逐链 pTM 与平均界面 ipTM · 均值为 AF3 chain_iptm 口径，非对称界面建议结合 PAE 判断</div></section>`;
}
function renderConfidence(result) {
  const sampleCurve=viewerModelInfo().index>0?state.modelCurve:null;
  const values=sampleCurve||getPlddt(result), avg=sampleCurve?mean(values):result.meanPlddt??mean(values);
  let body;
  if(!values.length) body=`<div class="empty-analysis">${icon('chart')}<strong>暂无模型置信度</strong><p>预测输出包含 pLDDT 后显示。<br>实验结构的 B 因子不作为 pLDDT。</p></div>`;
  else {
    const w=250,h=74,pad=4,points=values.map((v,i)=>[pad+i/Math.max(values.length-1,1)*(w-pad*2),h-(Math.max(0,Math.min(100,v??0))/100)*(h-8)]);
    const line=points.map((p,i)=>(i?'L':'M')+p.map(v=>v.toFixed(1)).join(',')).join(' '),area=`${line} L${w-pad},${h} L${pad},${h} Z`,high=values.filter(v=>v>=90).length/values.length*100;
    body=`<div class="confidence-value"><strong>${avg!=null?Number(avg).toFixed(1):'—'}</strong><span class="quality-pill">${avg>=90?'非常高':avg>=70?'较高':avg>=50?'需检查':'较低'}</span></div><div class="confidence-chart"><svg viewBox="0 0 ${w} ${h+5}" preserveAspectRatio="none" role="img" aria-label="逐残基pLDDT折线图"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#9abf75" stop-opacity=".35"/><stop offset="100%" stop-color="#9abf75" stop-opacity=".02"/></linearGradient></defs><path d="M4 15H246M4 39H246M4 64H246" stroke="#e9efe2" stroke-width=".7" stroke-dasharray="2 3"/><path d="${area}" fill="url(#chart-fill)"/><path d="${line}" fill="none" stroke="#7fa66d" stroke-width="1.35"/></svg><div class="chart-labels"><span>1</span><span>Residue index</span><span>${values.length}</span></div></div><div class="confidence-bands"><div class="bandbar"><span style="width:${high}%"></span></div><small>${high.toFixed(0)}% ≥ 90</small></div>`;
  }
  return `<section class="panel analysis-panel"><div class="panel-heading"><h3>${icon('chart')}置信度分析</h3><span class="subtle">pLDDT</span></div><div class="analysis-title-note">${sampleCurve?'当前查看采样 · 曲线由结构文件 B 因子（pLDDT 惯例）导出':'局部结构的预测可信程度'}</div>${body}</section>`;
}
function renderPAE(result) {return window.renderPAEPanel(result);}
function drawPAE() {window.mountPAEPanel(state.job?.result);}
function renderSequence(job,result) {const residues=getResidues(result);let list=residues;if(!list.length)list=(job.chains||[]).flatMap(c=>Array.from(c.sequence||'').map((aa,i)=>({aa,index:i+1,chain:c.id||'A'})));const chainIds=[...new Set(list.map(r=>r.chain||'A'))];return `<section class="panel sequence-panel"><div class="panel-heading"><h3>${icon('chain')}残基序列</h3><span class="chain-label"><i class="chain-dot"></i>Chain ${chainIds.map(escapeHTML).join(' / ')}</span></div><div class="sequence-container"><div class="sequence-index">N-TERMINUS <span style="float:right">${list.length} RESIDUES · C-TERMINUS</span></div><div class="sequence-row">${list.map((r,i)=>`<span class="residue" data-residue="${i}" title="${escapeHTML(r.chain||'A')}:${r.residueNumber??r.index??i+1} ${escapeHTML(r.aa||r.resName||'X')}${Number.isFinite(r.plddt)?' · pLDDT '+r.plddt.toFixed(1):''}" ${Number.isFinite(r.plddt)?`style="color:${confidenceColor(r.plddt)}"`:''}>${escapeHTML(r.aa||'X')}</span>`).join('')}</div></div><div class="sequence-selection" id="sequence-selection">点击残基，在三维结构中定位</div></section>`;}

function renderStageProgress(job) {const stages=[['validation','输入校验','sequence','file'],['preparation','数据准备','MSA / reference','search'],['features','特征处理','featurisation','layers'],['inference','结构计算','model / replay','molecule'],['analysis','质量解析','confidence','chart'],['completed','结果交付','artifacts','check']];const stage=String(job.stage||'validation').toLowerCase();let index=stages.findIndex(s=>s[0]===stage);if(index<0)index=Math.max(0,Math.min(5,Math.floor(Number(job.progress||0)/20)));const finished=job.status==='completed';return `<section class="panel pipeline-panel"><div class="panel-heading"><h3>${icon('workflow')}任务执行流程</h3><span class="subtle">${finished?'已完成':escapeHTML(statusLabels[job.status]||'')} · ${Math.round(job.progress||0)}%</span></div><div class="pipeline-steps">${stages.map((s,i)=>`<div class="pipeline-step ${finished||i<index?'done':i===index?'current':''}"><div class="step-icon">${icon(finished||i<index?'check':s[3])}</div><div class="step-name">${s[1]}</div><div class="step-meta">${job.engine==='reference'&&i===3?'参考结构读取':s[2]}</div></div>`).join('')}</div><div class="panel-footer"><span>${job.engine==='reference'?'REFERENCE MODE · 读取已有公开结果':`任务 ${escapeHTML(job.id?.slice(0,10))}`}</span><button class="text-button" id="toggle-logs">查看执行日志 ↗</button></div><div id="inline-logs" class="hidden" style="padding:0 18px 18px">${renderLogs(job)}</div></section>`;}
function renderLogs(job) {return `<div class="event-log">${(job.events||[]).map(event=>`<div><span class="log-time">${escapeHTML(timeLabel(event.time||event.timestamp))}</span><span class="log-stage">${escapeHTML(event.stage||'event')}</span>${escapeHTML(event.message||'')}</div>`).join('')||'等待任务日志…'}</div>`;}

function rankedScoreBars(models, viewIndex) {
  // ColabFold ranking-plot / AF3 Server convention: compare every sample's score
  // in one view — a clear drop behind rank 1 supports trusting the top sample,
  // crowded scores warrant caution. Bars share one hue (magnitude), the sample
  // currently in the viewer is emphasized by state, never by rank color.
  if(models.length<2) return '';
  const metric=models.filter(m=>Number.isFinite(m.rankingScore)).length>=2?'rankingScore':'meanPlddt';
  const scored=models.map((m,i)=>({m,i,v:m[metric]})).filter(x=>Number.isFinite(x.v)).sort((a,b)=>(a.m.rank??a.i)-(b.m.rank??b.i));
  if(scored.length<2) return '';
  const digits=metric==='rankingScore'?3:1, max=Math.max(...scored.map(x=>x.v));
  const gap=scored[0].v-Math.max(...scored.slice(1).map(x=>x.v));
  const label=metric==='rankingScore'?'排名分':'平均 pLDDT';
  const rows=scored.map(x=>`<div class="score-bar-row${x.i===viewIndex?' current':''}" title="排名 ${x.m.rank??x.i+1} · ${escapeHTML(x.m.name||'sample-'+x.i)} · ${label} ${x.v.toFixed(digits)}"><span class="score-bar-label">排名 ${x.m.rank??x.i+1}</span><span class="score-bar-track"><span class="score-bar" style="width:${Math.max(2,x.v/max*100).toFixed(1)}%"></span></span><span class="score-bar-value">${x.v.toFixed(digits)}</span></div>`).join('');
  return `<div class="score-compare" aria-label="各采样${label}对比"><div class="score-compare-title">${label}对比<span>首名领先 ${gap.toFixed(digits)} · 分差越大择优越可靠，接近则需谨慎对比</span></div>${rows}</div>`;
}
function renderResultArtifacts(job) {
  const result=job.result||{},models=result.rankedModels||[],artifacts=result.artifacts||job.artifacts||[];
  if(!artifacts.length&&!models.length)return '';
  const score=v=>Number.isFinite(v)?Number(v).toFixed(3):'—';
  const viewInfo=viewerModelInfo(job);
  const modelRows=models.map((model,i)=>{const current=models.length>=2&&viewInfo.index===i;const view=models.length>=2&&model.structureUrl?`<button class="text-button" data-view-model="${i}" ${current?'disabled title="已在三维视图中"':''}>${current?'当前视图':'三维查看'}</button>`:'';return `<tr${current?' class="current-model-row"':''}><td class="mono">${escapeHTML(model.rank||'—')}</td><td>${escapeHTML(model.name||'结构模型')}</td><td>${Number.isFinite(model.meanPlddt)?model.meanPlddt.toFixed(1):'—'}</td><td>${score(model.ptm)}</td><td>${score(model.iptm)}</td><td>${score(model.rankingScore)}</td><td class="model-actions">${view}${model.structureUrl?`<a class="text-button" href="${escapeHTML(model.structureUrl)}" download>下载结构 ↗</a>`:'主结果见三维视图'}</td></tr>`;}).join('');
  return `<section class="panel" style="margin-top:20px"><div class="panel-heading"><h3>${icon('layers')}模型与结果制品</h3><span class="subtle">${models.length} MODEL${models.length===1?'':'S'} · ${artifacts.length} FILES</span>${artifacts.length&&job.status==='completed'?`<a class="text-button" href="${escapeHTML(appUrl(`/api/jobs/${encodeURIComponent(job.id)}/archive`))}" download title="打包下载该任务全部结果制品">${icon('download')} 下载全部 zip ↗</a>`:''}</div>${rankedScoreBars(models,viewInfo.index)}${models.length?`<div class="table-scroll"><table><thead><tr><th>排名</th><th>模型 / 样本</th><th>pLDDT</th><th>pTM</th><th>ipTM</th><th>排名分</th><th>结构文件</th></tr></thead><tbody>${modelRows}</tbody></table></div>`:''}<div class="artifact-list">${artifacts.map(file=>`<a class="artifact-link" href="${escapeHTML(file.url||'#')}" download>${icon('file')}<span>${escapeHTML(file.name||'artifact')}</span><small>${file.size!=null?Math.max(.1,file.size/1024).toFixed(1)+' KB':''}</small>${icon('download')}</a>`).join('')}</div><details class="execution-details"><summary>参数、执行环境与来源</summary><dl><dt>模型 / 来源版本</dt><dd>${escapeHTML(result.provenance?.engineVersion|| (result.provenance?.databaseVersion?'AlphaFold DB v'+result.provenance.databaseVersion:'未提供'))}</dd>${result.provenance?.workerId?`<dt>执行节点 / 位置</dt><dd>${escapeHTML(result.provenance.workerId)} / ${escapeHTML(result.provenance.location||result.provenance.workerLocation||'—')}</dd><dt>节点批次 / 尝试</dt><dd>${escapeHTML(result.provenance.batchId||'—')} / ${result.provenance.attempt??'—'}</dd><dt>常驻进程 / 模型加载</dt><dd>PID ${escapeHTML(result.provenance.residentPid??'—')} · ${result.provenance.modelLoads??'—'} 次</dd>`:''}<dt>随机种子</dt><dd>${escapeHTML((job.seeds||[]).join(', ')||'—')}</dd><dt>请求采样参数 / Recycles</dt><dd>${job.engine==='reference'?'已有结果回放':escapeHTML(`${job.numSamples??'—'} / ${job.numRecycles??'—'}`)}</dd><dt>实际返回结构数</dt><dd>${result.returnedModelCount??models.length}</dd><dt>分配 GPU 组</dt><dd>${escapeHTML(result.provenance?.assignedGpuGroup?.join(', ')|| (job.engine==='reference'?'无需 GPU':'由运行器分配'))}</dd><dt>平台端到端耗时</dt><dd>${result.timings?.totalSeconds!=null?result.timings.totalSeconds.toFixed(3)+' 秒（包含平台阶段）':'—'}</dd></dl></details></section>`;
}

function jobConfidenceCell(job) {
  const result=job.result;
  if(job.status!=='completed'||!result)return '<span class="muted">—</span>';
  const parts=[];
  if(Number.isFinite(result.meanPlddt)){
    const band=result.meanPlddt>=90?['high','≥90 很高']:result.meanPlddt>=70?['gray','70–90 置信']:result.meanPlddt>=50?['low','50–70 低']:['low','<50 很低'];
    parts.push(`<span title="平均 pLDDT，AF DB 四带 ≥90/70–90/50–70/<50；多采样任务为排名 1 主结果">${result.meanPlddt.toFixed(1)} <span class="campaign-score-band ${band[0]}">${band[1]}</span></span>`);
  }
  if(Number.isFinite(result.iptm)){
    const band=campaignScoreBand(result.iptm);
    const label=band==='high'?'≥0.8 高':band==='gray'?'0.6–0.8 灰区':'<0.6 低';
    parts.push(`<span title="ipTM，EBI 界面置信阈值 ≥0.8 高置信、0.6–0.8 灰区">ipTM ${result.iptm.toFixed(2)} <span class="campaign-score-band ${band}">${label}</span></span>`);
  }
  return parts.length?parts.join(' '):'<span class="muted">—</span>';
}
function sortedJobsForTable(jobs) {
  // BLAST-style score-column sort: completed jobs with a mean pLDDT first
  // (descending), then unscored completions, then the rest. Never mutates the
  // source array; ties keep the list's recency order (stable sort).
  if(state.jobsSort!=='confidence')return jobs;
  const tier=job=>job.status==='completed'&&Number.isFinite(job.result?.meanPlddt)?0:job.status==='completed'?1:2;
  return [...jobs].sort((a,b)=>tier(a)-tier(b)||(Number(b.result?.meanPlddt||-1)-Number(a.result?.meanPlddt||-1)));
}
function renderJobsTable(jobs,title,compact=false) {return `<section class="panel"><div class="panel-heading"><h3 class="jobs-heading">${icon('layers')}${title}<span class="count-pill">${jobs.length}</span></h3>${compact?'<button class="text-button" data-goto="jobs">全部任务 ↗</button>':'<button class="text-button" id="export-jobs">导出当前筛选 ↗</button>'}</div><div class="table-scroll"><table><thead><tr><th>任务名称</th><th>模型 / 来源</th><th>状态</th>${compact?'<th>置信度</th>':`<th aria-sort="${state.jobsSort==='confidence'?'descending':'none'}"><button class="text-button" data-jobs-sort="confidence" id="jobs-sort-confidence" title="按平均 pLDDT 降序排列（BLAST 结果表分值列排序惯例）">置信度 ${state.jobsSort==='confidence'?'↓':'⇅'}</button></th>`}<th>序列长度</th><th>创建时间</th><th></th></tr></thead><tbody>${jobs.map(job=>`<tr data-job-id="${escapeHTML(job.id)}"><td><div class="job-name">${icon('file')}${escapeHTML(job.name)}</div><div class="job-subline mono">${escapeHTML(job.id.slice(0,12))}</div></td><td>${escapeHTML(engineNames[job.engine]||job.engine)}</td><td>${statusPill(job.status)}</td><td class="mono">${jobConfidenceCell(job)}</td><td class="mono">${job.result?.sequenceLength||job.chains?.reduce((n,c)=>n+(c.sequence?.length||0),0)||'—'} <span class="muted">aa</span></td><td class="mono">${escapeHTML(timeLabel(job.createdAt))}</td><td><button class="row-action" title="查看任务" aria-label="查看${escapeHTML(job.name)}">↗</button></td></tr>`).join('')||'<tr><td colspan="7" style="text-align:center;padding:40px">暂无匹配任务</td></tr>'}</tbody></table></div></section>`;}
function filteredJobs() {return state.jobs.filter(j=>(state.filter==='all'||j.status===state.filter)&&`${j.name} ${j.id} ${j.engine} ${engineNames[j.engine]||''}`.toLowerCase().includes(state.search.toLowerCase()));}
function refreshJobsTable() {
  const root=$('#jobs-table-container');if(!root)return;
  const active=document.activeElement,focusedId=active?.closest('[data-job-id]')?.dataset.jobId;
  const scroll=root.querySelector('.table-scroll')?.scrollLeft||0;
  root.innerHTML=renderJobsTable(sortedJobsForTable(filteredJobs()),'全部实验');hydrateIcons(root);
  root.querySelectorAll('[data-job-id]').forEach(el=>el.addEventListener('click',()=>selectJob(el.dataset.jobId)));
  $('#export-jobs')?.addEventListener('click',()=>downloadJSON(filteredJobs(),'foldspace-filtered-jobs.json'));
  const table=root.querySelector('.table-scroll');if(table)table.scrollLeft=scroll;
  if(focusedId)[...root.querySelectorAll('[data-job-id]')].find(el=>el.dataset.jobId===focusedId)?.querySelector('button')?.focus({preventScroll:true});
}
function renderJobs() {const jobs=sortedJobsForTable(filteredJobs());return `<div class="toolbar"><div class="search-input">${icon('search')}<input id="job-search" placeholder="搜索任务名称、ID 或引擎" value="${escapeHTML(state.search)}" aria-label="搜索任务"></div><select id="job-filter" aria-label="筛选任务状态">${[['all','全部状态'],['running','运行中'],['queued','排队中'],['completed','已完成'],['failed','失败'],['cancelled','已取消']].map(([k,v])=>`<option value="${k}" ${state.filter===k?'selected':''}>${v}</option>`).join('')}</select></div><div id="jobs-table-container">${renderJobsTable(jobs,'全部实验')}</div><div class="section-note" style="margin-top:16px">点击任务可查看结构、参数和日志。失败或取消的任务可以重新提交，已完成的结果可独立下载。</div>`;}
function renderChainReuseArchitecture(){
  const blocks=[['32','48','168','68','固定靶标 T','精确链缓存 · 搜索一次','shared'],['32','150','168','68','候选 B₁ … Bₙ','缺失的链分别准备',''],['294','30','234','48','T + B₁','独立完整输入',''],['294','106','234','48','T + B₂','独立完整输入',''],['294','182','234','48','T + Bₙ','独立完整输入',''],['646','76','246','108','常驻 GPU 执行池','每个组合独立特征化与推理','execution']];
  return `<section class="panel chain-architecture"><div class="panel-heading"><h3>${icon('layers')}固定靶标的输入复用</h3><span class="subtle">AF3 自动准备 · 机制示意</span></div><div class="chain-architecture-canvas"><svg viewBox="0 0 930 264" role="img" aria-label="固定靶标链搜索一次，与不同候选组装完整输入后分别提交常驻GPU执行池。"><defs><marker id="chain-flow-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" fill="currentColor"/></marker></defs><g class="chain-diagram-path"><path d="M200 82H242V54H294M242 82V130H294M242 130V206H294"/><path d="M200 184H265V64H294M265 140H294M265 184V216H294"/><path d="M528 54H584V105H646M528 130H646M528 206H584V155H646"/></g>${blocks.map(([x,y,w,h,title,note,kind])=>`<g class="chain-diagram-block ${kind}" transform="translate(${x} ${y})"><rect width="${w}" height="${h}" rx="12"/><text x="16" y="${kind==='execution'?38:25}" class="chain-diagram-title">${title}</text><text x="16" y="${kind==='execution'?64:43}" class="chain-diagram-note">${note}</text>${kind==='execution'?'<text x="16" y="85" class="chain-diagram-note">本地 / 阿里云兼容节点领取批次</text>':''}</g>`).join('')}</svg></div><div class="chain-architecture-foot"><p><strong>N+1 个唯一链搜索身份 → N 个完整组合预测。</strong> 原始 MSA 与模板按版本复用，完整复合物重新完成配对。共享链等待时释放准备槽位，独立候选继续流动；批次详情展示 MSA 深度、覆盖率和首次准备程序耗时。</p><a href="https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md" target="_blank" rel="noreferrer">AF3 官方依据 ↗</a></div></section>`;
}
function renderMSASearchArchitecture(){
  const steps=[
    ['01 · SEARCH','MMseqs2 搜索','本地 ColabFold 搜索 UniRef30 与环境库，生成单链未配对 A3M。GPU 模式仅在此段持有搜索设备。','CPU / GPU 可选'],
    ['02 · CPU FINALIZE','补齐配对与模板','按所选策略搜索原生 UniProt 配对候选，并用未配对 MSA 搜索 AF3 模板。此段释放搜索 GPU。','配对与模板可独立配置'],
    ['03 · AF3 INPUT','校验并组装完整输入','核对 query、对齐宽度和链身份，分别保存 unpairedMsa、pairedMsa 与 templates，再组合复合物。','链缓存 · MSA QC'],
    ['04 · PREDICTION','进入常驻预测执行池','已准备制品分发给本地或阿里云 GPU worker，完整目标特征化与推理沿用既有流程。','就绪队列 · 逐项恢复']
  ];
  return `<section class="panel msa-search-architecture"><div class="panel-heading"><h3>${icon('search')}MSA 搜索加速路径</h3><span class="subtle">两段执行 · 策略示意</span></div><p class="msa-search-intro">搜索后端由数据库配置绑定。未配对搜索、配对候选与模板分别记录，策略随批次保存。</p><ol class="msa-search-flow">${steps.map(([id,title,note,tag])=>`<li><span class="msa-search-stage">${id}</span><h4>${title}</h4><p>${note}</p><span class="msa-search-device">${tag}</span></li>`).join('')}</ol><div class="msa-search-foot"><p>GPU 搜索使用独立设备组，并与同节点推理进程共享设备锁。共享链或设备繁忙时暂缓目标，其他任务继续流动。</p><button class="button button-secondary" data-goto="databases">查看数据库策略 ${icon('arrow')}</button></div></section>`;
}
function renderPipeline() {
  const resident=state.system.pipeline?.predictionMode==='workers';
  const nodes=[
    ['01','流式录入与持久任务账本','FASTA / JSONL 每 100 项写入一次，targetId 保证重传幂等。批次封存与逐项状态独立记录。','campaigns.sqlite3 · streaming · idempotency','file'],
    ['02','数据库预检与两层准备缓存','AF3 先查整目标，再复用相同链的 MSA / 模板。共享链繁忙时先搜索其他链，依赖暂缓时释放准备槽位，再组装完整输入。','whole target → chain cache → assembly','search'],
    ['03',resident?'可校验传输与常驻批次':'有界就绪缓冲与 GPU 执行',resident?'本地 / 阿里云节点领取兼容参数和长度桶的多目标批次，校验下载准备包，同一模型进程连续执行。':'准备中与就绪项达到水位即停止继续取准备任务。GPU 执行池消费就绪数据，CPU 搜索不占用 GPU 租约。',resident?'SHA-256 · resident model · batch pull':'backpressure · GPU groups · prediction','cpu'],
    ['04','逐项验证与交付','预测适配器读取结构与置信度，独立验证池核对输出序列和结果路径。结果文件留在独立目录，账本只保存元数据和已发布指针。','mmCIF / PDB · pLDDT · PAE','chart'],
    ['05','恢复与取消',resident?'每个目标独立回传并校验发布。节点失联后回收尚未提交的租约，已交付目标保留；新会话撤销旧进程发布权。':'每次执行有租期和令牌。过期或重启后从可恢复阶段继续；旧进程无法覆盖新的正式结果。','lease · attempt fencing · durable state','shield'],
    ['06','分页探索整个批次','查看各阶段积压、筛选失败目标，分页读取列表。完成的任意目标可进入三维结构工作台。','stage counts · pagination · 3Dmol.js','molecule']
  ];
  return `<div class="architecture-flow">${nodes.map((n,i)=>`<section class="architecture-node ${i===2?'dark':''}"><div class="number">${n[0]} / ${icon(n[4])}</div><h3>${n[1]}</h3><p>${n[2]}</p><div class="tech">${n[3]}</div></section>`).join('')}</div><section class="panel pipeline-detail"><h3 style="font-size:15px;font-weight:500">CPU 与 GPU 各自消费任务，多个目标同时流动</h3><p class="section-note">批次调度器在一个控制节点运行。AF3 使用原生 data pipeline / inference 拆分，ColabFold 使用本地搜索 → A3M → 预测；AF2 当前通过原有完整任务运行器执行。AF3 的 GPU 阶段仍包含原生 featurisation。预测执行槽可绑定显式 GPU 组，实际多卡计算能力取决于模型运行器。</p><p class="section-note">${resident?'当前使用常驻节点模式：AF3 原生 ModelRunner 与权重跨目标、跨批次复用。准备包下发和结果包回收通过认证 HTTP(S) 传输，逐项校验 SHA-256 与租约后进入验证队列。节点运行时、PID、初始化次数、传输与恢复记录可在混合云页面查看。':'当前使用 legacy 模式，每个目标启动独立预测进程。启用 workers 模式后可接入常驻 AF3 节点；ColabFold 保留原有 staged 运行器。'}</p><div class="task-actions"><button class="button button-soft" data-goto="campaigns">打开批次管线 ${icon('arrow')}</button><button class="button button-secondary" data-goto="databases">检查数据库 ${icon('search')}</button><button class="button button-secondary" data-goto="cluster">混合云执行 ${icon('server')}</button><button class="button button-secondary" data-goto="storage">存储与缓存 ${icon('layers')}</button><button class="button button-secondary" data-goto="sources">查看实现依据 ${icon('book')}</button></div></section>${renderMSASearchArchitecture()}${renderChainReuseArchitecture()}`;
}

function renderSources() {const items=state.sources.length?state.sources:[{title:'AlphaFold 2',url:'https://github.com/google-deepmind/alphafold',description:'官方推理流程、FASTA 输入与模型输出。',category:'OFFICIAL IMPLEMENTATION'},{title:'AlphaFold 3',url:'https://github.com/google-deepmind/alphafold3',description:'多组分输入、分阶段执行及结构与置信度输出。',category:'OFFICIAL IMPLEMENTATION'},{title:'ColabFold',url:'https://github.com/sokrypton/ColabFold',description:'基于 MMseqs2 的搜索与批量蛋白结构预测实现。',category:'BATCH PREDICTION'},{title:'nf-core / proteinfold',url:'https://nf-co.re/proteinfold',description:'可复现的工作流与集群批处理编排参考。',category:'WORKFLOW'},{title:'3Dmol.js',url:'https://3dmol.org/',description:'本项目实际使用的 WebGL 三维分子可视化库。',category:'VISUALIZATION'},{title:'AlphaFold Protein Structure Database',url:'https://alphafold.ebi.ac.uk/entry/P0CG47',description:'默认参考示例的真实结构、pLDDT 与 PAE 来源。',category:'REFERENCE DATA'}];return `<div class="source-grid">${items.map((item,i)=>`<article class="panel source-card"><div class="source-top"><span>${escapeHTML(item.category||item.type||'PRIMARY SOURCE')}</span><span>${String(i+1).padStart(2,'0')}</span></div><h3>${escapeHTML(item.title||item.name||'研究来源')}</h3><p>${escapeHTML(item.description||item.supports||item.summary||'官方实现与技术文档，具体采用方式见 docs/research.md。')}</p><a class="source-link" href="${escapeHTML(/^https?:\/\//.test(item.url||'')?item.url:'#')}" target="_blank" rel="noreferrer">查看原始来源 ↗</a></article>`).join('')}</div><p class="section-note" style="margin-top:20px">资料核查日期：2026-10-23。项目中的研究说明记录了方案取舍、版本与代码 / 权重许可。原始预测的模型版本由结果来源或任务清单保留。</p>`;}
function renderEnvironment() {const workerMode=state.system.pipeline?.predictionMode==='workers';if(workerMode){const capabilities=state.system.pipeline?.capabilities||[];return `<section class="panel config-note" style="margin-top:0;margin-bottom:20px"><strong>常驻节点模式 · 控制面负责搜索与验证，GPU 节点负责模型执行</strong><p>在批次管线提交目标，选择本地、阿里云或共同执行。真实节点及其模型版本、GPU 设备、输入长度上限、PID 与模型初始化次数在混合云页面查看。</p><button class="button button-soft" data-goto="cluster">查看常驻节点 ${icon('server')}</button></section><div class="environment-grid">${capabilities.map(engine=>`<section class="panel environment-card"><div class="environment-top"><h3>${escapeHTML(engineNames[engine.engine]||engine.engine)}</h3><span class="status-pill ${engine.available?'completed':'queued'}"><i></i>${engine.available?'准备能力可用':'此模式未就绪'}</span></div><p>${escapeHTML(engine.engine==='reference'?'公开结果回放，验证实际网络传输与常驻进程，不执行新结构预测。':engine.engine==='alphafold3'?'控制节点运行 CPU 准备；已准备输入交给支持同一模型配置的常驻 GPU 节点。':engine.diagnostics?.join(' ')||'保留原有 legacy 运行器。')}</p><code>${escapeHTML(engine.executionMode||'—')}</code></section>`).join('')}</div>`;}const engines=state.engines.length?state.engines.map(engine=>workerMode&&engine.id!=='reference'?{...engine,available:false,description:'常驻节点模式通过批次管线提交真实模型任务。'}:engine):[{id:'reference',name:'公开结构回放',available:true,description:'读取随项目提供的公开结构与真实分析数据。'}];return `<div class="environment-grid">${engines.map(engine=>`<section class="panel environment-card"><div class="environment-top"><h3>${escapeHTML(engine.name||engineNames[engine.id]||engine.id)}</h3><span class="status-pill ${engine.available?'completed':'queued'}"><i></i>${engine.available?'可用':'待配置'}</span></div><p>${escapeHTML(engine.description||engine.reason||'通过配置外部运行器连接该预测环境。')}</p><code>${escapeHTML(engine.environmentVariable||engine.envVar||({alphafold2:'PROTEIN_STUDIO_ALPHAFOLD2_COMMAND',alphafold3:'PROTEIN_STUDIO_ALPHAFOLD3_COMMAND',colabfold:'PROTEIN_STUDIO_COLABFOLD_COMMAND',reference:'随项目提供 · 无需 GPU'}[engine.id]||engine.id))}</code></section>`).join('')}</div><section class="panel config-note"><strong>接入真实 GPU 推理</strong><p>在启动服务前配置对应引擎的命令 JSON 数组，连接已经安装好权重、数据库及依赖的模型环境。示例位于 <code>config/engines.example.json</code>，部署方法见 <code>docs/deployment.md</code>。</p><p>本机状态：${escapeHTML(state.system.gpu_available?'已检测到 '+(state.system.gpus?.length||0)+' 张 NVIDIA GPU':'本机未检测到 NVIDIA GPU，真实推理可连接外部运行环境')}。已配置的命令是否能完成预测，将由任务执行和退出状态验证。</p><div class="task-actions"><button class="button button-soft" id="refresh-environment">${icon('rotate')}重新检测</button><button class="button button-secondary" data-goto="pipeline">查看执行管线</button></div></section>`;}

async function loadStructure() {
  const id=state.job?.id;if(!id||!state.job.result)return;const container=$('#molecule-viewer');if(!container)return;
  try {
    if(!window.$3Dmol)throw new Error('三维渲染库未能载入，请检查 web/vendor/3Dmol-min.js');
    const info=viewerModelInfo();
    const response=await fetch(appUrl(info.url));if(!response.ok)throw new Error('结构文件读取失败');const text=await response.text();
    if(state.job?.id!==id||state.view!=='studio'||!container.isConnected||viewerModelInfo().url!==info.url)return;
    const format=info.format;
    const viewer=window.$3Dmol.createViewer(container,{backgroundColor:state.light?'#edf1e9':'#102a34',antialias:true,cartoonQuality:8});state.viewer=viewer;
    viewer.addModel(text,format==='mmcif'?'cif':format);applyViewerStyle();viewer.zoomTo();viewer.zoom(1.07);viewer.rotate(15,'y');viewer.rotate(-12,'x');viewer.render();if(state.spinning)viewer.spin('y',.3);
    viewer.setClickable({},true,atom=>selectAtom(atom));viewer.render();$('#viewer-loading')?.classList.add('hidden');
    state.modelCurve=info.index>0?bFactorResidueCurve():null;
    if(info.index>0){const panel=document.querySelector('.analysis-panel:not(.pae-panel)');if(panel)panel.outerHTML=renderConfidence(state.job.result);}
    const observer=new ResizeObserver(()=>{if(container.isConnected&&state.viewer===viewer){viewer.resize();viewer.render();}else observer.disconnect();});observer.observe(container);
  } catch(error) {if($('#viewer-loading'))$('#viewer-loading').innerHTML=`<span>${escapeHTML(error.message)}</span>`;}
}
function residueConfidenceColorer(residues) {
  const missingColor='#8e9ca6', values=new Map(), insertions=new Map();
  const part=value=>{const text=String(value??'').trim();return text==='.'||text==='?'?'':text;};
  const identity=(chain,number,insertion)=>{
    if(number===null||number===undefined||number==='')return null;
    const position=Number(number);if(!Number.isInteger(position))return null;
    return [part(chain)||'A',position,part(insertion)];
  };
  for(const residue of residues){
    const id=identity(residue.chain,residue.residueNumber,residue.insertionCode);if(!id)continue;
    const key=JSON.stringify(id),base=JSON.stringify(id.slice(0,2));
    if(!insertions.has(base))insertions.set(base,new Set());insertions.get(base).add(id[2]);
    const value=Number.isFinite(residue.plddt)&&residue.plddt>=0&&residue.plddt<=100?residue.plddt:null;
    // Conflicting residue identities are unknown, never borrowed from another chain.
    values.set(key,values.has(key)&&values.get(key)!==value?null:value);
  }
  return atom=>{
    const id=identity(atom.chain,atom.resi,atom.icode);if(!id)return missingColor;
    // The bundled text-mmCIF parser can omit insertion codes. Do not color an
    // ambiguous insertion group using the confidence of its uninserted residue.
    if(atom.icode==null&&(insertions.get(JSON.stringify(id.slice(0,2)))?.size||0)>1)return missingColor;
    const value=values.get(JSON.stringify(id));
    return Number.isFinite(value)?confidenceColor(value):missingColor;
  };
}
function bFactorConfidenceColorer() {
  // Non-primary ranked models: AF2/ColabFold/AF3 write per-residue pLDDT into the
  // B-factor column of ranked structure files. A file without usable factors
  // (all-zero or absent B values) returns null and falls back to chain coloring.
  const atoms=state.viewer?.getModel()?.selectedAtoms?.({})||[];
  const factors=atoms.map(atom=>Number(atom.b)).filter(Number.isFinite);
  if(!factors.length||!factors.some(value=>value>0&&value<=100))return null;
  return atom=>{const value=Number(atom.b);return Number.isFinite(value)&&value>0&&value<=100?confidenceColor(value):'#8e9ca6';};
}
function lowConfidenceSelection(threshold) {
  // Selection groups for low-confidence display modes (AF DB FAQ: pLDDT < 50
  // regions are often unstructured and must not be interpreted; PDBe-KB dims
  // < 70; PyMOL users hide b < 50). Ranked samples read the B-factor column,
  // the primary model maps residue pLDDT by the colorer's identity rules.
  // Returns null when no per-residue confidence maps to atoms, or when the
  // subset would cover every atom — a blanked view hides rather than informs.
  const groups=new Map();let scored=0;
  const add=(chain,resi)=>{const key=String(chain??'').trim()||'A';if(!groups.has(key))groups.set(key,new Set());groups.get(key).add(Number(resi));};
  if(viewerModelInfo().index>0){
    const atoms=state.viewer?.getModel()?.selectedAtoms?.({})||[];
    for(const atom of atoms){
      const value=Number(atom.b);
      if(!(value>0&&value<=100))continue;
      scored++;if(value<threshold)add(atom.chain,atom.resi);
    }
  } else {
    if(!getPlddt().length)return null;
    for(const residue of getResidues()){
      const value=residue.plddt;
      if(!(Number.isFinite(value)&&value>=0&&value<=100))continue;
      scored++;
      const number=Number(residue.residueNumber);
      if(Number.isInteger(number)&&value<threshold)add(residue.chain,number);
    }
  }
  const low=[...groups.values()].reduce((sum,set)=>sum+set.size,0);
  if(!groups.size||!scored||low>=scored)return null;
  return [...groups].map(([chain,set])=>({chain,resi:[...set]}));
}
function bFactorResidueCurve() {
  // Per-residue pLDDT for the selected non-primary sample, read from CA B factors
  // under the same AF2/ColabFold/AF3 convention as bFactorConfidenceColorer.
  // Returns null when the file carries no usable factors so nothing is fabricated.
  const atoms=state.viewer?.getModel()?.selectedAtoms?.({})||[];
  const seen=new Set(),values=[];
  for(const atom of atoms){
    if((atom.atom||atom.name)!=='CA')continue;
    const value=Number(atom.b);if(!Number.isFinite(value))continue;
    const key=(atom.chain||'A')+':'+(atom.resi??values.length);
    if(seen.has(key))continue;
    seen.add(key);values.push(value);
  }
  return values.length&&values.some(value=>value>0&&value<=100)?values:null;
}
function applyViewerStyle() {
  const v=state.viewer;if(!v)return;v.removeAllSurfaces();v.setStyle({},{});const hasConfidence=getPlddt().length>0;
  const chainPalette=['#9edac6','#b7cb85','#8cbbde','#e9bf95','#bdabdb'];const chainIds=[...new Set(getResidues().map(r=>r.chain||'A'))];
  const chainColoring={colorfunc:atom=>chainPalette[Math.max(0,chainIds.indexOf(atom.chain||'A'))%chainPalette.length]};
  const info=viewerModelInfo();let coloring;
  if(state.color==='spectrum')coloring={color:'spectrum'};
  else if(state.color==='confidence'&&info.index>0)coloring={colorfunc:bFactorConfidenceColorer()||chainColoring.colorfunc};
  else if(state.color==='confidence'&&hasConfidence)coloring={colorfunc:residueConfidenceColorer(getResidues())};
  else coloring=chainColoring;
  if(state.style==='cartoon')v.setStyle({},{cartoon:{...coloring,thickness:.5,style:'rectangle'}});
  if(state.style==='stick')v.setStyle({},{stick:{radius:.13,...coloring},sphere:{scale:.22,...coloring}});
  if(state.style==='surface'){v.setStyle({},{cartoon:{...coloring,opacity:.55}});v.addSurface(window.$3Dmol.SurfaceType.VDW,{opacity:.8,...coloring});}
  if(state.lowConf&&state.style!=='surface'){
    const threshold=state.lowConf===1?70:50, groups=lowConfidenceSelection(threshold);
    if(groups)for(const group of groups){
      const sel={chain:group.chain,resi:group.resi};
      if(state.lowConf===1){
        if(state.style==='cartoon')v.setStyle(sel,{cartoon:{...coloring,thickness:.5,style:'rectangle',opacity:.18}});
        if(state.style==='stick')v.setStyle(sel,{stick:{radius:.13,...coloring,opacity:.18},sphere:{scale:.22,...coloring,opacity:.18}});
      }else v.setStyle(sel,{});
    }
  }
  v.render();
}
function selectAtom(atom) {state.viewer?.removeAllLabels();state.viewer?.addLabel(`${atom.resn||''} ${atom.chain||''}:${atom.resi}`,{position:{x:atom.x,y:atom.y,z:atom.z},fontSize:11,backgroundColor:'#183c39',fontColor:'#e4f5d8',borderRadius:5,backgroundOpacity:.85});state.viewer?.render();const residues=getResidues();const i=residues.findIndex(r=>r.chain===(atom.chain||'A')&&(Number(r.residueNumber??r.index)===Number(atom.resi)||Number(r.resi)===Number(atom.resi)));if(i>=0)highlightSequence(i);else if($('#sequence-selection'))$('#sequence-selection').textContent=`${atom.resn||'Residue'} · ${atom.chain||'A'}:${atom.resi}`;}
function highlightSequence(index) {document.querySelectorAll('.residue.selected').forEach(el=>el.classList.remove('selected'));const el=document.querySelector(`[data-residue="${index}"]`);el?.classList.add('selected');const residue=getResidues()[index];if(!residue)return;$('#sequence-selection').textContent=`${residue.chain||'A'}:${residue.residueNumber??residue.index??index+1} · ${residue.aa||''}${Number.isFinite(residue.plddt)?` · pLDDT ${residue.plddt.toFixed(1)}`:''}`;}
function downloadJSON(value,name) {const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json'});downloadBlob(blob,name);}
function downloadBlob(blob,name) {const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}
function focusPageHeading() { window.scrollTo({top:0,behavior:'instant'});$('#page-title')?.focus({preventScroll:true}); }
function jobIdFromHash(hash=location.hash){const match=/^#job\/([^/]+)$/.exec(hash||'');return match?decodeURIComponent(match[1]):null;}
async function selectJob(id) {
  const request=++state.selectionRequest;
  try {
    const job=await api(`/api/jobs/${encodeURIComponent(id)}`);
    if(request!==state.selectionRequest)return;
    state.job=job;state.selectedId=id;localStorage.setItem('foldspace.selectedJob',id);
    // The URL follows the selection (AF DB / SWISS-MODEL stable result-URL
    // convention) so a #job/{id} link opens this workspace directly.
    state.view='studio';location.hash='job/'+id;renderMain();focusPageHeading();
  }catch(e){if(request===state.selectionRequest)toast(e.message,true);}
}
function navigate(view) {
  if(!views[view])return;
  state.selectionRequest++;state.view=view;location.hash=view;renderMain();focusPageHeading();
  if(view==='campaigns'||view==='databases')refreshCampaignData().catch(e=>toast(e.message,true));
}
window.openCampaignFromCluster=async function(campaignId,stage=''){
  if(!campaignId&&stage){
    try{
      const response=await api('/api/campaigns');const campaigns=Array.isArray(response)?response:response.campaigns||[];
      const selected=campaigns.find(c=>c.id===batchState.selectedId&&c.counts?.[stage]>0)||campaigns.find(c=>c.counts?.[stage]>0);
      if(selected)campaignId=selected.id;
      else toast('近期批次没有此阶段目标，可在批次选择器继续检查。');
    }catch(error){toast(error.message,true);}
  }
  if(campaignId){batchState.selectedId=campaignId;localStorage.setItem('foldspace.campaign',campaignId);}
  batchState.stage=stage;batchState.offset=0;navigate('campaigns');
};
async function refreshEnvironment() {try{state.system=await api('/api/system');state.engines=state.system.engines||[];if(state.view==='environment')renderMain();}catch(e){toast(e.message,true);}}
function bindViewEvents() {
  if(state.view==='cluster'&&window.bindClusterEvents)window.bindClusterEvents();
  if(state.view==='storage'&&window.bindStorageEvents)window.bindStorageEvents();
  bindCampaignViewEvents();
  document.querySelectorAll('[data-goto]').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.goto)));
  document.querySelectorAll('[data-new-job]').forEach(el=>el.addEventListener('click',openJobDialog));
  document.querySelectorAll('[data-job-id]').forEach(el=>el.addEventListener('click',()=>selectJob(el.dataset.jobId)));
  document.querySelectorAll('[data-cancel]').forEach(el=>el.addEventListener('click',async()=>{try{await api(`/api/jobs/${el.dataset.cancel}/cancel`,{method:'POST',body:'{}'});toast('已请求取消任务');await refreshJobs();}catch(e){toast(e.message,true);}}));
  document.querySelectorAll('[data-retry]').forEach(el=>el.addEventListener('click',async()=>{try{const job=await api(`/api/jobs/${el.dataset.retry}/retry`,{method:'POST',body:'{}'});toast('已重新提交任务');await refreshJobs();await selectJob(job.id||el.dataset.retry);}catch(e){toast(e.message,true);}}));
  $('#toggle-logs')?.addEventListener('click',()=>$('#inline-logs').classList.toggle('hidden'));
  $('#export-job')?.addEventListener('click',()=>downloadJSON(state.job,`${state.job.id}.json`));
  $('#export-jobs')?.addEventListener('click',()=>downloadJSON(filteredJobs(),'foldspace-filtered-jobs.json'));
  $('#job-search')?.addEventListener('input',e=>{state.search=e.target.value;refreshJobsTable();});
  $('#job-filter')?.addEventListener('change',e=>{state.filter=e.target.value;refreshJobsTable();});
  document.querySelectorAll('[data-style]').forEach(el=>el.addEventListener('click',()=>{state.style=el.dataset.style;if(state.style==='surface'&&state.lowConf)toast('Surface 模式不参与低置信区域显示');document.querySelectorAll('[data-style]').forEach(b=>b.classList.toggle('active',b===el));applyViewerStyle();}));
  $('#color-mode')?.addEventListener('change',e=>{state.color=e.target.value;applyViewerStyle();});
  $('#lowconf-toggle')?.addEventListener('click',()=>{
    const next=(state.lowConf+1)%3, threshold=next===1?70:50;
    if(next&&state.style==='surface'){toast('Surface 模式不参与低置信区域显示');return;}
    if(next&&!lowConfidenceSelection(threshold)){toast('当前视图无逐残基置信度，无法切换低置信显示');return;}
    state.lowConf=next;
    $('#lowconf-toggle').textContent=`低置信显示：${next===1?'淡化 <70':next===2?'隐藏 <50':'关'}`;
    applyViewerStyle();
  });
  $('#model-select')?.addEventListener('change',e=>switchViewerModel(Number(e.target.value)));
  document.querySelectorAll('[data-view-model]').forEach(el=>el.addEventListener('click',()=>switchViewerModel(Number(el.dataset.viewModel))));
  document.addEventListener('click',e=>{const sort=e.target.closest('[data-jobs-sort]');if(sort){state.jobsSort=state.jobsSort==='confidence'?'recent':'confidence';refreshJobsTable();}});
  $('#rotate-view')?.addEventListener('click',()=>{state.spinning=!state.spinning;state.viewer?.spin(state.spinning?'y':false,.3);$('#rotate-view').classList.toggle('active',state.spinning);});
  $('#reset-view')?.addEventListener('click',()=>{state.viewer?.zoomTo();state.viewer?.render();});
  $('#light-view')?.addEventListener('click',()=>{state.light=!state.light;state.viewer?.setBackgroundColor(state.light?'#edf1e9':'#102a34');$('#viewer-container').classList.toggle('light-viewer',state.light);state.viewer?.render();});
  $('#snapshot-view')?.addEventListener('click',()=>{if(!state.viewer)return;const link=document.createElement('a');link.href=state.viewer.pngURI();link.download=`${state.job?.name||'protein'}-structure.png`;link.click();toast('三维视图已导出');});
  $('#fullscreen-view')?.addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('#viewer-container').requestFullscreen();setTimeout(()=>state.viewer?.resize(),150);}catch{toast('当前浏览器未允许全屏');}});
  document.querySelectorAll('[data-residue]').forEach(el=>el.addEventListener('click',()=>{const i=Number(el.dataset.residue),r=getResidues()[i];highlightSequence(i);if(!r||!state.viewer)return;const selection={chain:r.chain||'A',resi:r.residueNumber??r.index??r.resi??i+1};const atoms=state.viewer.getModel()?.selectedAtoms(selection)||[];const atom=atoms.find(a=>a.atom==='CA')||atoms[0];if(atom){selectAtom(atom);state.viewer.zoomTo(selection,600);state.viewer.render();}}));
  $('#refresh-environment')?.addEventListener('click',refreshEnvironment);
}

function parseFASTA(text) {const rows=text.trim().split(/\r?\n/);if(!rows.some(r=>r.trim().startsWith('>')))return [{id:'A',sequence:text.replace(/\s+/g,'').toUpperCase()}];const chains=[];let current=null;for(const line of rows){if(line.trim().startsWith('>')){current={id:String.fromCharCode(65+chains.length),label:line.trim().slice(1).trim(),sequence:''};chains.push(current);}else if(line.trim()){if(!current)throw new Error('FASTA 序列必须位于标题行之后');current.sequence+=line.replace(/\s+/g,'').toUpperCase();}}return chains;}
function updateResidueCount() {try{const chains=parseFASTA($('#sequence-input').value);$('#input-residue-count').textContent=`${chains.reduce((n,c)=>n+c.sequence.length,0)} 个残基 · ${chains.length} ${$('#input-mode')?.value==='batch'?'个独立任务':'条链'}`;}catch{$('#input-residue-count').textContent='请检查 FASTA 格式';}}
async function loadExample() {
  const initialSequence=$('#sequence-input').value;
  const button=$('#load-example-button');button.disabled=true;
  try {
    if(!state.example)state.example=await api('/api/examples/ubiquitin-afdb');
    // A delayed example must not overwrite text entered while the request was pending.
    if($('#sequence-input').value!==initialSequence)return;
    const example=state.example;
    $('#sequence-input').value=(example.chains||[]).map(c=>`>Chain_${c.id}\n${c.sequence}`).join('\n');
    if(!$('#job-name').value)$('#job-name').value=example.name||'Ubiquitin B · 参考结构';updateResidueCount();
  }catch(e){toast(e.message,true);}finally{button.disabled=false;}
}
function jobEngines() {
  const workerMode=state.system.pipeline?.predictionMode==='workers';
  const list=state.engines.length?state.engines:[{id:'reference',name:'参考结构',available:true},...['alphafold3','alphafold2','colabfold'].map(id=>({id,name:engineNames[id],available:false}))];
  return list.map(engine=>workerMode&&engine.id!=='reference'?{...engine,available:false,viaCampaign:true}:engine);
}
function saveEngineDraft() {
  if(!state.parametersEngine)return;
  state.engineDrafts[state.parametersEngine]={seeds:$('#job-seeds').value,samples:$('#job-samples').value,recycles:$('#job-recycles').value};
}
function renderSubmissionRoute() {
  const selected=jobEngines().find(e=>e.id===state.selectedEngine);
  const root=$('#submission-route');if(!root)return;
  const batch=$('#input-mode').value==='batch';
  if(selected?.viaCampaign||batch){
    root.innerHTML=`<div><strong>${selected?.viaCampaign?'常驻模型任务通过批次管线提交':'大规模任务使用批次管线'}</strong><p>${selected?.viaCampaign?'在那里选择序列库与 MSA 策略、执行位置，并跟踪每个目标。':'这里适合最多 200 条独立序列；更多目标可通过文件流式导入并逐项恢复。'} 当前表单内容会保留。</p></div><button type="button" class="button button-secondary" id="open-batch-route">打开批次管线 ↗</button>`;
    $('#open-batch-route').onclick=()=>{$('#job-dialog').close();navigate('campaigns');$('#campaign-name')?.focus();};
  }else if(selected&&!selected.available){
    root.innerHTML='<span>查看引擎缺少的配置与接入方式。</span><button type="button" class="button button-secondary" id="open-engine-route">查看运行环境 ↗</button>';
    $('#open-engine-route').onclick=()=>{$('#job-dialog').close();navigate('environment');};
  }else root.innerHTML='';
}
function renderEngineOptions() {
  const engines=jobEngines();
  $('#engine-options').innerHTML=engines.map(engine=>`<button type="button" class="engine-option ${state.selectedEngine===engine.id?'selected':''} ${!engine.available?'unavailable':''}" data-engine="${escapeHTML(engine.id)}" aria-pressed="${state.selectedEngine===engine.id}" title="${escapeHTML(engine.viaCampaign?'从批次管线提交到常驻模型节点':engine.description||'')}"><i class="engine-dot"></i><strong>${escapeHTML(engineNames[engine.id]||engine.name)}</strong><small>${engine.id==='reference'?'公开结果回放':engine.viaCampaign?'从批次管线提交':engine.available?'已配置运行器':'尚未配置'}</small></button>`).join('');
  $('#engine-options').querySelectorAll('[data-engine]').forEach(el=>el.addEventListener('click',()=>{
    if(state.submitting)return;
    saveEngineDraft();state.selectedEngine=el.dataset.engine;renderEngineOptions();
  }));
  const selected=engines.find(e=>e.id===state.selectedEngine);
  if(state.parametersEngine!==state.selectedEngine){
    const draft=state.engineDrafts[state.selectedEngine]||{seeds:'1',samples:selected?.defaultNumSamples||1,recycles:state.selectedEngine==='alphafold3'?10:3};
    $('#job-seeds').value=draft.seeds;$('#job-samples').value=draft.samples;$('#job-recycles').value=draft.recycles;
    state.parametersEngine=state.selectedEngine;
  }
  $('#job-samples').max=selected?.maxNumSamples||20;
  for(const id of ['job-samples','job-recycles','job-seeds'])$('#'+id).disabled=state.selectedEngine==='reference'||state.submitting;
  document.querySelector('label[for="job-samples"]').textContent=state.selectedEngine==='colabfold'?'每种子模型数量':state.selectedEngine==='alphafold3'?'每种子扩散样本数':state.selectedEngine==='alphafold2'?'每模型预测数':'参考结构数量';
  $('#engine-caption').textContent=state.system.pipeline?.predictionMode==='workers'?'当前为常驻节点模式':'按本地运行器配置';
  $('#engine-note').textContent=state.selectedEngine==='reference'?'参考体验读取已发布结构及真实置信度，支持内置示例的原始序列。使用“载入示例”即可体验完整流程。':selected?.viaCampaign?'真实模型由常驻节点连续处理批次目标。具体可用性由批次管线中的引擎和数据库预检给出。':selected?.available?'任务将由已配置的真实引擎执行，输入与参数随结果保存。':'此引擎尚未配置，可在运行环境中查看接入方式。';
  $('#submit-job-button').disabled=state.submitting||!selected?.available;
  renderSubmissionRoute();
}
function openJobDialog() {$('#form-error').textContent='';renderEngineOptions();$('#job-dialog').showModal();$('#job-dialog').scrollTop=0;if(!$('#sequence-input').value)loadExample();setTimeout(()=>$('#job-name').focus(),50);}
async function showAcceptedJob(job,request) {
  state.jobs=[job,...state.jobs.filter(item=>item.id!==job.id)];
  if(request===state.selectionRequest){
    state.selectionRequest++;state.job=job;state.selectedId=job.id;
    localStorage.setItem('foldspace.selectedJob',job.id);
    state.view='studio';location.hash='studio';renderMain();focusPageHeading();
  }else if(state.view==='jobs')refreshJobsTable();
  try {await refreshJobs();}
  catch(error){setServiceState(error.message);toast(`任务 ${job.id.slice(0,12)} 已接收。列表暂未更新，恢复连接后继续跟踪。`,true);}
}
async function submitJob(event) {event.preventDefault();if(state.submitting||!jobEngines().find(e=>e.id===state.selectedEngine)?.available)return;$('#form-error').textContent='';const button=$('#submit-job-button'),submissionRequest=state.selectionRequest;state.submitting=true;button.disabled=true;button.textContent='正在提交…';$('#job-form').setAttribute('aria-busy','true');try{const chains=parseFASTA($('#sequence-input').value),seeds=$('#job-seeds').value.split(/[,\s]+/).filter(Boolean).map(Number);if(!seeds.length||seeds.some(n=>!Number.isInteger(n)||n<0))throw new Error('随机种子需要是非负整数，用逗号分隔');const payload={name:$('#job-name').value.trim(),engine:state.selectedEngine,chains,seeds,numSamples:Number($('#job-samples').value),numRecycles:Number($('#job-recycles').value),priority:$('#job-priority').value,preset:chains.length>1?'multimer':'monomer',example:'ubiquitin-afdb'};let job;if($('#input-mode').value==='batch'){if(chains.length>200)throw new Error('此表单每批最多 200 条序列，请使用“打开批次管线”流式导入文件');const batch=await api('/api/jobs/batch',{method:'POST',body:JSON.stringify({jobs:chains.map((chain,index)=>({...payload,name:`${payload.name.slice(0,80)} · ${chain.label?.slice(0,30)||index+1}`,chains:[{id:'A',sequence:chain.sequence}],preset:'monomer'}))})});job=batch.jobs[0];toast(`已提交 ${batch.count} 个独立预测任务`);}else{job=await api('/api/jobs',{method:'POST',body:JSON.stringify(payload)});toast('任务已提交，开始跟踪执行进展');}$('#job-dialog').close();await showAcceptedJob(job,submissionRequest);}catch(error){$('#form-error').textContent=error.message;$('#form-error').scrollIntoView({block:'nearest'});}finally{state.submitting=false;$('#job-form').removeAttribute('aria-busy');button.innerHTML=icon('play')+'提交任务';renderEngineOptions();}}

function prepareImport(file) {
  if(!file)return;
  if(file.size>10*1024*1024){toast('结构文件超过10 MiB，请通过运行器接入较大结果',true);return;}
  state.importFile=file;$('#import-filename').textContent=file.name;$('#import-source').value='unknown';$('#confidence-files').value='';$('#import-error').textContent='';$('#import-dialog').showModal();$('#structure-file').value='';
}
async function importStructure(event) {
  event.preventDefault();const file=state.importFile;if(!file)return;const button=$('#confirm-import'),submissionRequest=state.selectionRequest;if(button.disabled)return;button.disabled=true;$('#import-error').textContent='';
  try {
    const text=await file.text(),format=/\.(cif|mmcif)$/i.test(file.name)?'mmcif':'pdb';
    let confidence=null;
    for(const item of $('#confidence-files').files){
      if(item.size>10*1024*1024)throw new Error('单个置信度文件超过10 MiB');
      let data;try{data=JSON.parse(await item.text());}catch{throw new Error(item.name+' 不是有效的 JSON 文件');}
      if(Array.isArray(data))data=data[0];
      if(!data||typeof data!=='object'||Array.isArray(data))throw new Error(item.name+' 应为置信度对象或单对象数组');
      confidence={...(confidence||{}),...data};
    }
    const payload={name:file.name,structureText:text,format,source:$('#import-source').value,...(confidence?{confidence}:{})};
    const body=JSON.stringify(payload);if(new Blob([body]).size>12*1024*1024)throw new Error('结构与置信度的合计请求超过12 MiB，请通过模型运行器接入');
    const job=await api('/api/import',{method:'POST',body});$('#import-dialog').close();state.importFile=null;toast('结构与可用分析结果已导入');await showAcceptedJob(job,submissionRequest);
  }catch(error){$('#import-error').textContent=error.message;}finally{button.disabled=false;}
}

async function refreshJobs() {
  const response=await api('/api/jobs');const jobs=Array.isArray(response)?response:response.jobs||[];const listChanged=JSON.stringify(state.jobs.map(j=>[j.id,j.status,j.updatedAt]))!==JSON.stringify(jobs.map(j=>[j.id,j.status,j.updatedAt]));state.jobs=jobs;$('#nav-job-count').textContent=String(jobs.length);
  if(state.view==='jobs'&&listChanged)refreshJobsTable();
  if(state.selectedId){const listed=jobs.find(j=>j.id===state.selectedId);if(listed&&(!state.job||listed.updatedAt!==state.job.updatedAt||listed.status!==state.job.status)){const selectedId=state.selectedId,request=state.selectionRequest;const job=await api(`/api/jobs/${encodeURIComponent(selectedId)}`);if(state.selectedId===selectedId&&state.selectionRequest===request){state.job=job;if(state.view==='studio')renderMain();}}}
}
function setServiceState(error=null) {
  state.error=error;
  $('#connection-dot').classList.toggle('off',Boolean(error));
  $('#connection-label').textContent=error?'连接暂时中断':'本地服务已连接';
  $('#service-notice').classList.toggle('hidden',!error);
  $('#service-notice-text').textContent=error?'暂时无法更新服务数据，当前内容保留。系统会自动重连，也可立即重试。 '+error:'';
}
async function pollWorkspace() {
  if(state.polling)return;
  state.polling=true;const reconnect=$('#reconnect-service');reconnect.disabled=true;
  try {
    if(state.error){state.system=await api('/api/system');state.engines=state.system.engines||[];window.configurePublicDemo?.(state.system.publicDemo);if($('#job-dialog').open)renderEngineOptions();}
    await refreshJobs();
    if(!state.job&&state.jobs.length){
      const chosen=state.jobs.find(j=>j.status==='completed')||state.jobs[0];
      const request=state.selectionRequest;
      const job=await api(`/api/jobs/${encodeURIComponent(chosen.id)}`);
      if(!state.job&&request===state.selectionRequest){state.selectedId=chosen.id;state.job=job;renderMain();}
    }
    if(state.view==='campaigns'||state.view==='databases')await refreshCampaignData();
    setServiceState();
  }catch(error){setServiceState(error.message);}finally{state.polling=false;reconnect.disabled=false;}
}
async function initialize() {
  hydrateIcons();state.view=views[location.hash.slice(1)]?location.hash.slice(1):'studio';renderMain();
  document.querySelectorAll('.nav-item').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.view)));
  $('#new-job-button').addEventListener('click',()=>{if(['campaigns','cluster'].includes(state.view)){navigate('campaigns');$('#campaign-name')?.focus();}else openJobDialog();});$('#job-form').addEventListener('submit',submitJob);$('#load-example-button').addEventListener('click',loadExample);$('#sequence-input').addEventListener('input',updateResidueCount);$('#input-mode').addEventListener('change',()=>{updateResidueCount();renderSubmissionRoute();});
  document.querySelectorAll('[data-close-dialog]').forEach(el=>el.addEventListener('click',()=>$('#job-dialog').close()));$('#help-button').addEventListener('click',()=>$('#help-dialog').showModal());$('[data-close-help]').addEventListener('click',()=>$('#help-dialog').close());
  $('#import-form').addEventListener('submit',importStructure);document.querySelectorAll('[data-close-import]').forEach(el=>el.addEventListener('click',()=>$('#import-dialog').close()));$('#import-button').addEventListener('click',()=>$('#structure-file').click());$('#structure-file').addEventListener('change',e=>prepareImport(e.target.files[0]));
  window.addEventListener('hashchange',()=>{const jobHash=jobIdFromHash();if(jobHash){if(state.selectedId!==jobHash)selectJob(jobHash);return;}const view=location.hash.slice(1);if(views[view]&&state.view!==view){state.selectionRequest++;state.view=view;renderMain();focusPageHeading();if(view==='campaigns'||view==='databases')refreshCampaignData().catch(e=>toast(e.message,true));}});
  try {
    const [,system]=await Promise.all([api('/api/health'),api('/api/system')]);state.system=system;state.engines=system.engines||[];window.configurePublicDemo?.(system.publicDemo);setServiceState();
    fetch(appUrl('/sources.json')).then(r=>r.ok?r.json():null).then(data=>{const list=Array.isArray(data)?data:data?.sources;state.sources=Array.isArray(list)?list:[];if(state.view==='sources')renderMain();}).catch(()=>{});
    await refreshJobs();
    await refreshCampaignData();
    if(!state.jobs.length){state.autoSeeded=true;state.example=await api('/api/examples/ubiquitin-afdb');const job=await api('/api/jobs',{method:'POST',body:JSON.stringify({name:state.example.name||'Ubiquitin B · 公开参考',engine:'reference',chains:state.example.chains,example:'ubiquitin-afdb',seeds:[1],numSamples:1})});await refreshJobs();state.selectedId=job.id;localStorage.setItem('foldspace.selectedJob',job.id);state.job=await api(`/api/jobs/${job.id}`);renderMain();}
    else {const deep=jobIdFromHash();const previous=localStorage.getItem('foldspace.selectedJob');const chosen=state.jobs.find(j=>j.id===deep)||state.jobs.find(j=>j.id===previous)||state.jobs.find(j=>j.status==='completed')||state.jobs[0];state.selectedId=chosen.id;state.job=await api(`/api/jobs/${chosen.id}`);renderMain();
      if(deep&&deep!==chosen.id){toast('链接指向的任务不存在，已显示最近任务。',true);location.hash='studio';}}
  } catch(error){setServiceState(error.message);renderMain();}
  $('#reconnect-service').addEventListener('click',pollWorkspace);
  setInterval(()=>{if(!document.hidden)pollWorkspace();},2500);
}
initialize();
