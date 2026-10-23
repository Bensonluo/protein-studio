'use strict';

// Keep one result's preview and one exact window in memory. Original matrices
// remain downloadable artifacts; opening a target record never fetches them.
(()=>{
  const limit=256,tileLimit=128;
  let active=null,cache=null,generation=0;
  const text=value=>escapeHTML(value);
  const positive=value=>Number.isInteger(value)&&value>0;
  const finite=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
  function localUrl(value){
    if(typeof value!=='string'||!value)return null;
    try{const url=new URL(value,location.href);return url.origin===location.origin&&url.pathname.startsWith('/api/')?url:null;}catch{return null;}
  }
  function spec(result={}){
    result=result||{};
    const d=result.paeArtifact;
    if(d){return {descriptor:d,rows:d.rows,cols:d.cols,axisKind:d.axisKind||'unknown',available:d.status==='available'&&positive(d.rows)&&positive(d.cols),key:`${d.sha256||''}|${d.previewUrl||''}|${d.rows}|${d.cols}`,max:finite(result.maxPae)&&result.maxPae>0?result.maxPae:null};}
    const matrix=Array.isArray(result.pae)&&result.pae.length?result.pae:null;
    return {inline:matrix,rows:matrix?.length||0,cols:matrix?.[0]?.length||0,axisKind:'unknown',available:!!matrix&&positive(matrix[0]?.length),key:matrix,max:finite(result.maxPae)&&result.maxPae>0?result.maxPae:null};
  }
  function axis(s){return s.axisKind==='token'?'模型 token':s.axisKind==='residue'?'残基索引':'模型索引';}
  function downloads(s){const d=s.descriptor||{},url=localUrl(d.downloadUrl),binary=localUrl(d.binaryDownloadUrl);return `${url?`<a class="text-button pae-download" href="${text(url.pathname+url.search)}" download>下载矩阵来源 JSON ↗</a>`:''}${binary?`<a class="text-button pae-download" href="${text(binary.pathname+binary.search)}" download>下载矩阵制品 ↗</a>`:''}`;}
  function dimension(s){return positive(s.rows)&&positive(s.cols)?`${s.rows.toLocaleString('zh-CN')} × ${s.cols.toLocaleString('zh-CN')}`:'尺寸未提供';}
  function renderInspector(result){
    const s=spec(result);if(!s.descriptor&&!s.inline)return '';
    return `<section class="campaign-inspect-section pae-inspector-summary"><h3>PAE 矩阵制品</h3><p><strong>${dimension(s)}</strong> · ${axis(s)} · ${s.available?'可按需查看':'当前不可预览'}</p><p class="campaign-inspect-hint">此详情仅读取矩阵元数据。打开结构视图后，可加载采样预览或查看局部原始值。</p>${downloads(s)}${!s.available&&s.descriptor?.reason?`<p class="campaign-inspect-hint">${text(s.descriptor.reason)}</p>`:''}</section>`;
  }
  function renderAvailability(item){
    const info=item?.resultAvailability;if(info?.status!=='legacy-large-result')return '';
    const url=localUrl(info.downloadUrl||item.resultJsonUrl),size=finite(info.bytes)?`${(info.bytes/1024/1024).toFixed(1)} MiB`:'';
    return `<section class="panel pae-legacy-result"><span class="eyebrow">PUBLISHED RESULT</span><h3>结果已交付 · 旧版大型结果文件</h3><p>${text(info.message||'该结果采用旧版内联矩阵格式，详情不自动载入完整文件。')}</p>${size?`<p class="pae-legacy-size">原始结果文件 ${size}</p>`:''}${url?`<a class="button button-secondary" href="${text(url.pathname+url.search)}" download>下载原始结果 JSON ↗</a>`:''}<p>下载保留原始结果；此处没有重新运行预测。</p></section>`;
  }
  function render(result){
    const s=spec(result),intro=`<div class="panel-heading"><h3>${icon('grid')}相对位置误差</h3><span class="subtle">PAE · Å</span></div><div class="analysis-title-note">原始行列顺序 · 不作对称化</div>`;
    if(!s.available)return `<section class="panel analysis-panel pae-panel">${intro}<div class="empty-analysis">${icon('grid')}<strong>${s.descriptor?'PAE 暂不可预览':'暂无 PAE 数据'}</strong><p>${text(s.descriptor?.reason||'预测输出包含 PAE 后可显示；不从三维坐标推算。')}</p></div><div class="pae-downloads">${downloads(s)}</div></section>`;
    return `<section class="panel analysis-panel pae-panel" id="pae-panel">${intro}<div class="pae-metadata"><strong>${dimension(s)}</strong><span>${axis(s)} · 原始矩阵</span></div><div id="pae-idle" class="pae-idle"><span class="pae-idle-grid" aria-hidden="true">${icon('grid')}</span><p>按显示像素读取有界预览。<br>完整矩阵保留在结果文件中。</p><button class="button button-primary" type="button" id="pae-load-preview">${icon('play')}加载矩阵预览</button></div><p id="pae-error" class="pae-error" role="alert" hidden></p><div id="pae-plot" hidden><div class="pae-plot-mode"><strong id="pae-mode-label"></strong><button type="button" class="text-button" id="pae-overview" hidden>返回全图</button></div><div class="pae-wrap"><span class="pae-axis-y">行 · ${axis(s)}</span><canvas id="pae-canvas" class="pae-canvas" width="256" height="256" aria-label="PAE 原始行列顺序矩阵"></canvas><div class="pae-bottom">列 · ${axis(s)}</div><div class="pae-legend"><span>0 Å</span><span class="pae-gradient"></span><span id="pae-color-max"></span></div></div><div class="plot-tooltip" id="pae-tooltip">移动指针查看实际采样点</div><p class="pae-method-note" id="pae-method-note"></p><div class="pae-missing-key"><i></i>灰色：缺失值</div><form id="pae-window-form" class="pae-window-form"><div class="pae-window-heading">查看局部原始值 <span>坐标从 1 开始</span></div><label>起始行<input id="pae-row" type="number" min="1" max="${s.rows}" step="1" value="1" required></label><label>起始列<input id="pae-col" type="number" min="1" max="${s.cols}" step="1" value="1" required></label><label>窗口大小<select id="pae-window-size"><option value="32">32 × 32</option><option value="64" selected>64 × 64</option><option value="128">128 × 128</option></select></label><button type="submit" class="button button-secondary" id="pae-load-window">读取局部</button><div class="pae-window-nav" aria-label="原始矩阵窗口导航"><button type="button" data-pae-move="up" aria-label="上一行窗口">↑</button><button type="button" data-pae-move="left" aria-label="上一列窗口">←</button><button type="button" data-pae-move="right" aria-label="下一列窗口">→</button><button type="button" data-pae-move="down" aria-label="下一行窗口">↓</button></div></form></div><div class="pae-downloads">${downloads(s)}</div><p class="pae-source-note">PAE 是模型误差估计，预览不产生新的预测结果。</p></section>`;
  }
  function stop(){generation++;active?.controller?.abort();active=null;}
  function validData(data,s,exact){
    if(!data||data.method!==(exact?'exact':'nearest-center')||data.unit!=='angstrom')throw new Error('矩阵响应的方法或单位不匹配');
    const h=data.rowIndices?.length,w=data.colIndices?.length,max=exact?tileLimit:limit;
    if(!positive(h)||!positive(w)||h>max||w>max||!Array.isArray(data.values)||data.values.length!==h)throw new Error('矩阵响应超过窗口上限或尺寸无效');
    const checkIndices=(items,n)=>items.every((v,i)=>Number.isInteger(v)&&v>=0&&v<n&&(!i||v>items[i-1]));
    if(!checkIndices(data.rowIndices,s.rows)||!checkIndices(data.colIndices,s.cols))throw new Error('矩阵索引与原始尺寸不匹配');
    if(exact&&(!data.rowIndices.every((v,i)=>v===data.rowIndices[0]+i)||!data.colIndices.every((v,i)=>v===data.colIndices[0]+i)))throw new Error('原始窗口索引不连续');
    if(data.sourceShape&&(data.sourceShape[0]!==s.rows||data.sourceShape[1]!==s.cols))throw new Error('矩阵来源尺寸已变化，请刷新结果');
    if(data.shape&&(data.shape[0]!==h||data.shape[1]!==w))throw new Error('矩阵预览形状不匹配');
    if(!data.values.every(row=>Array.isArray(row)&&row.length===w&&row.every(v=>v===null||finite(v))))throw new Error('矩阵包含非法值或不完整的行');
    return data;
  }
  function inlineData(s,options){
    const exact=options.rowStart!=null,h=exact?options.rows:Math.min(s.rows,options.maxSize),w=exact?options.cols:Math.min(s.cols,options.maxSize);
    const rows=Array.from({length:h},(_,i)=>exact?options.rowStart+i:Math.floor((i+.5)*s.rows/h));
    const cols=Array.from({length:w},(_,i)=>exact?options.colStart+i:Math.floor((i+.5)*s.cols/w));
    return {sourceShape:[s.rows,s.cols],shape:[h,w],method:exact?'exact':'nearest-center',unit:'angstrom',rowIndices:rows,colIndices:cols,values:rows.map(r=>cols.map(c=>finite(s.inline[r]?.[c])?s.inline[r][c]:null))};
  }
  async function boundedJson(response){
    if(!response.ok){const info=await response.json().catch(()=>({}));throw new Error(info.error||`矩阵读取失败 (${response.status})`);}
    const reader=response.body.getReader(),chunks=[];let bytes=0;
    try{for(;;){const {done,value}=await reader.read();if(done)break;bytes+=value.byteLength;if(bytes>4*1024*1024)throw new Error('矩阵响应超过 4 MiB 上限');chunks.push(value);}}finally{await reader.cancel();}
    const all=new Uint8Array(bytes);let offset=0;for(const chunk of chunks){all.set(chunk,offset);offset+=chunk.length;}return JSON.parse(new TextDecoder().decode(all));
  }
  function showError(session,message){if(active!==session||!session.root.isConnected)return;const el=session.root.querySelector('#pae-error');el.textContent=message;el.hidden=!message;}
  function busy(session,value){session.root.setAttribute('aria-busy',String(value));for(const id of ['pae-load-preview','pae-load-window'])session.root.querySelector('#'+id).disabled=value;}
  async function load(session,options){
    session.controller?.abort();const controller=new AbortController();session.controller=controller;const request=++session.request;showError(session,'');busy(session,true);
    const exact=options.rowStart!=null;
    try{
      let data;
      if(session.spec.inline)data=inlineData(session.spec,options);
      else{const url=localUrl(exact?session.spec.descriptor.tileUrl:session.spec.descriptor.previewUrl);if(!url)throw new Error('此结果未提供可用的本地矩阵接口');url.search='';for(const [key,value]of Object.entries(options))url.searchParams.set(key,value);data=await boundedJson(await fetch(url.pathname+url.search,{signal:controller.signal}));}
      if(active!==session||request!==session.request||controller.signal.aborted)return;
      data=validData(data,session.spec,exact);session.data=data;
      if(exact)cache.tile=data;else{cache.preview=data;cache.tile=null;}
      paint(session,data);
    }catch(error){if(error.name!=='AbortError'&&active===session&&request===session.request)showError(session,error.message);}
    finally{if(active===session&&request===session.request)busy(session,false);}
  }
  function previewSize(root){return Math.max(1,Math.min(limit,Math.ceil((root.getBoundingClientRect().width-54)*Math.min(devicePixelRatio||1,2))));}
  function paint(session,data){
    const root=session.root,canvas=root.querySelector('#pae-canvas'),h=data.rowIndices.length,w=data.colIndices.length,ctx=canvas.getContext('2d');canvas.width=w;canvas.height=h;
    let max=session.spec.max||cache.colorMax;
    if(!max){max=0;for(const row of data.values)for(const v of row)if(finite(v))max=Math.max(max,v);max=max||1;cache.colorMax=max;}
    const img=ctx.createImageData(w,h);
    for(let y=0;y<h;y++)for(let x=0;x<w;x++){const v=data.values[y][x],k=(y*w+x)*4;if(v===null){img.data[k]=196;img.data[k+1]=201;img.data[k+2]=198;}else{const t=Math.max(0,Math.min(1,v/max));img.data[k]=Math.round(28+208*t);img.data[k+1]=Math.round(83+157*t);img.data[k+2]=Math.round(65+132*t);}img.data[k+3]=255;}ctx.putImageData(img,0,0);
    root.querySelector('#pae-idle').hidden=true;root.querySelector('#pae-plot').hidden=false;
    const exact=data.method==='exact';root.querySelector('#pae-mode-label').textContent=exact?'局部原始矩阵':`${h} × ${w} 采样预览`;
    root.querySelector('#pae-overview').hidden=!exact;
    root.querySelector('#pae-color-max').textContent=`${Number(max.toPrecision(4))} Å`;
    const range=`行 ${data.rowIndices[0]+1}–${data.rowIndices.at(-1)+1} / 列 ${data.colIndices[0]+1}–${data.colIndices.at(-1)+1}`;
    root.querySelector('#pae-method-note').textContent=(exact?`${range}。逐格保留原始值。`:'每像素取对应区间中心的原始点；不是区间平均，细小区域可能未被采样。点击图中位置可读取附近原始窗口。')+(session.spec.max?'':` 色标上限来自已加载预览（${Number(max.toPrecision(4))} Å），不代表完整矩阵最大值。`);
    root.querySelector('#pae-tooltip').textContent=exact?`${range} · 移动指针查看原始值`:'移动指针查看实际采样点';
    session.data=data;
  }
  function point(session,event){const rect=session.root.querySelector('#pae-canvas').getBoundingClientRect(),d=session.data;if(!d)return null;const y=Math.max(0,Math.min(d.rowIndices.length-1,Math.floor((event.clientY-rect.top)/rect.height*d.rowIndices.length))),x=Math.max(0,Math.min(d.colIndices.length-1,Math.floor((event.clientX-rect.left)/rect.width*d.colIndices.length)));return {row:d.rowIndices[y],col:d.colIndices[x],value:d.values[y][x]};}
  function windowOptions(session){const root=session.root,row=Number(root.querySelector('#pae-row').value)-1,col=Number(root.querySelector('#pae-col').value)-1,size=Number(root.querySelector('#pae-window-size').value);if(!Number.isInteger(row)||row<0||row>=session.spec.rows||!Number.isInteger(col)||col<0||col>=session.spec.cols||![32,64,128].includes(size))throw new Error('请输入原始矩阵范围内的整数坐标');return {rowStart:row,colStart:col,rows:Math.min(size,session.spec.rows-row),cols:Math.min(size,session.spec.cols-col)};}
  function mount(result){
    stop();const root=document.getElementById('pae-panel');if(!root)return;const s=spec(result);if(!s.available)return;
    if(!cache||cache.key!==s.key)cache={key:s.key,preview:null,tile:null,colorMax:null};
    const session={root,spec:s,request:0,controller:null,data:null,generation};active=session;
    root.querySelector('#pae-load-preview').addEventListener('click',()=>load(session,{maxSize:previewSize(root)}));
    root.querySelector('#pae-overview').addEventListener('click',()=>{session.controller?.abort();session.request++;busy(session,false);if(cache.preview)paint(session,cache.preview);else load(session,{maxSize:previewSize(root)});});
    root.querySelector('#pae-window-form').addEventListener('submit',event=>{event.preventDefault();try{load(session,windowOptions(session));}catch(error){showError(session,error.message);}});
    root.querySelectorAll('[data-pae-move]').forEach(button=>button.addEventListener('click',()=>{const size=Number(root.querySelector('#pae-window-size').value),vertical=['up','down'].includes(button.dataset.paeMove),input=root.querySelector(vertical?'#pae-row':'#pae-col'),sign=['up','left'].includes(button.dataset.paeMove)?-1:1;input.value=Math.max(1,Math.min(vertical?s.rows:s.cols,Number(input.value)+sign*size));try{load(session,windowOptions(session));}catch(error){showError(session,error.message);}}));
    const canvas=root.querySelector('#pae-canvas');canvas.addEventListener('mousemove',event=>{const p=point(session,event);if(!p)return;root.querySelector('#pae-tooltip').textContent=`${session.data.method==='exact'?'原始值':'原始采样点'} · 行 ${p.row+1} / 列 ${p.col+1} · ${p.value===null?'数据缺失':String(p.value)+' Å'}`;});
    canvas.addEventListener('click',event=>{const p=point(session,event);if(!p)return;const half=Math.floor(Number(root.querySelector('#pae-window-size').value)/2);root.querySelector('#pae-row').value=Math.max(1,p.row-half+1);root.querySelector('#pae-col').value=Math.max(1,p.col-half+1);load(session,windowOptions(session));});
    if(cache.preview)paint(session,cache.preview);else if(s.inline)load(session,{maxSize:previewSize(root)});
  }
  window.renderPAEPanel=render;window.mountPAEPanel=mount;window.stopPAEPanel=stop;window.renderPAEInspectorMetadata=renderInspector;window.renderResultAvailability=renderAvailability;
})();
