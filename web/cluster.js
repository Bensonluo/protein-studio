'use strict';

// Independent page module. All resource records come from /api/cluster.
(() => {
  const ui = {data:null, filter:'all', query:'', auto:true, timer:null, request:null, busy:false, generation:0};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
  const list = value => Array.isArray(value) ? value : [];
  const root = () => document.getElementById('cluster-page');
  const date = value => {
    if (value == null) return '—';
    const d = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  };
  const bytes = value => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    if (value < 1024) return `${value} B`;
    const power = Math.min(4,Math.floor(Math.log(value)/Math.log(1024)));
    return `${(value/1024**power).toFixed(1)} ${['B','KiB','MiB','GiB','TiB'][power]}`;
  };
  const symbol = name => {
    const paths = {node:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h.01M7 17h.01M12 6h6M12 17h6"/>',cloud:'<path d="M7 18a5 5 0 1 1 1-10 6 6 0 0 1 11 3 3.5 3.5 0 0 1-1 7Z"/>',cpu:'<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4M19 9h4m-4 6h4"/>',transfer:'<path d="M3 7h17m-5-5 5 5-5 5M21 17H4m5-5-5 5 5 5"/>',shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',refresh:'<path d="M20 8a8 8 0 1 0 .5 7M20 3v5h-5"/>',pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>'};
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.node}</svg>`;
  };
  const locations = {local:'本地',aliyun:'阿里云'};
  const engines = {reference:'公开结构回放',alphafold3:'AlphaFold 3',colabfold:'ColabFold',alphafold2:'AlphaFold 2'};
  const states = {online:'在线',offline:'离线',completed:'已完成',running:'执行中',leased:'已领取',active:'执行中',pending:'等待',failed:'失败',expired:'租约过期',retrying:'重试中',verified:'校验通过',transferred:'已传输',uploaded:'已上传',downloaded:'已下载',sent:'已发送',accepted:'已接收',rejected:'已拒绝',cancelled:'已取消',abandoned:'已回收',finished:'批次结束',validated:'验证完成'};
  const pill = status => `<span class="cluster-pill ${['online','completed','verified','accepted'].includes(status)?'good':['offline','failed','rejected','expired'].includes(status)?'warn':''}"><i></i>${esc(states[status]||status||'未报告')}</span>`;
  const empty = (title,text) => `<div class="cluster-empty">${symbol('node')}<strong>${esc(title)}</strong><p>${esc(text)}</p></div>`;
  const isDemo = node => node.demo === true || node.mode === 'fixture' || node.engine === 'reference';
  const nodeMatch = node => (ui.filter === 'all' || node.location === ui.filter) && (!ui.query || [node.id,node.name,node.engine,list(node.devices).join(' ')].join(' ').toLowerCase().includes(ui.query.toLowerCase()));
  const nodeName = id => {const node=list(ui.data?.nodes).find(n=>n.id===id);return node?.name||id||'未分配';};
  const short = (value,length=14) => typeof value === 'string' && value.length > length ? value.slice(0,length)+'…' : value||'—';

  function metric(label,value,note,type='') {
    return `<article class="cluster-metric ${type}"><span>${label}</span><strong>${value}</strong><small>${note}</small></article>`;
  }
  function metrics() {
    const data=ui.data, nodes=list(data?.nodes), online=nodes.filter(n=>n.status==='online');
    const planned=data?.planned?.localGpuCards;
    return metric('本地规划资源',number(planned),'GPU 卡 · 配置计划','planned')+
      metric('本地在线节点',number(online.filter(n=>n.location==='local').length),'实际注册并保持心跳')+
      metric('阿里云位置在线节点',number(online.filter(n=>n.location==='aliyun').length),'位置由管理员注册配置')+
      metric('常驻进程已处理目标',number(nodes.reduce((sum,n)=>sum+(Number.isFinite(n.processedTargets)?n.processedTargets:0),0)),'节点上报累计数 · 含回放');
  }
  function flow() {
    const data=ui.data, batches=list(data?.batches), transfers=list(data?.transfers), nodes=list(data?.nodes);
    const count=(direction)=>transfers.filter(t=>t.direction===direction).length;
    const cards=[
      ['cpu','01','本地 CPU 准备','序列库搜索 · MSA / 模板','完整库留在准备侧'],
      ['transfer','02','准备包下发','清单 + 文件 SHA-256',`${number(count('download'))} 条近期下发记录`],
      ['node','03','常驻模型执行','同一进程连续处理目标',`${number(nodes.filter(n=>n.status==='online'&&n.loaded).length)} 个在线进程报告已加载`],
      ['cloud','04','逐项结果回传','结构 / 置信度 / manifest',`${number(count('upload'))} 条近期回传记录`],
      ['shield','05','校验并交付','校验通过后发布结果',`${number(batches.reduce((s,b)=>s+list(b.members).filter(m=>m.stage==='completed').length,0))} 个近期批次已完成目标`]
    ];
    return cards.map(([ico,index,title,desc,detail])=>`<div class="cluster-flow-step"><div class="cluster-flow-icon">${symbol(ico)}<span>${index}</span></div><h3>${title}</h3><p>${desc}</p><small>${detail}</small></div>`).join('');
  }
  function nodeCards() {
    const all=list(ui.data?.nodes), nodes=all.filter(nodeMatch);
    if (!all.length) return empty('还没有节点注册',`本地 ${number(ui.data?.planned?.localGpuCards)} 卡是规划资源。启动节点 agent 并收到心跳后，这里才会出现实际执行节点。`);
    if (!nodes.length) return empty('没有符合筛选的节点','调整位置或节点名称筛选。');
    return nodes.map(node=>{
      const demo=isDemo(node), devices=list(node.devices), groups=list(node.gpuGroups);
      const loads=node.modelInitCount ?? node.modelLoads;
      return `<article class="cluster-node"><header><div class="cluster-node-icon ${node.location==='aliyun'?'cloud':''}">${symbol(node.location==='aliyun'?'cloud':'node')}</div><div class="cluster-node-heading"><span>${esc(locations[node.location]||node.location||'位置未报告')}${demo?' · 演示节点':''}</span><h3>${esc(node.name||node.id)}</h3></div>${pill(node.status)}</header><div class="cluster-node-engine">${esc(engines[node.engine]||node.engine||'引擎未报告')}<span>${demo?'REFERENCE · 无 GPU 推理':'WORKER REPORTED'}</span></div><div class="cluster-node-facts"><div><small>进程 PID</small><b>${esc(node.pid??'—')}</b></div><div><small>${demo?'回放运行时初始化':'模型初始化次数'}</small><b>${number(loads)}</b></div><div><small>已处理目标</small><b>${number(node.processedTargets)}</b></div><div><small>驻留状态</small><b>${node.loaded===true?'已加载':node.loaded===false?'未加载':'未报告'}</b></div></div><div class="cluster-device-line"><span>设备</span><code>${esc(devices.length?devices.map(d=>typeof d==='object'?JSON.stringify(d):d).join(' · '):demo?'无需 GPU':'未上报设备')}</code></div><footer><span>最近心跳</span><time>${esc(date(node.lastHeartbeat))}</time></footer><details data-cluster-detail="node-${esc(node.id)}"><summary>节点身份与运行依据</summary><dl><dt>节点 ID</dt><dd>${esc(node.id)}</dd><dt>GPU 组</dt><dd>${esc(groups.length?JSON.stringify(groups):'未配置 / 未报告')}</dd><dt>输入长度上限</dt><dd>${number(node.maxResidues)} residues · 管理员/运行器限制，非显存实测保证</dd><dt>权重加载次数</dt><dd>${number(node.modelLoads)}</dd><dt>运行依据</dt><dd>${esc(node.evidence||'节点上报，控制面未独立验证硬件')}</dd></dl></details></article>`;
    }).join('');
  }
  function related(record) {
    if (ui.filter==='all' && !ui.query) return true;
    return list(ui.data?.nodes).some(node=>node.id===record.nodeId && nodeMatch(node));
  }
  function batches() {
    const rows=list(ui.data?.batches).filter(related);
    if (!rows.length) return empty('还没有执行批次记录','准备完成的目标被常驻节点领取后，将显示所属节点、剩余目标与完成进度。');
    return `<div class="cluster-table-wrap" data-cluster-scroll="batches"><table class="cluster-table"><thead><tr><th>批次 / 节点</th><th>状态</th><th>逐项目标进度</th><th>领取时间</th></tr></thead><tbody>${rows.map(batch=>{
      const total=Number.isFinite(batch.total)?batch.total:0, done=Number.isFinite(batch.completed)?batch.completed:0, percent=total>0?Math.min(100,Math.max(0,done/total*100)):0;
      return `<tr><td><button class="cv-batch-link" data-cv-batch="${esc(batch.id)}" title="查看批次 ${esc(batch.id)}">${esc(short(batch.id))} ↗</button><small>${esc(nodeName(batch.nodeId))}</small>${batch.gpuGroup?`<small>GPU 组 ${esc(JSON.stringify(batch.gpuGroup))}</small>`:''}</td><td>${pill(batch.status)}</td><td><div class="cluster-progress-label"><b>回传 ${number(batch.completed)} / ${number(batch.total)}</b><span>剩余 ${number(batch.remaining)}</span></div><div class="cluster-progress" role="progressbar" aria-label="已完成目标" aria-valuenow="${done}" aria-valuemin="0" aria-valuemax="${total}"><span style="width:${percent}%"></span></div></td><td>${esc(date(batch.createdAt))}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function transfers() {
    const rows=list(ui.data?.transfers).filter(related);
    if (!rows.length) return empty('等待真实数据传输','下发准备包和上传结果都会记录方向、字节数、SHA-256 与接收状态。');
    return `<div class="cluster-table-wrap" data-cluster-scroll="transfers"><table class="cluster-table"><thead><tr><th>方向 / 节点</th><th>传输量</th><th>SHA-256 / 标识</th><th>状态</th></tr></thead><tbody>${rows.map(t=>`<tr><td><strong class="cluster-transfer-direction">${t.direction==='download'?'↓ 准备侧 → 节点':t.direction==='upload'?'↑ 节点 → 控制面':esc(t.direction||'未报告')}</strong><small>${esc(nodeName(t.nodeId))}</small><small>${esc(date(t.createdAt))}</small></td><td>${bytes(t.bytes)}</td><td><details data-cluster-detail="transfer-${esc(t.id)}"><summary><code>${esc(short(t.sha256,18))}</code></summary><dl><dt>完整 SHA-256</dt><dd><code>${esc(t.sha256||'未报告')}</code></dd><dt>批次 / 目标</dt><dd>${esc(t.batchId||'—')} / ${esc(t.itemId||'—')}</dd><dt>传输 ID</dt><dd>${esc(t.id)}</dd></dl></details></td><td>${pill(t.status)}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function events() {
    const rows=list(ui.data?.events).filter(e=>!e.nodeId || related(e)).slice(0,30);
    if (!rows.length) return `<div class="cluster-events-empty">${symbol('shield')}<div><strong>暂无恢复与重试事件</strong><p>节点失联、租约过期或重新领取发生后显示真实记录。</p></div></div>`;
    const names={predicting:'节点领取目标',validating:'逐项结果已接收',ready:'任务恢复就绪',lease_recovered:'失联租约已回收',retrying:'重新尝试'};
    const message=e=>e.message==='Lease revoked; safe stage recovery.'?'失联目标的租约已回收，未交付目标重新就绪；已接收结果继续验证。':e.message==='Verified remote output bundle accepted; independent CPU validation queued.'?'结果包与 SHA-256 校验通过，进入独立结果验证队列。':String(e.message||'').startsWith('Remote worker claimed target:')?'节点已领取目标，使用当前租约处理并回传。':e.message||e.detail||'';
    return `<ol class="cluster-events" data-cluster-scroll="events">${rows.map(e=>`<li><i></i><div><strong>${esc(names[e.type]||e.type||e.event||'执行事件')}</strong><p>${esc(message(e))}</p><small>${esc(e.nodeId?nodeName(e.nodeId):'控制面')}${e.batchId?' · '+esc(short(e.batchId)):''}${e.itemId?' · 目标 '+esc(short(e.itemId,10)):''}</small></div><time>${esc(date(e.timestamp||e.createdAt||e.time))}</time></li>`).join('')}</ol>`;
  }
  function updatePart(id,html) {
    const element=document.getElementById(id);
    if (!element) return;
    const active=document.activeElement;
    // Polling replaces these controls. Preserve the current semantic control,
    // rather than sending keyboard users back to the document on every update.
    const batch=element.contains(active)?active?.dataset.cvBatch:null;
    const detail=element.contains(active)&&active?.matches('summary')?active.closest('[data-cluster-detail]')?.dataset.clusterDetail:null;
    const opened=new Set([...element.querySelectorAll('details[open][data-cluster-detail]')].map(d=>d.dataset.clusterDetail));
    const scrolls=new Map([...element.querySelectorAll('[data-cluster-scroll]')].map(d=>[d.dataset.clusterScroll,[d.scrollLeft,d.scrollTop]]));
    element.innerHTML=html;
    element.querySelectorAll('details[data-cluster-detail]').forEach(d=>{d.open=opened.has(d.dataset.clusterDetail);});
    element.querySelectorAll('[data-cluster-scroll]').forEach(d=>{const position=scrolls.get(d.dataset.clusterScroll);if(position){d.scrollLeft=position[0];d.scrollTop=position[1];}});
    if(batch!==null&&batch!==undefined)element.querySelector(`[data-cv-batch="${CSS.escape(batch)}"]`)?.focus({preventScroll:true});
    else if(detail)element.querySelector(`[data-cluster-detail="${CSS.escape(detail)}"] > summary`)?.focus({preventScroll:true});
  }
  function paint() {
    if (!root()) return;
    updatePart('cluster-metrics',metrics());updatePart('cluster-flow',flow());updatePart('cluster-nodes',nodeCards());
    updatePart('cluster-batches',batches());updatePart('cluster-transfers',transfers());updatePart('cluster-events',events());window.FoldspaceClusterVisuals?.update(ui.data);
    const count=document.getElementById('cluster-node-count');if(count)count.textContent=`${list(ui.data?.nodes).filter(nodeMatch).length} 个节点`;
    const status=document.getElementById('cluster-mode-note');if(status)status.textContent=ui.data?.enabled?'节点接入已启用 · 列表来自实时注册与心跳':'节点接入尚未启用 · 规划资源不计为在线产能';
    const protocol=document.getElementById('cluster-transport-note');if(protocol)protocol.textContent=ui.data?.topology?.transport||'等待控制面返回传输协议';
  }
  window.renderCluster = function renderCluster() {
    return `<div id="cluster-page"><section class="cluster-hero"><div><span class="cluster-eyebrow">HYBRID EXECUTION · RESIDENT WORKERS</span><h2>数据准备一次，模型持续工作。</h2><p>本地资源与阿里云节点分片执行，逐项回传可校验的结构结果。</p><span id="cluster-mode-note" class="cluster-mode-note">正在读取真实节点状态</span></div><div class="cluster-hero-orbit" aria-hidden="true"><span>${symbol('node')}</span><i></i><span>${symbol('cloud')}</span></div></section><div id="cluster-error" class="cluster-error" role="status" hidden></div><div id="cluster-topology"></div><div id="cluster-overview"></div><div class="cluster-metrics" id="cluster-metrics">${metric('本地规划资源','—','等待控制面配置')}${metric('本地在线节点','—','等待实际心跳')}${metric('阿里云位置在线节点','—','等待实际心跳')}${metric('常驻进程已处理目标','—','等待节点上报')}</div><details class="panel cluster-flow-panel cv-contract"><summary>五阶段数据契约与传输窗口</summary><div class="cluster-section-heading"><div><span class="cluster-kicker">DATA → COMPUTE → VERIFIED RESULTS</span><h3>一条可追溯的执行链</h3></div><span class="cluster-transport" id="cluster-transport-note">准备包与结果包均校验哈希</span></div><div class="cluster-flow" id="cluster-flow">${flow()}</div><p class="cluster-note">执行批次是在同一个驻留进程中连续处理多个目标；是否支持模型张量 minibatch 或组内多卡，由实际引擎决定。</p></details><div class="cluster-toolbar"><div class="cluster-location-tabs" role="group" aria-label="节点位置筛选"><button data-cluster-location="all">全部位置</button><button data-cluster-location="local">本地</button><button data-cluster-location="aliyun">阿里云</button></div><label class="cluster-search">${symbol('search')}<input id="cluster-search" type="search" placeholder="节点名称 / 引擎" aria-label="搜索节点" value="${esc(ui.query)}"></label><label class="cluster-auto"><input id="cluster-auto" type="checkbox" ${ui.auto?'checked':''}>自动刷新</label><button class="button cluster-refresh" id="cluster-refresh">${symbol('refresh')}刷新</button></div><section class="cluster-node-section"><div class="cluster-section-heading"><h3>已注册执行节点 <span id="cluster-node-count">0 个节点</span></h3><time id="cluster-last-updated">等待更新</time></div><div class="cluster-node-grid" id="cluster-nodes">${empty('正在读取节点注册表','不会把规划卡数画成已上线的设备。')}</div></section><div class="cluster-ledger-grid"><section class="panel"><div class="cluster-section-heading"><div><span class="cluster-kicker">RESIDENT BATCHES</span><h3>节点执行批次</h3></div><span class="cluster-window-note">最近 50 条</span></div><div id="cluster-batches"></div></section><section class="panel"><div class="cluster-section-heading"><div><span class="cluster-kicker">HASHED TRANSFERS</span><h3>数据传输与校验</h3></div><span class="cluster-window-note">最近 100 条</span></div><div id="cluster-transfers"></div></section></div><section class="panel cluster-events-panel"><div class="cluster-section-heading"><div><span class="cluster-kicker">LEASES & RECOVERY</span><h3>失联恢复与重试</h3></div><span class="cluster-window-note">保留已完成目标，恢复未完成工作</span></div><div id="cluster-events"></div></section><p class="cluster-footnote">在线状态来自心跳；设备、PID 与模型驻留次数来自节点上报。标记为“演示节点”的 local / aliyun 记录只用于验证真实 HTTP 传输与公开结构回放，不代表已经在阿里云购买实例或执行 GPU 模型。</p></div>`;
  };
  window.refreshCluster = async function refreshCluster() {
    if (!root() || ui.busy) return;
    const generation=ui.generation;ui.busy=true;const controller=new AbortController();ui.request=controller;
    const button=document.getElementById('cluster-refresh');if(button)button.disabled=true;
    try {
      const response=await fetch('/api/cluster',{signal:controller.signal,cache:'no-store'});
      const data=await response.json();
      if (!response.ok) throw new Error(data.error||`读取失败 (${response.status})`);
      if (!data || !Array.isArray(data.nodes)) throw new Error('控制面返回的节点数据格式不正确');
      if (generation!==ui.generation || !root()) return;
      ui.data=data;paint();
      const error=document.getElementById('cluster-error');if(error)error.hidden=true;
      const stamp=document.getElementById('cluster-last-updated');if(stamp)stamp.textContent='更新于 '+date(Date.now());
    } catch(error) {
      if(error.name!=='AbortError' && generation===ui.generation && root()) {
        const element=document.getElementById('cluster-error');element.hidden=false;element.textContent='暂时无法更新节点状态：'+error.message+'。保留上次数据。';
      }
    } finally {
      if(generation===ui.generation){ui.busy=false;ui.request=null;const activeButton=document.getElementById('cluster-refresh');if(activeButton)activeButton.disabled=false;}
    }
  };
  window.stopClusterRefresh = function stopClusterRefresh() {
    ui.generation++;if(ui.timer)clearInterval(ui.timer);ui.timer=null;if(ui.request)ui.request.abort();ui.request=null;ui.busy=false;
  };
  window.bindClusterEvents = function bindClusterEvents() {
    window.stopClusterRefresh();const page=root();if(!page)return;window.FoldspaceClusterVisuals?.bind(page);
    page.querySelectorAll('[data-cluster-location]').forEach(button=>{
      button.classList.toggle('active',button.dataset.clusterLocation===ui.filter);
      button.onclick=()=>{ui.filter=button.dataset.clusterLocation;page.querySelectorAll('[data-cluster-location]').forEach(b=>b.classList.toggle('active',b.dataset.clusterLocation===ui.filter));paint();};
    });
    page.querySelector('#cluster-search').oninput=event=>{ui.query=event.target.value;paint();};
    page.querySelector('#cluster-auto').onchange=event=>{ui.auto=event.target.checked;};
    page.querySelector('#cluster-refresh').onclick=()=>window.refreshCluster();
    if(ui.data)paint();window.refreshCluster();
    ui.timer=setInterval(()=>{if(!root()){window.stopClusterRefresh();return;}if(ui.auto && !document.hidden)window.refreshCluster();},4000);
  };
})();
