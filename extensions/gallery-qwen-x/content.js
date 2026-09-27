/* Runs in Chrome's isolated world. Does not read cookies or DMs. It only reads
 * the visible text of the X post attached to the image being edited. */
(()=>{
'use strict';
const controls=new Map();let scheduled=0,refreshTimer=0,viewStateReady=false;
const viewStates=new Map();
const imageSelector='article img, [role="dialog"] img';
const wand='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 20 13-13 3 3L7 23M14 10l3 3M5 2v5M2.5 4.5h5M19 1v4M17 3h4M21 16v5M18.5 18.5h5"/></svg>';
const send=message=>chrome.runtime.sendMessage(message).then(r=>{if(!r?.success)throw new Error(r?.error||'扩展未响应，请刷新 X。');return r;});
function viewStateKey(button){return String(button?._mediaKey||'').trim();}
function preferredOriginal(button,job){const key=viewStateKey(button),state=key?viewStates.get(key):null;return Boolean(state&&state.jobId===job?.localId&&state.originalVisible===true);}
function persistViewState(button,job,originalVisible){const key=viewStateKey(button);if(!key||!job?.localId)return;viewStates.set(key,{jobId:job.localId,originalVisible:Boolean(originalVisible),updatedAt:Date.now()});while(viewStates.size>200)viewStates.delete(viewStates.keys().next().value);send({type:'VIEW_STATE_SET',states:Object.fromEntries(viewStates)}).catch(()=>{});}
async function loadViewStates(){try{const stored=await send({type:'VIEW_STATE_GET'}),entries=stored?.states&&typeof stored.states==='object'?stored.states:{};Object.entries(entries).forEach(([key,state])=>{if(!state||!state.jobId)return;const current=viewStates.get(key);if(!current||Number(state.updatedAt||0)>=Number(current.updatedAt||0))viewStates.set(key,state);});}catch{}viewStateReady=true;scheduleScan();}
let toastTimer;function toast(text){let el=document.getElementById('gqx-toast');if(!el){el=document.createElement('div');el.id='gqx-toast';el.setAttribute('role','status');document.body.append(el);}el.textContent=text;el.classList.add('gqx-visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('gqx-visible'),6000);}
function hidePreview(){const o=document.getElementById('gqx-preview');if(!o)return;o.classList.remove('gqx-preview-visible');const image=o.querySelector('.gqx-preview-image');if(image)image.removeAttribute('src');}
function showPreview(src,alt='Qwen 改后'){if(!src)return;let o=document.getElementById('gqx-preview');if(!o){o=document.createElement('div');o.id='gqx-preview';o.setAttribute('role','dialog');o.setAttribute('aria-modal','true');o.setAttribute('aria-label','改后图放大查看');const i=document.createElement('img');i.className='gqx-preview-image';const c=document.createElement('button');c.type='button';c.className='gqx-preview-close';c.setAttribute('aria-label','关闭放大查看');c.title='关闭';c.textContent='×';o.append(i,c);document.body.append(o);const d=()=>hidePreview();c.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();d();});o.addEventListener('click',e=>{if(e.target===o)d();});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&o.classList.contains('gqx-preview-visible'))d();},true);}const i=o.querySelector('.gqx-preview-image');i.src=src;i.alt=alt;o.classList.add('gqx-preview-visible');o.querySelector('.gqx-preview-close')?.focus({preventScroll:true});}
function apply(button,job){const status=String(job?.status||'unknown');const busy=['submitting','queued','downloading','generating'].includes(status);button._job=job;button.disabled=busy;button.classList.toggle('gqx-busy',busy);const saved=Boolean(job?.result?.saved_to_gallery);const labels={queued:'排队中',downloading:'读取原图',generating:'改图中',done:saved?'已保存':'已替换',error:'改图失败',unknown:'核对任务',interrupted:'核对任务',submitting:'提交中'};const label=labels[status]||'Qwen';button.querySelector('span').textContent=label;if(busy){button.dataset.gqxStatus=label;button.setAttribute('aria-busy','true');button.setAttribute('aria-label','改图状态：'+label);}else{delete button.dataset.gqxStatus;button.setAttribute('aria-busy','false');button.setAttribute('aria-label','用 Qwen 修改这张图');}button.title=(job?.message||'用 Qwen 修改这张图')+(['done','error'].includes(status)?' · 已直接替换原图；Shift+点击按新提示词重做':'');}
function inlineHost(img){return img.closest('.gqx-photo')||photoHost(img);}
function restoreOriginal(img){const result=img._gqxResult,toolbar=img._gqxToolbar;hidePreview();if(result?.isConnected)result.remove();if(toolbar?.isConnected)toolbar.remove();img._gqxResult=null;img._gqxToolbar=null;img._gqxSave=null;img._gqxZoom=null;img.classList.remove('gqx-original-hidden');delete img.dataset.gqxInline;delete img.dataset.gqxInlineRetry;}
async function inlineResult(img,button,job){if(job.status!=='done'||img.dataset.gqxInlineLoading===job.localId)return;const existing=img._gqxResult;if(existing?.isConnected&&existing.dataset.gqxJob===job.localId){const save=img._gqxSave;if(save){save.disabled=Boolean(job?.result?.saved_to_gallery);save.textContent=save.disabled?'已保存':'保存到画廊';}return;}img.dataset.gqxInlineLoading=job.localId;try{const r=await send({type:'RESULT',localId:job.localId,kind:'image'});const host=inlineHost(img);if(!host||!img.isConnected)throw new Error('原图位置已离开页面');const result=document.createElement('img');result.className='gqx-replaced-result';result.alt='Qwen 改后';result.dataset.gqxJob=job.localId;result.src=r.dataUrl;for(const type of ['pointerdown','pointerup','mousedown','mouseup','click'])result.addEventListener(type,e=>{e.preventDefault();e.stopImmediatePropagation();});await result.decode();if(!result.naturalWidth)throw new Error('改后图无法显示');restoreOriginal(img);host.classList.add('gqx-photo');if(getComputedStyle(host).position==='static')host.dataset.gqxPositioned='true';img.classList.add('gqx-original-hidden');host.append(result);const toolbar=document.createElement('div');toolbar.className='gqx-direct-toolbar';toolbar.setAttribute('role','group');toolbar.setAttribute('aria-label','Qwen 改图操作');const save=document.createElement('button');save.type='button';save.className='gqx-inline-action gqx-inline-save';save.textContent=job?.result?.saved_to_gallery?'已保存':'保存到画廊';save.disabled=Boolean(job?.result?.saved_to_gallery);save.addEventListener('click',async e=>{e.preventDefault();e.stopPropagation();const current=controls.get(img)?._job||job;if(current?.result?.saved_to_gallery){save.disabled=true;save.textContent='已保存';return;}save.disabled=true;save.textContent='保存中…';try{const response=await send({type:'SAVE_RESULT',localId:current.localId});const updated=response.job||{...current,result:{...(current.result||{}),saved_to_gallery:true}};if(controls.get(img))controls.get(img)._job=updated;save.textContent='已保存';toast('改图已保存到画廊。');}catch(error){save.disabled=false;save.textContent='保存到画廊';toast(error.message);}});const zoom=document.createElement('button');zoom.type='button';zoom.className='gqx-inline-action gqx-inline-zoom';zoom.textContent='放大查看';zoom.onclick=e=>{e.preventDefault();e.stopPropagation();showPreview(result.src,result.alt);};toolbar.append(save,zoom);host.append(toolbar);img._gqxResult=result;img._gqxToolbar=toolbar;img._gqxSave=save;img.dataset.gqxInline=job.localId;delete img.dataset.gqxInlineLoading;}catch(e){delete img.dataset.gqxInlineLoading;}}
function sourceArticleFor(img){
  return img.closest('article')||img.closest('[role="dialog"]')?.querySelector('article')||null;
}
function sourceUrlFor(img){const article=sourceArticleFor(img);return article?.querySelector('a[href*="/status/"] time')?.closest('a')?.href||article?.querySelector('a[href*="/status/"]')?.href||img.closest('a[href*="/status/"]')?.href||location.href;}
function sourceTextFor(img){
  const article=sourceArticleFor(img);
  const text=article?.querySelector('[data-testid="tweetText"]')?.innerText||'';
  return String(text).replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim().slice(0,20000);
}
async function rerollImage(img,button,reroll){
  if(reroll.disabled||button.disabled||!['done','error','unknown','interrupted'].includes(button._job?.status))return;
  const previousJob=button._job;
  reroll.disabled=true;reroll.setAttribute('aria-busy','true');reroll.title='正在重新生成改图';
  apply(button,{...previousJob,status:'submitting',message:'正在重新提交改图'});
  try{
    const response=await send({type:'GENERATE',mediaUrl:button._media,sourceUrl:sourceUrlFor(img),sourceText:sourceTextFor(img)});
    apply(button,response.job);
    if(response.job.status==='done')inlineResult(img,button,response.job);
    toast('已提交重新改图，完成后会替换当前改图。');scheduleRefresh();
  }catch(error){apply(button,previousJob);reroll.disabled=false;reroll.removeAttribute('aria-busy');reroll.title='重新生成改图';toast(error.message);}
}
function ensureRerollControls(){for(const [img,button] of controls){const toolbar=img._gqxToolbar;if(!toolbar||!img._gqxResult)continue;const available=['done','error','unknown','interrupted'].includes(button._job?.status),busy=Boolean(img.dataset.gqxInlineLoading);if(img._gqxReroll?.isConnected){img._gqxReroll.disabled=button.disabled||!available||busy;continue;}const reroll=document.createElement('button');reroll.type='button';reroll.className='gqx-inline-action gqx-inline-reroll';reroll.setAttribute('aria-label','重新生成改图');reroll.title='重新生成改图';reroll.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8.1 8.1 0 0 0-14.8-4.5L3 9"/><path d="M3 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 14.8 4.5L21 15"/><path d="M21 20v-5h-5"/></svg>';reroll.disabled=button.disabled||!available||busy;for(const type of ['pointerdown','pointerup','mousedown','mouseup'])reroll.addEventListener(type,e=>{e.preventDefault();e.stopPropagation();});reroll.addEventListener('click',e=>{e.preventDefault();e.stopImmediatePropagation();rerollImage(img,button,reroll);});toolbar.insertBefore(reroll,toolbar.firstChild);img._gqxReroll=reroll;}}
function photoHost(img){
  let host=img.closest('[data-testid="tweetPhoto"]');
  if(!host){const link=img.closest('a[href*="/photo/"]');host=link?.parentElement||link||img.parentElement;}
  for(let candidate=host;candidate&&candidate!==document.body;candidate=candidate.parentElement){
    const rect=candidate.getBoundingClientRect(),style=getComputedStyle(candidate);
    if(rect.width>0&&rect.height>0&&style.display!=='none'&&style.visibility!=='hidden')return candidate;
  }
  return host;
}
function scan(){scheduled=0;let attached=false;for(const [img,button]of controls){if(!img.isConnected){button.remove();controls.delete(img);}}
  if(/^\/(?:messages|i\/chat)(?:\/|$)/.test(location.pathname)){for(const b of controls.values())b.remove();controls.clear();return;}
  for(const img of document.querySelectorAll(imageSelector)){
    const media=GalleryX.mediaUrlFromImage(img);
    if(controls.has(img)){const b=controls.get(img);if(media&&b._media!==media){const nextKey=GalleryX.mediaKey(media),sameMedia=Boolean(nextKey&&nextKey===b._mediaKey);b._media=media;b._mediaKey=nextKey;if(!sameMedia){restoreOriginal(img);b._job=null;b.disabled=false;b.querySelector('span').textContent='Qwen';}}if(media&&b._job?.status==='done')inlineResult(img,b,b._job);continue;}
    if(!media)continue;
    let host=photoHost(img);if(!host)continue;
    // X's public post view makes its media link inert. Put our independent
    // action next to that local wrapper; never remove X's inert attribute.
    let ancestor=img.parentElement;
    for(let depth=0;ancestor&&depth<3;depth++,ancestor=ancestor.parentElement){
      if(ancestor.hasAttribute('inert')&&ancestor.parentElement){host=ancestor.parentElement;break;}
    }
    if(!host.classList.contains('gqx-photo'))host.classList.add('gqx-photo');
    if(getComputedStyle(host).position==='static')host.dataset.gqxPositioned='true';
    const b=document.createElement('button');b.type='button';b.className='gqx-wand';b.setAttribute('aria-label','用 Qwen 修改这张图片');b.title='画廊魔法棒 · Qwen-Image-2.1 Q8';b.innerHTML=wand+'<span>Qwen</span>';b._media=media;b._mediaKey=GalleryX.mediaKey(media);
    for(const type of ['pointerdown','pointerup','mousedown','mouseup'])b.addEventListener(type,e=>{e.preventDefault();e.stopPropagation();});
    b.addEventListener('click',async e=>{e.preventDefault();e.stopImmediatePropagation();if(!e.isTrusted||b.disabled)return;
      if(b._job && !(e.shiftKey && ['done','error'].includes(b._job.status))){
        if(b._job.status==='done'){
          // The wand acts as a toggle after a result is ready: click once to
          // show the generated image, click again to reveal the original.
          if(img._gqxResult?.isConnected){
            restoreOriginal(img);
            toast('已恢复原图。');
          }else{
            inlineResult(img,b,b._job);
            toast('改图已直接替换原图。');
          }
        }else if(['submitting','queued','downloading','generating'].includes(b._job.status)){
          toast(b._job.message||'改图仍在处理中，请稍候；完成后会直接显示在原图位置。');
        }else{
          toast(b._job.message||'请稍后重试这张图片。');
        }
        return;
      }
      if(b._job && e.shiftKey && !confirm('按当前保存的提示词再生成一张？新任务将使用 WIND 显卡，旧结果保留。'))return;
      if(b._job?.status==='done')restoreOriginal(img);b._job=null;
      apply(b,{status:'submitting',message:'正在提交改图'});
      const article=sourceArticleFor(img);let source=article?.querySelector('a[href*="/status/"] time')?.closest('a')?.href||article?.querySelector('a[href*="/status/"]')?.href||img.closest('a[href*="/status/"]')?.href||location.href;
    try{const r=await send({type:'GENERATE',mediaUrl:b._media,sourceUrl:source,sourceText:sourceTextFor(img)});apply(b,r.job);toast('已交给 WIND Qwen，完成后会直接替换原图，可选择保存到画廊。');scheduleRefresh();}
      catch(err){apply(b,{status:'error',message:err.message});toast(err.message);}
    });host.append(b);controls.set(img,b);attached=true;
  }
  ensureRerollControls();
  if(attached)scheduleRefresh(0);
}
function scheduleScan(){if(!scheduled)scheduled=requestAnimationFrame(scan);}
const observer=new MutationObserver(records=>{if(records.some(r=>r.type==='attributes'||[...r.addedNodes,...r.removedNodes].some(n=>n.nodeType===1&&!n.id?.startsWith('gqx-')&&!n.classList?.contains('gqx-wand'))))scheduleScan();});
observer.observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src','srcset','data-src','data-srcset','data-original','data-lazy-src','data-image-url','data-url']});
document.addEventListener('load',event=>{if(event.target?.tagName==='IMG')scheduleScan();},true);
document.addEventListener('error',event=>{if(event.target?.tagName==='IMG')scheduleScan();},true);
async function refresh(){refreshTimer=0;if(document.hidden)return;try{const r=await send({type:'JOBS'});for(const [img,b]of controls){const key=b._mediaKey||GalleryX.mediaKey(b._media);const candidates=r.jobs.filter(j=>(j.mediaKey||GalleryX.mediaKey(j.mediaUrl||''))===key&&key);const job=candidates.sort((a,z)=>Number(z.updatedAt||z.createdAt||0)-Number(a.updatedAt||a.createdAt||0))[0]||r.jobs.find(j=>j.mediaUrl===b._media);if(job){if(job.id&&(['queued','downloading','generating'].includes(job.status)||job.status==='done'&&!job.result)){const status=await send({type:'STATUS',localId:job.localId});apply(b,status.job);if(status.job.status==='done')inlineResult(img,b,status.job);}else {apply(b,job);if(job.status==='done')inlineResult(img,b,job);}}}}catch{}if([...controls.values()].some(b=>b.disabled))scheduleRefresh();}
function scheduleRefresh(delay=3000){if(!refreshTimer&&!document.hidden)refreshTimer=setTimeout(refresh,delay);}
chrome.runtime.onMessage.addListener(message=>{if(message.type==='GALLERY_QWEN_UPDATE'){for(const [img,b] of controls){const key=b._mediaKey||GalleryX.mediaKey(b._media);const incomingKey=message.job.mediaKey||GalleryX.mediaKey(message.job.mediaUrl||'');if((key&&key===incomingKey)||b._media===message.job.mediaUrl){apply(b,message.job);if(message.job.status==='done')inlineResult(img,b,message.job);}}}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){scheduleScan();scheduleRefresh(0);}else{clearTimeout(refreshTimer);refreshTimer=0;}});
// Keep a user's explicit "show original" choice stable across mutation scans
// and background status refreshes until they click the wand again.
const gqxInlineResultOriginal=inlineResult;
inlineResult=async function(img,button,job){if(job?.status==='done'){if(!viewStateReady)return;if(preferredOriginal(button,job)){restoreOriginal(img);img.dataset.gqxOriginalVisible=job.localId;button._gqxOriginalVisibleFor=job.localId;apply(button,job);return;}delete img.dataset.gqxOriginalVisible;button._gqxOriginalVisibleFor='';}return gqxInlineResultOriginal(img,button,job);};
const gqxApplyOriginal=apply;
apply=function(button,job){gqxApplyOriginal(button,job);if(job?.status==='done'&&button._gqxOriginalVisibleFor===job.localId){button.disabled=false;button.querySelector('span').textContent='显示改图';button.title='当前显示原图；再次点击显示改图';}};
document.addEventListener('click',event=>{const button=event.target?.closest?.('.gqx-wand');if(!button||event.shiftKey||button.disabled)return;const pair=[...controls].find(([,candidate])=>candidate===button);if(!pair||pair[1]._job?.status!=='done')return;const [img]=pair;event.preventDefault();event.stopImmediatePropagation();if(img._gqxResult?.isConnected){restoreOriginal(img);img.dataset.gqxOriginalVisible=button._job.localId;button._gqxOriginalVisibleFor=button._job.localId;persistViewState(button,button._job,true);button.disabled=false;apply(button,button._job);toast('已恢复原图。');}else{delete img.dataset.gqxOriginalVisible;button._gqxOriginalVisibleFor='';persistViewState(button,button._job,false);apply(button,button._job);inlineResult(img,button,button._job);toast('已直接显示改图。');}},true);
const gqxEnsureRerollOriginal=ensureRerollControls;
ensureRerollControls=function(){gqxEnsureRerollOriginal();for(const [img]of controls){const save=img._gqxSave,zoom=img._gqxToolbar?.querySelector('.gqx-inline-zoom');if(save){save.setAttribute('aria-label','保存到画廊');save.title='保存到画廊';}if(zoom){zoom.setAttribute('aria-label','放大查看');zoom.title='放大查看';}}};
loadViewStates();
})();
