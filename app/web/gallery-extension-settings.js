/* Gallery settings for the local Chrome extension. The page never receives X credentials. */
(()=>{
  'use strict';
  let extensionId='',lastSynced='',syncTimer=0,recentPollTimer=0,autoEnabled=true;
  const MAX_BATCH=12,MAX_BYTES=10*1024*1024;
  const ALLOWED_TYPES=new Set(['image/jpeg','image/jpg','image/png','image/webp']);
  const P='/api/browser-extension';
  const el=id=>document.getElementById(id);
  const status=(text,state='neutral')=>{
    if(el('gxStatus'))el('gxStatus').textContent=text;
    const dot=el('gxStatusDot'),badge=el('gxStatusBadge');
    [dot,badge?.querySelector('.gx-status-dot')].filter(Boolean).forEach(node=>{
      node.classList.remove('gx-ok','gx-warn','gx-error');
      if(state!=='neutral')node.classList.add(`gx-${state}`);
    });
  };
  async function api(body){
    const r=await fetch(P+'/manage',{...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{}),cache:'no-store'});
    const d=await r.json();
    if(!r.ok)throw new Error(d.message||d.error||`HTTP ${r.status}`);
    return d;
  }
  function external(message){
    return new Promise((resolve,reject)=>{
      if(!extensionId||!window.chrome?.runtime?.sendMessage){reject(new Error('尚未安装扩展，请先点击安装。'));return;}
      try{
        chrome.runtime.sendMessage(extensionId,message,r=>{
          const err=chrome.runtime.lastError;
          if(err||!r?.success)reject(new Error(r?.error||'未检测到扩展。首次加载后请刷新画廊页面。'));
          else resolve(r);
        });
      }catch(e){reject(e);}
    });
  }
  function base64FromBuffer(buffer){
    const bytes=new Uint8Array(buffer);let binary='';
    for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
    return btoa(binary);
  }
  function updateBatchSelection(){
    const input=el('gxBatchImages'),files=Array.from(input?.files||[]),label=el('gxBatchSelection'),button=el('gxBatchSubmit');
    if(!label)return;
    if(!files.length){label.textContent='选择图片';if(button)button.disabled=true;return;}
    const valid=files.filter(file=>ALLOWED_TYPES.has((file.type||'').toLowerCase())&&file.size<=MAX_BYTES);
    label.textContent=valid.length===files.length?`已选择 ${files.length} 张图片`:`已选择 ${files.length} 张 · ${files.length-valid.length} 张不可用`;
    if(button)button.disabled=!valid.length||files.length>MAX_BATCH;
  }
  async function batchGalleryImages(){
    const input=el('gxBatchImages'),files=Array.from(input?.files||[]),batchStatus=el('gxBatchStatus'),button=el('gxBatchSubmit');
    if(!files.length)throw new Error('请先选择图片。');
    if(files.length>MAX_BATCH)throw new Error(`一次最多选择 ${MAX_BATCH} 张图片。`);
    button.disabled=true;
    let accepted=0;const failures=[];
    for(let i=0;i<files.length;i++){
      const file=files[i],type=(file.type||'').toLowerCase();
      if(batchStatus)batchStatus.textContent=`正在提交 ${i+1}/${files.length}：${file.name}`;
      try{
        if(!ALLOWED_TYPES.has(type))throw new Error('格式不支持');
        if(file.size>MAX_BYTES)throw new Error('超过 10 MiB');
        const data=base64FromBuffer(await file.arrayBuffer());
        await external({type:'BATCH_GENERATE',file:{name:file.name,type,size:file.size,data}});
        accepted++;
      }catch(error){failures.push(`${file.name}：${error.message}`);}
    }
    input.value='';updateBatchSelection();
    if(failures.length){
      const detail=failures.slice(0,3).join('；');
      if(batchStatus)batchStatus.textContent=`已提交 ${accepted} 张，失败 ${failures.length} 张。${detail}${failures.length>3?'…':''}`;
      status(`批量改图完成：成功 ${accepted} 张，失败 ${failures.length} 张。`,'warn');
    }else{
      if(batchStatus)batchStatus.textContent=`已提交 ${accepted} 张，结果可在最近任务查看。`;
      status(`批量改图已提交 ${accepted} 张。`,'ok');
    }
  }
  const recentStatus={submitting:'提交中',queued:'排队中',downloading:'读取原图',generating:'改图中',done:'已完成',error:'失败',unknown:'待核对',interrupted:'已中断'};
  function recentJobName(job){return job.inputType==='upload'?(job.sourceName||'上传图片'):'X 图片改图';}
  function recentJobDetail(job){const label=recentStatus[job.status]||job.status||'未知状态';const when=job.createdAt?new Date(job.createdAt).toLocaleString():'刚刚';return `${label} · ${when}`;}
  async function loadRecentTasks(){
    const box=el('gxRecentTasks');if(!box)return;
    box.textContent='正在加载最近任务…';
    try{
      const r=await external({type:'JOBS'}),jobs=(r.jobs||[]).slice().sort((a,b)=>Number(b.updatedAt||b.createdAt||0)-Number(a.updatedAt||a.createdAt||0)).slice(0,12);
      box.replaceChildren();
      if(!jobs.length){const empty=document.createElement('div');empty.className='gx-recent-empty';empty.textContent='暂无任务。提交批量图片或在 X 上点击魔法棒后会显示在这里。';box.append(empty);return;}
      jobs.forEach(job=>{
        const row=document.createElement('div');row.className='gx-recent-item';
        const meta=document.createElement('div');meta.className='gx-recent-meta';
        const name=document.createElement('span');name.className='gx-recent-name';name.textContent=recentJobName(job);
        const detail=document.createElement('span');detail.className='gx-recent-detail';detail.textContent=job.message||recentJobDetail(job);
        meta.append(name,detail);row.append(meta);
        if(job.status==='done'&&job.inputType==='upload'){
          const view=document.createElement('button');view.type='button';view.className='btn btn-secondary';view.textContent='查看改图';
          view.addEventListener('click',async()=>{view.disabled=true;view.textContent='加载中…';try{const result=await external({type:'RESULT',localId:job.localId,kind:'image'});const image=document.createElement('img');image.className='gx-recent-preview';image.alt='改后图';image.src=result.dataUrl;meta.append(image);view.remove();}catch(error){view.disabled=false;view.textContent='查看改图';detail.textContent=error.message;}});row.append(view);
        }else if(job.sourceUrl){
          const open=document.createElement('button');open.type='button';open.className='btn btn-secondary';open.textContent='打开原帖';open.addEventListener('click',()=>window.open(job.sourceUrl,'_blank','noopener,noreferrer'));row.append(open);
        }
        box.append(row);
      });
    }catch(error){box.textContent=`无法读取最近任务：${error.message}`;}
  }
  window.loadBrowserExtensionSettings=async()=>{
    try{
      const d=await api();
      extensionId=d.extension_id;
      el('gxPrompt').value=d.prompt||'';el('gxSteps').value=d.steps||25;el('gxSync').checked=d.sync_gallery_prompt!==false;
      autoEnabled=d.auto_connect_enabled!==false;
      el('gxClients').textContent=autoEnabled?`自动连接已启用 · ${d.clients.length} 个客户端`:'自动连接已停用';
      el('gxRevoke').textContent=autoEnabled?'停用扩展连接':'启用扩展连接';
      try{const r=await external({type:'PING'});status(r.connected?'扩展已安装并连接。':'扩展已安装，请点击连接扩展。',r.connected?'ok':'warn');}
      catch(e){status(e.message,'warn');}
    }catch(e){status(e.message,'error');}
  };
  window.saveBrowserExtensionSettings=async()=>{try{await api({operation:'save',prompt:el('gxPrompt').value,steps:Number(el('gxSteps').value),sync_gallery_prompt:el('gxSync').checked});status('已保存，X 魔法棒和批量改图都会使用此要求。','ok');}catch(e){status(e.message,'error');}};
  window.syncExtensionMagicPrompt=prompt=>{if(typeof prompt!=='string'||prompt===lastSynced)return;clearTimeout(syncTimer);syncTimer=setTimeout(()=>{api({operation:'sync_prompt',prompt}).then(()=>{lastSynced=prompt;}).catch(()=>{});},900);};
  window.installGalleryExtension=()=>{const guide=el('gxInstallGuide');guide.hidden=false;const a=document.createElement('a');a.href=P+'/download';a.download='gallery-qwen-x.zip';document.body.append(a);a.click();a.remove();status('安装包已请求下载。解压后，在 Chrome 扩展页选择“加载已解压的扩展程序”。','neutral');};
  window.connectGalleryExtension=async()=>{const b=el('gxConnect');b.disabled=true;try{await external({type:'PING'});await api({operation:'enable_auto_connect'});await external({type:'CONNECT',baseUrl:location.origin});await window.loadBrowserExtensionSettings();status('连接成功。保存修改要求后即可在 X 或此页批量改图。','ok');}catch(e){status(e.message,'error');}finally{b.disabled=false;}};
  document.addEventListener('DOMContentLoaded',()=>{
    el('gxInstall')?.addEventListener('click',window.installGalleryExtension);
    el('gxConnect')?.addEventListener('click',window.connectGalleryExtension);
    el('gxSave')?.addEventListener('click',window.saveBrowserExtensionSettings);
    el('gxBatchImages')?.addEventListener('change',updateBatchSelection);
    el('gxBatchSubmit')?.addEventListener('click',async()=>{try{await batchGalleryImages();}catch(e){if(el('gxBatchStatus'))el('gxBatchStatus').textContent=e.message;status(e.message,'error');updateBatchSelection();}});
    el('gxRecentToggle')?.addEventListener('click',()=>{const panel=el('gxRecentPanel'),button=el('gxRecentToggle');if(!panel||!button)return;const open=panel.hidden;panel.hidden=!open;button.setAttribute('aria-expanded',String(open));if(open){loadRecentTasks();clearInterval(recentPollTimer);recentPollTimer=setInterval(()=>{if(!panel.hidden)loadRecentTasks();},5000);}else{clearInterval(recentPollTimer);recentPollTimer=0;}});
    el('gxRecentRefresh')?.addEventListener('click',loadRecentTasks);
    el('gxCopyExtensions')?.addEventListener('click',()=>copy('chrome://extensions').then(()=>status('已复制。粘贴到 Chrome 地址栏打开。'),e=>status(e.message,'error')));
    el('gxImportPrompt')?.addEventListener('click',()=>{const state=typeof readCustomGenState==='function'?readCustomGenState():{};const current=document.getElementById('cgPrompt')?.value||state.prompt||'';el('gxPrompt').value=current;status(current?'已填入画廊魔法棒提示词，请保存。':'请先在“穿搭生成”填写修改要求。',current?'ok':'warn');});
    el('gxRevoke')?.addEventListener('click',async()=>{if(autoEnabled){const yes=typeof confirmDeleteAction==='function'?await confirmDeleteAction({title:'停用扩展连接？',description:'扩展将不能自动连接、提交或查询任务，画廊图片不会删除。',confirmLabel:'停用连接'}):confirm('停用扩展连接？');if(!yes)return;}try{await api({operation:autoEnabled?'revoke':'enable_auto_connect'});await window.loadBrowserExtensionSettings();status(autoEnabled?'已启用自动连接。':'已停用扩展连接。','ok');}catch(e){status(e.message,'error');}});
  });
  async function copy(text){try{await navigator.clipboard.writeText(text);}catch{const input=document.createElement('textarea');input.value=text;input.style.position='fixed';input.style.left='-9999px';document.body.append(input);input.select();const ok=document.execCommand('copy');input.remove();if(!ok)throw new Error('复制不可用，请手动复制。');}}
})();
