'use strict';
// Native SVG/HTML views driven only by the controller snapshot.
(() => {
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list=value=>Array.isArray(value)?value:[];
  const count=value=>Number.isFinite(value)?value.toLocaleString('zh-CN'):'—';
  const reasonLabels={data_plane_disabled:'远程领取未启用',no_registered_worker:'尚未注册节点',no_online_worker:'没有在线节点',all_workers_draining:'节点正在排空',engine_mismatch:'没有匹配引擎',residue_limit:'超过节点长度门槛',sampling_mismatch:'采样或循环设置不匹配',runtime_mismatch:'运行版本不匹配',gpu_groups_busy:'兼容节点执行槽忙'};
  const stageNames={waiting_input:'待准备',preparing:'CPU 准备',ready:'就绪缓冲',predicting:'常驻推理',validating:'结果验证',completed:'已交付',failed:'失败',cancelled:'已取消'};
  const nodeDemo=n=>n.demo||n.mode==='fixture'||n.engine==='reference';
  const stages=data=>data?.pipeline?.counts||data?.pipeline?.stages||null;
  const nvalue=(counts,key)=>typeof counts?.[key]==='number'?counts[key]:counts?.[key]?.count;
  let current=null, selectedBatch=null, returnBatch=null, returnFocus=null;
  const icon=(name)=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({input:'<path d="M5 3h10l4 4v14H5zM14 3v5h5M8 12h8M8 16h5"/>',cpu:'<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4M19 9h4m-4 6h4"/>',cloud:'<path d="M7 18a5 5 0 1 1 1-10 6 6 0 0 1 11 3 3.5 3.5 0 0 1-1 7Z"/>',structure:'<circle cx="6" cy="7" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><path d="m8 9 3 6m3 0 3-6M9 7h6"/>',arrow:'<path d="M4 12h16m-5-5 5 5-5 5"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',check:'<path d="m5 12 4 4L19 6"/>'})[name]||''}</svg>`;
  const value=(data,key)=>nvalue(stages(data),key);
  function topology(data){
    const nodes=list(data?.nodes), online=nodes.filter(n=>n.status==='online');
    const real=online.filter(n=>!nodeDemo(n));
    const devices=location=>real.filter(n=>n.location===location).reduce((s,n)=>s+list(n.devices).length,0);
    const active=list(data?.batches).filter(b=>b.status==='running'&&b.remaining>0);
    const busy=location=>active.some(b=>b.location===location);
    const planned=Number.isInteger(data?.planned?.localGpuCards)?data.planned.localGpuCards:0;
    const shown=Math.min(32,planned), local=online.filter(n=>n.location==='local'), cloud=online.filter(n=>n.location==='aliyun');
    const slots=Array.from({length:shown},(_,i)=>`<span class="cv-gpu-slot ${i<devices('local')?'registered':''}" title="${i<devices('local')?'节点上报设备':'规划卡位'} ${i+1}">${String(i+1).padStart(2,'0')}</span>`).join('');
    const pending=(value(data,'waiting_input')||0)+(value(data,'ready')||0)>0;
    const working=(value(data,'preparing')||0)+(value(data,'predicting')||0)+(value(data,'validating')||0)>0||active.length>0;
    return `<section class="panel cv-topology"><header class="cv-heading"><div><span class="cv-eyebrow">LIVE EXECUTION MAP</span><h3>一份清单，本地与云端并行推进。</h3></div><div class="cv-live-state ${working?'working':''}"><i></i>${working?'任务正在流动':pending?'任务等待匹配节点':online.length?'节点就绪 · 等待新工作':'等待执行节点上线'}</div></header>
      <div class="cv-map ${working?'is-working':''}">
        <svg class="cv-routes" viewBox="0 0 1000 340" preserveAspectRatio="none" aria-hidden="true"><defs><marker id="cv-arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="m1 1 5 3-5 3" fill="none" stroke="#73a38b"/></marker></defs><path d="M228 170H290Q310 170 310 105H365M228 170H290Q310 170 310 265H365M680 105H720Q740 105 740 170H795M680 265H720Q740 265 740 170H795"/><path class="cv-route-motion" d="M228 170H290Q310 170 310 105H365M228 170H290Q310 170 310 265H365M680 105H720Q740 105 740 170H795M680 265H720Q740 265 740 170H795"/></svg>
        <div class="cv-source"><div class="cv-map-card"><span class="cv-card-overline">01 / PREPARE</span><div class="cv-card-icon">${icon('input')}</div><h4>序列与特征准备</h4><p>MSA 搜索 · 模板 · 缓存</p><div class="cv-data-chips"><span>A3M</span><span>JSON</span><span>SHA-256</span></div><div class="cv-map-count"><b>${count(value(data,'preparing'))}</b><span>准备中</span><b>${count(value(data,'ready'))}</b><span>待领取</span></div></div><span class="cv-map-caption">序列库留在准备侧</span></div>
        <div class="cv-compute"><button class="cv-lane ${busy('local')?'busy':''}" data-cv-location="local"><div class="cv-lane-top"><span>${icon('cpu')}本地常驻执行池</span><i>${!local.length?'未上线':busy('local')?'执行中':'待命'}</i></div><div class="cv-gpu-grid">${slots||'<span class="cv-no-plan">未配置卡位计划</span>'}${planned>32?`<span>+${planned-32}</span>`:''}</div><div class="cv-lane-meta"><span>${count(planned)} 卡规划 · ${devices('local')} 上报设备</span><b>${local.length} 在线进程${local.some(nodeDemo)?' · 含演示':''}</b></div></button>
        <button class="cv-lane cv-cloud ${busy('aliyun')?'busy':''}" data-cv-location="aliyun"><div class="cv-lane-top"><span>${icon('cloud')}阿里云弹性执行池</span><i>${!cloud.length?'未上线':busy('aliyun')?'执行中':'待命'}</i></div><div class="cv-cloud-body"><span class="cv-cloud-glyph">${icon('cloud')}</span><div><strong>${cloud.length}<small>在线进程</small></strong><span>${devices('aliyun')} 个上报 GPU 设备${cloud.some(nodeDemo)?' · 含本机演示':''}</span></div><span class="cv-resident-tag">MODEL RESIDENT</span></div></button></div>
        <div class="cv-sink"><div class="cv-map-card"><span class="cv-card-overline">03 / DELIVER</span><div class="cv-structure-glyph">${icon('structure')}</div><h4>逐项校验与交付</h4><p>结构 · 置信度 · 执行来源</p><div class="cv-delivered"><b>${count(value(data,'completed'))}</b><span>已交付目标</span></div><span class="cv-validation">${icon('check')}SHA-256 + 租约验证</span></div><span class="cv-map-caption">完成一项，交付一项</span></div>
      </div><div class="cv-map-legend"><span><i class="registered"></i>节点上报设备</span><span><i></i>尚未注册的规划卡位</span><span>实线为执行关系，动画随队列执行状态启停</span></div></section>`;
  }
  function overview(data){
    const counts=stages(data), pipeline=data?.pipeline;
    if(!counts)return `<section class="panel cv-overview"><div class="cv-heading"><h3>管线全局态势</h3><span>等待控制面提供阶段汇总</span></div></section>`;
    const keys=['waiting_input','preparing','ready','predicting','validating','completed','failed'];
    const total=keys.reduce((sum,key)=>sum+(nvalue(counts,key)||0),0)+(nvalue(counts,'cancelled')||0);
    const done=nvalue(counts,'completed')||0;
    const occupancy=pipeline.queue||pipeline.buffer||pipeline.backpressure||{};
    const used=occupancy.used??occupancy.occupied??((nvalue(counts,'preparing')||0)+(nvalue(counts,'ready')||0));
    const cap=occupancy.highWaterMark??occupancy.cap??occupancy.readyCap??pipeline.readyCap;
    const recent=pipeline.recent||{};
    const locations=pipeline.locations||{};
    const diagnostics=Object.entries(locations).filter(([key])=>['local','aliyun'].includes(key));
    return `<section class="panel cv-overview"><header class="cv-heading"><div><span class="cv-eyebrow">GLOBAL PIPELINE</span><h3>从积压到交付，每个阶段都可追踪。</h3></div><span class="cv-quiet">全部账本 · ${count(total)} 个目标</span></header><div class="cv-stage-ribbon">${keys.map((key,i)=>`<button class="cv-stage ${key==='failed'?'error':''}" data-cv-stage="${key}" title="打开批次管线检查${stageNames[key]}阶段"><small>${String(i+1).padStart(2,'0')}</small><strong>${count(nvalue(counts,key))}</strong><span>${stageNames[key]}</span><i></i></button>`).join('')}</div><div class="cv-overview-footer"><div class="cv-delivery-bar"><span>交付率 <b>${total?(100*done/total).toFixed(1):'0.0'}%</b></span><div><i style="width:${total?100*done/total:0}%"></i></div></div><div class="cv-buffer"><span>可路由就绪与在途准备</span><b>${count(used)} / ${count(cap)}</b><small>有界缓冲</small></div><button class="cv-open-campaigns" data-cv-stage="">查看批次与目标 ${icon('arrow')}</button></div><div class="cv-recent"><span>最近 ${count(recent.windowSeconds?recent.windowSeconds/60:undefined)} 分钟</span><span>交付 <b>${count(recent.completed?.targets)}</b></span><span>失败目标 <b>${count(recent.failed?.targets)}</b></span><span>失联恢复目标 <b>${count(recent.recovered?.targets)}</b></span><small>目标数去重 · 含参考回放</small></div>${diagnostics.length?`<div class="cv-routing">${diagnostics.map(([location,d])=>`<div><span>${location==='local'?'本地':'阿里云'}领取检查</span><strong>${count(d.eligibleReady)} <small>/ ${count(d.readyCandidates)} 就绪目标当前可领取</small></strong><p>${list(d.reasons).filter(r=>r.count>0).map(r=>`<span>${esc(reasonLabels[r.code]||r.label||r.code)} <b>${count(r.count)}</b></span>`).join('')||'当前没有受阻的就绪目标'}</p></div>`).join('')}</div><div class="cv-routing-note">混合目标可同时匹配两侧，位置计数不相加；暂停批次不进入领取检查。仅为在线兼容节点预取准备数据；暂无匹配节点时保持待准备并保留缓存。</div>`:''}</section>`;
  }
  function batchDetail(data,id){
    const batch=list(data?.batches).find(b=>b.id===id);
    if(!batch)return '<p class="cv-empty-detail">该批次已离开近期窗口，完整目标仍可在批次管线中查看。</p>';
    const node=list(data.nodes).find(n=>n.id===batch.nodeId);
    return `<div class="cv-inspector-summary"><div><small>执行节点</small><strong>${esc(node?.name||batch.nodeId)}</strong></div><div><small>批次状态</small><strong>${batch.status==='running'?'执行中':'已结束'}</strong></div><div><small>已接收 / 总数</small><strong>${count(batch.completed)} / ${count(batch.total)}</strong></div></div><p class="cv-detail-id">${esc(batch.id)}</p><div class="cv-members">${list(batch.members).map(m=>`<button class="cv-member" data-cv-item="${esc(m.itemId)}" data-cv-campaign="${esc(m.campaignId)}"><span class="cv-member-dot ${esc(m.stage)}"></span><div><strong>${esc(m.name||m.targetId||m.itemId)}</strong><small>${esc(m.campaignName||m.campaignId||'')} · ${m.leaseActive?'租约有效':'无活动租约'}</small></div><span>${stageNames[m.stage]||esc(m.stage)}</span>${icon('arrow')}</button>`).join('')||'<p class="cv-empty-detail">没有可用的成员记录</p>'}</div><p class="cv-detail-note">已接收表示结果包通过提交检查，已交付表示独立验证完成。每项目标有独立租约。</p>`;
  }
  function openBatch(id){
    selectedBatch=id;
    returnBatch=id;returnFocus=document.activeElement;
    let dialog=document.getElementById('cv-batch-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='cv-batch-dialog';dialog.className='cv-dialog';dialog.setAttribute('aria-labelledby','cv-batch-title');dialog.innerHTML=`<div class="cv-dialog-head"><div><span class="cv-eyebrow">RESIDENT BATCH INSPECTOR</span><h2 id="cv-batch-title">常驻批次详情</h2></div><button data-cv-close aria-label="关闭批次详情">${icon('close')}</button></div><div id="cv-batch-body"></div>`;document.body.append(dialog);dialog.addEventListener('click',event=>{if(event.target===dialog||event.target.closest('[data-cv-close]'))dialog.close();const target=event.target.closest('[data-cv-item]');if(target){dialog.close();window.inspectCampaignTarget?.(target.dataset.cvCampaign,target.dataset.cvItem);}});dialog.addEventListener('close',()=>{selectedBatch=null;const page=document.getElementById('cluster-page');if(page){const trigger=returnBatch?page.querySelector(`[data-cv-batch="${CSS.escape(returnBatch)}"]`):null;(trigger||(returnFocus?.isConnected?returnFocus:null)||page.querySelector('#cluster-refresh'))?.focus({preventScroll:true});}returnFocus=null;returnBatch=null;});}
    dialog.querySelector('#cv-batch-body').innerHTML=batchDetail(current,id);
    if(!dialog.open)dialog.showModal();
  }
  function update(data){
    current=data;
    const topologyRoot=document.getElementById('cluster-topology');
    if(topologyRoot){const focused=document.activeElement?.dataset.cvLocation;topologyRoot.innerHTML=topology(data);if(focused)topologyRoot.querySelector(`[data-cv-location="${focused}"]`)?.focus({preventScroll:true});}
    const queue=document.getElementById('cluster-overview');if(queue){const focused=document.activeElement?.dataset.cvStage;queue.innerHTML=overview(data);if(focused!==undefined)queue.querySelector(`[data-cv-stage="${focused}"]`)?.focus({preventScroll:true});}
    if(selectedBatch&&document.getElementById('cv-batch-dialog')?.open){const body=document.getElementById('cv-batch-body'),scroll=body.scrollTop,focused=document.activeElement?.dataset.cvItem;body.innerHTML=batchDetail(data,selectedBatch);body.scrollTop=scroll;if(focused)body.querySelector(`[data-cv-item="${CSS.escape(focused)}"]`)?.focus({preventScroll:true});}
  }
  function bind(root){
    root.addEventListener('click',event=>{
      const location=event.target.closest('[data-cv-location]');
      if(location){root.querySelector(`[data-cluster-location="${location.dataset.cvLocation}"]`)?.click();root.querySelector('#cluster-nodes')?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'center'});}
      const stage=event.target.closest('[data-cv-stage]');if(stage)window.openCampaignFromCluster?.(null,stage.dataset.cvStage);
      const batch=event.target.closest('[data-cv-batch]');if(batch)openBatch(batch.dataset.cvBatch);
    });
  }
  window.FoldspaceClusterVisuals={update,bind,openBatch};
})();
