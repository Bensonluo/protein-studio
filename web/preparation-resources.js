/* Preparation admission estimates, deliberately separate from measured usage. */
(() => {
  const known=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
  const count=value=>known(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:2}):'未声明';
  const bytes=value=>{
    if(!known(value))return '未声明';
    if(value===0)return '0 B';
    const units=['B','KiB','MiB','GiB','TiB'],power=Math.min(4,Math.floor(Math.log(value)/Math.log(1024)));
    return `${(value/1024**power).toLocaleString('zh-CN',{maximumFractionDigits:1})} ${units[power]}`;
  };
  window.renderPreparationResourcesPanel=function(config){
    const data=config?.preparationResources;
    if(!data)return '';
    const limits=data.limits||{},estimates=data.estimates||{},inflight=data.inflight||{},disk=data.disk||{};
    const esc=value=>escapeHTML(String(value??''));
    const reasonLabels={cpu_budget:'CPU 预算已预留，等待在途准备释放',memory_budget:'内存估计预留不足以再启动一次准备',disk_free:'可用磁盘不足以保留底线并启动下一次准备',disk_probe:'无法确认准备目录的可用磁盘空间'};
    const reasons=Array.isArray(data.blockedReasons)?data.blockedReasons:[];
    const waiting=known(data.waiting)?data.waiting:0;
    const status=!data.configured?'预算未配置':data.wouldBlock?'等待可用预算':'可接受准备工作';
    const cards=[
      ['cpu','CPU 估计预留',`${count(inflight.cpuReserved)} / ${count(limits.cpuBudget)}`,`每次准备 ${count(estimates.cpuPerPrepare)} 核`],
      ['memory','内存估计预留',`${bytes(inflight.memoryReservedBytes)} / ${bytes(limits.memoryBudgetBytes)}`,`每次准备 ${bytes(estimates.memoryPerPrepareBytes)}`],
      ['disk','磁盘可用空间',bytes(disk.minFreeBytes),`预留 ${bytes(inflight.diskReservedBytes)} · 保留底线 ${bytes(limits.diskMinFreeBytes)}`],
      ['admission','准备工作预留',count(inflight.count),`${count(waiting)} 个准备线程等待准入`]
    ];
    return `<section class="preparation-budget ${data.wouldBlock?'constrained':''}" aria-label="准备资源准入"><div class="preparation-budget-heading"><div><span class="preparation-budget-eyebrow">PREPARE WITHIN RESOURCE BUDGET</span><h4>按资源预算推进准备。</h4></div><span class="preparation-budget-status">${esc(status)}</span></div><dl class="preparation-budget-cards">${cards.map(([key,label,value,note])=>`<div data-preparation-resource="${key}"><dt>${esc(label)}</dt><dd>${esc(value)}</dd><small>${esc(note)}</small></div>`).join('')}</dl>${reasons.length?`<ul class="preparation-budget-reasons">${reasons.map(reason=>`<li data-preparation-block="${esc(reason.code)}">${esc(reasonLabels[reason.code]||reason.label||reason.code)}</li>`).join('')}</ul>`:''}<p class="preparation-budget-note">${data.configured?'按管理员声明的每次准备开销，在领取任务之前预留预算；不足时任务仍留在待准备，释放后继续。':'尚未设置 CPU / 内存 / 磁盘估计预算；当前只按准备线程数与就绪水位限制并发。'} 这里显示调度预留，不是实测利用率或操作系统硬限制。等待数指准备线程，不是候选数量。</p></section>`;
  };
})();
