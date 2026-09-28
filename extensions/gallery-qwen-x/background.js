/* Gallery Qwen X. Automatic scoped connection, never X cookies. */
'use strict';
importScripts('shared.js','connection.js');
const trustedReady = chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
let submitChain = Promise.resolve(), storageChain = Promise.resolve();
const deletedLocalIds=new Set();
const apiOrigin = value => GalleryX.galleryOrigin(value);
const terminalStatuses = new Set(['done','error','interrupted']);
function recordWithMediaKey(record){
  const next={...record};
  // Older task records may predate localId. Reuse their stable server/request
  // identifier so they remain viewable, deletable, and cacheable after an
  // extension upgrade.
  if(!next.localId){
    const legacyId=next.request_id||next.requestId||next.id;
    if(typeof legacyId==='string'&&legacyId.trim())next.localId=legacyId.trim();
  }
  if(!next.mediaKey&&next.mediaUrl)next.mediaKey=GalleryX.mediaKey(next.mediaUrl);
  return next;
}
function jobIdentity(job){
  return String(job?.localId||job?.request_id||job?.requestId||job?.id||'').trim();
}
function jobMediaKey(job){
  return job?.mediaKey||GalleryX.mediaKey(job?.mediaUrl||'');
}
function sourceTextValue(value){
  return String(value||'').replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim().slice(0,20000);
}
function newestFirst(a,b){return Number(b.updatedAt||b.createdAt||0)-Number(a.updatedAt||a.createdAt||0);}
async function settings() { return GalleryConnection.read(); }
async function api(path, options={}) { return GalleryConnection.request(path,options); }
async function jsonApi(path,body) {return (await api(path,body?{method:'POST',body:JSON.stringify(body)}:{})).json();}
async function connectResult(options={}) {
  const cfg=await GalleryConnection.ensure(options);
  return {success:true,connected:true,baseUrl:cfg.baseUrl,connectionMode:cfg.connectionMode};
}
async function writeJob(record) {
  if(record?.localId&&deletedLocalIds.has(record.localId))return;
  const cfg=await settings(),jobs=Array.isArray(cfg.jobs)?cfg.jobs:[];
  const next=recordWithMediaKey({...record,updatedAt:Date.now()});
  const i=jobs.findIndex(j=>j.localId===next.localId);
  if(i>=0) jobs[i]=recordWithMediaKey({...jobs[i],...next}); else jobs.unshift(next);
  // Job records are deliberately metadata-only. Keep completed records as a
  // durable index so a refresh or a later X re-render can find the server
  // result again; image bytes remain on the gallery and are fetched on demand.
  await chrome.storage.local.set({jobs});
}
function saveJob(record) {
  return queueStorage(()=>writeJob(record));
}
async function submit(message,sender) {
  const mediaUrl=GalleryX.mediaUrl(message.mediaUrl),sourceUrl=GalleryX.postUrl(message.sourceUrl||''),sourceText=sourceTextValue(message.sourceText);
  const mediaKey=GalleryX.mediaKey(mediaUrl);
  const cfg=await GalleryConnection.ensure();
  const prior=(cfg.jobs||[]).filter(j=>jobMediaKey(j)===mediaKey&&['submitting','queued','downloading','generating','unknown'].includes(j.status)).sort(newestFirst)[0];
  if(prior) return {success:true,job:prior};
  const config=await jsonApi('/config');
  if(!config.prompt?.trim()) throw new Error('先在画廊“Chrome 扩展”设置中填写修改提示词。');
  const record={localId:crypto.randomUUID(),mediaUrl,mediaKey,sourceUrl,sourceText,tabId:sender.tab.id,status:'submitting',createdAt:Date.now()};
  // Persist before sending: a lost response never automatically triggers another edit.
  await saveJob(record);
  try {
    const job=await jsonApi('/jobs',{media_url:mediaUrl,source_url:sourceUrl,source_text:sourceText,request_id:record.localId});
    Object.assign(record,{id:job.id,status:job.status,message:job.message});
    await saveJob(record);
    await chrome.alarms.create('gallery-qwen-jobs',{periodInMinutes:0.5});
    return {success:true,job:record};
  } catch(e) {
    const rejected=e.httpStatus>=400&&e.httpStatus<500;
    record.status=rejected?'error':'unknown';record.message=(rejected?'请求未提交：':'提交未确认，请先核对画廊，勿重复提交。')+e.message;
    await saveJob(record);
    throw new Error(record.message);
  }
}
function uploadName(value) {
  const name=String(value||'upload').replace(/[\x00-\x1f\x7f]/g,'').split(/[\\/]/).pop().trim();
  return (name||'upload').slice(0,120);
}
async function submitUpload(message,sender) {
  const file=message?.file||{};
  const type=String(file.type||'').toLowerCase();
  const allowed=new Set(['image/jpeg','image/jpg','image/png','image/webp']);
  if(!allowed.has(type))throw new Error('仅支持 JPEG、PNG 或 WEBP 图片。');
  const data=file.data;
  let bytes=null;
  if(typeof data==='string'){
    if(data.length>Math.ceil(10*1024*1024*4/3)+8)throw new Error('每张图片不能超过 10 MiB。');
    try{const binary=atob(data),decoded=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)decoded[i]=binary.charCodeAt(i);bytes=decoded.buffer;}catch{throw new Error('图片数据无效。');}
  }else if(data instanceof ArrayBuffer)bytes=data;
  else if(ArrayBuffer.isView(data))bytes=data.buffer.slice(data.byteOffset,data.byteOffset+data.byteLength);
  else if(Array.isArray(data))bytes=new Uint8Array(data).buffer;
  const size=Number(file.size||bytes?.byteLength||0);
  if(!bytes||!size||size>10*1024*1024||bytes.byteLength>10*1024*1024||bytes.byteLength!==size)throw new Error('每张图片不能超过 10 MiB。');
  const config=await jsonApi('/config');
  if(!config.prompt?.trim())throw new Error('先在画廊“Chrome 扩展”设置中填写修改提示词。');
  const record={localId:crypto.randomUUID(),inputType:'upload',sourceName:uploadName(file.name),sourceUrl:'',tabId:sender.tab?.id??null,status:'submitting',createdAt:Date.now()};
  // Persist before sending: a lost response must not cause a duplicate edit.
  await saveJob(record);
  try {
    const form=new FormData();
    form.append('image',new Blob([bytes],{type}),record.sourceName);
    form.append('request_id',record.localId);
    form.append('filename',record.sourceName);
    const response=await api('/uploads',{method:'POST',body:form});
    const job=await response.json();
    Object.assign(record,{id:job.id,status:job.status,message:job.message,inputType:job.input_type||'upload',sourceName:job.source_name||record.sourceName});
    await saveJob(record);
    await chrome.alarms.create('gallery-qwen-jobs',{periodInMinutes:0.5});
    return {success:true,job:record};
  } catch(e) {
    const rejected=e.httpStatus>=400&&e.httpStatus<500;
    record.status=rejected?'error':'unknown';record.message=(rejected?'请求未提交：':'提交未确认，请先核对画廊，勿重复提交。')+e.message;
    await saveJob(record);
    throw new Error(record.message);
  }
}
async function refreshJob(localId,force=false) {
  const cfg=await settings(),record=(cfg.jobs||[]).find(j=>j.localId===localId);
  if(!record)throw new Error('任务记录不存在。');
  // Older records may have reached done before result metadata was persisted;
  // fetch them once so the durable cache is self-healing after an upgrade.
  if(!record.id||(
    terminalStatuses.has(record.status)&&
    (record.status!=='done'||record.result&&!force)
  ))return record;
  try {
    const job=await jsonApi('/jobs/'+record.id);
    const fresh={...record,sourceText:job.source_text||record.sourceText||'',status:job.status,message:job.message,result:job.result};
    await saveJob(fresh);return fresh;
  } catch(e) {return {...record,connectionError:e.message};}
}
async function jobsSnapshot() {
  const cfg=await settings(),rawJobs=Array.isArray(cfg.jobs)?cfg.jobs:[];
  const jobs=rawJobs.map(recordWithMediaKey);
  if(jobs.some((job,index)=>job.localId!==rawJobs[index]?.localId))await replaceStoredJobs(jobs,cfg);
  await Promise.all(jobs
    // Refresh every non-terminal record, including statuses written by older
    // extension builds, so a completed upload cannot remain stuck forever.
    .filter(job=>job?.id&&!terminalStatuses.has(job.status))
    .map(job=>refreshJob(job.localId).catch(()=>job)));
  const fresh=await settings();
  return (fresh.jobs||[]).map(({tabId,...job})=>recordWithMediaKey(job));
}
function validLocalId(value){return typeof value==='string'&&value.length>0&&value.length<=120;}
// Recent-task deletion is intentionally local-only: server jobs may still finish,
// while saved gallery images must never be removed by clearing extension history.
function queueStorage(work){const run=storageChain.then(work);storageChain=run.catch(()=>{});return run;}
async function replaceStoredJobs(next,cfg){
  const saved={jobs:next};
  if(cfg?.baseUrl&&cfg.jobsByGallery&&typeof cfg.jobsByGallery==='object'&&!Array.isArray(cfg.jobsByGallery))
    saved.jobsByGallery={...cfg.jobsByGallery,[cfg.baseUrl]:next};
  await chrome.storage.local.set(saved);
}
async function deleteStoredJob(localId){
  if(!validLocalId(localId))throw new Error('任务编号无效。');
  const key=String(localId).trim();
  deletedLocalIds.add(key);
  return queueStorage(async()=>{
    const cfg=await settings(),jobs=Array.isArray(cfg.jobs)?cfg.jobs:[];
    const removed=jobs.filter(job=>jobIdentity(job)===key);
    const remaining=jobs.filter(job=>jobIdentity(job)!==key);
    if(!removed.length){
      // Deletion is idempotent: a polling refresh may have removed/migrated
      // the row between rendering and confirmation. Treat that stale click as
      // success instead of surfacing a misleading “record not found” error.
      return {success:true,localId:key,remaining:jobs.length,alreadyDeleted:true};
    }
    removed.forEach(job=>{const id=jobIdentity(job);if(id)deletedLocalIds.add(id);});
    await replaceStoredJobs(remaining,cfg);
    return {success:true,localId:key,remaining:remaining.length};
  });
}
async function clearStoredJobs(){
  return queueStorage(async()=>{
    const cfg=await settings(),jobs=Array.isArray(cfg.jobs)?cfg.jobs:[];
    jobs.forEach(job=>{const id=jobIdentity(job);if(id)deletedLocalIds.add(id);});
    await replaceStoredJobs([],cfg);
    return {success:true,cleared:jobs.length};
  });
}
async function imageData(localId,kind) {
  if(!['image','source'].includes(kind))throw new Error('invalid image kind');
  const cfg=await settings(),job=(cfg.jobs||[]).find(j=>j.localId===localId&&j.status==='done');
  if(!job) throw new Error('图片尚未完成。');
  const response=await api('/jobs/'+job.id+'/'+kind),bytes=new Uint8Array(await response.arrayBuffer());
  if(bytes.length>32*1024*1024)throw new Error('图片过大，请从画廊查看。');
  let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
  return 'data:image/png;base64,'+btoa(binary);
}
async function saveResult(localId) {
  const cfg=await settings(),record=(cfg.jobs||[]).find(j=>j.localId===localId);
  if(!record?.id||!/^[a-f0-9]{32}$/.test(record.id))throw new Error('任务尚未提交完成。');
  const saved=await api('/jobs/'+record.id+'/save',{method:'POST'});
  const job=await saved.json();
  const refreshed=await refreshJob(localId,true),serverJob=job.job||job;
  const updated=recordWithMediaKey({...record,...refreshed,...serverJob,result:{...(record.result||{}),...(serverJob.result||{}),saved_to_gallery:true}});
  await saveJob(updated);
  return {success:true,job:updated};
}
function sanitizeViewStates(states){
  if(!states||typeof states!=='object'||Array.isArray(states))return {};
  return Object.fromEntries(Object.entries(states).slice(-200).filter(([key,state])=>/^[\w-]{1,120}$/.test(key)&&state&&typeof state==='object'&&typeof state.jobId==='string'&&state.jobId.length<=120).map(([key,state])=>[key,{jobId:state.jobId,originalVisible:state.originalVisible===true,updatedAt:Number(state.updatedAt)||Date.now()}]));
}
async function getViewStates(){
  await trustedReady;
  const stored=await chrome.storage.local.get('gqxViewStates');
  return stored?.gqxViewStates&&typeof stored.gqxViewStates==='object'&&!Array.isArray(stored.gqxViewStates)?stored.gqxViewStates:{};
}
function ownPage(sender){return sender.id===chrome.runtime.id&&sender.url?.startsWith(chrome.runtime.getURL(''));}
function xPage(sender){try{return sender.id===chrome.runtime.id&&new URL(sender.url).protocol==='https:'&&GalleryX.X_HOSTS.includes(new URL(sender.url).hostname)&&sender.tab?.id!=null;}catch{return false;}}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  (async()=>{
    if(!message||typeof message!=='object')throw new Error('invalid message');
    const internal=ownPage(sender),fromX=xPage(sender);
    if(!internal&&!fromX)throw new Error('此页面不允许使用扩展。');
    if(message.type==='VIEW_STATE_GET'){
      if(!fromX)throw new Error('此页面不允许读取显示偏好。');
      return {success:true,states:await getViewStates()};
    }
    if(message.type==='VIEW_STATE_SET'){
      if(!fromX)throw new Error('此页面不允许保存显示偏好。');
      await trustedReady;
      await chrome.storage.local.set({gqxViewStates:sanitizeViewStates(message.states)});
      return {success:true};
    }
    switch(message.type){
      case 'GENERATE':
        if(!fromX)throw new Error('请选择 X 帖子图片。');
        const run=submitChain.then(()=>submit(message,sender));submitChain=run.catch(()=>{});return await run;
      case 'BATCH_GENERATE':
        if(!internal)throw new Error('批量上传只允许从扩展弹窗发起。');
        const uploadRun=submitChain.then(()=>submitUpload(message,sender));submitChain=uploadRun.catch(()=>{});return await uploadRun;
      case 'JOBS': {const cfg=await settings();return {success:true,connected:!!cfg.token,jobs:await jobsSnapshot()};}
      case 'DELETE_JOB':return await deleteStoredJob(message.localId);
      case 'CLEAR_JOBS':return await clearStoredJobs();
      case 'STATUS':return {success:true,job:await refreshJob(message.localId)};
      case 'RESULT':return {success:true,dataUrl:await imageData(message.localId,message.kind||'image')};
      case 'SAVE_RESULT':return await saveResult(message.localId);
      case 'OPEN_RESULT': {const cfg=await settings();if(!(cfg.jobs||[]).some(j=>j.localId===message.localId))throw new Error('任务不存在');await chrome.tabs.create({url:chrome.runtime.getURL('result.html')+'?job='+encodeURIComponent(message.localId)});return {success:true};}
      case 'OPEN_SOURCE': {const cfg=await settings(),record=(cfg.jobs||[]).find(j=>j.localId===message.localId),source=record?.sourceUrl||'';if(!/^https:\/\/(?:www\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]{1,50}\/status\/\d+$/.test(source))throw new Error('任务没有关联的 X 原帖。');await chrome.tabs.create({url:source});return {success:true};}
      case 'OPEN_GALLERY': {const cfg=await settings();await chrome.tabs.create({url:apiOrigin(cfg.baseUrl||'http://127.0.0.1:18889')});return {success:true};}
      default:throw new Error('unknown message');
    }
  })().then(respond,e=>respond({success:false,error:e.message}));return true;
});
chrome.runtime.onMessageExternal.addListener((message,sender,respond)=>{
  (async()=>{
    const base=apiOrigin(new URL(sender.url).origin);
    const cfg=await settings();
    if(message?.type==='PING')return {success:true,version:chrome.runtime.getManifest().version,connected:!!cfg.token};
    if(message?.type==='CONNECT'&&base===apiOrigin(message.baseUrl))return connectResult({baseUrl:base,explicit:true});
    if(cfg.baseUrl&&apiOrigin(cfg.baseUrl)!==base)throw new Error('当前画廊不是扩展已连接的地址。');
    if(message?.type==='JOBS')return {success:true,connected:!!cfg.token,jobs:await jobsSnapshot()};
    if(message?.type==='DELETE_JOB')return await deleteStoredJob(message.localId);
    if(message?.type==='CLEAR_JOBS')return await clearStoredJobs();
    if(message?.type==='RESULT')return {success:true,dataUrl:await imageData(message.localId,message.kind||'image')};
    if(message?.type==='SAVE_RESULT')return await saveResult(message.localId);
    if(message?.type==='BATCH_GENERATE'){
      const page=new URL(sender.url),pageBase=apiOrigin(page.origin);
      if(cfg.baseUrl&&apiOrigin(cfg.baseUrl)!==pageBase)throw new Error('批量上传只能从当前已连接的画廊发起。');
      const run=submitChain.then(()=>submitUpload(message,sender));submitChain=run.catch(()=>{});return await run;
    }
    throw new Error('不支持外部操作。');
  })().then(respond,e=>respond({success:false,error:e.message}));return true;
});
chrome.alarms.onAlarm.addListener(async alarm=>{
  if(alarm.name!=='gallery-qwen-jobs')return;
  const cfg=await settings(),active=(cfg.jobs||[]).filter(j=>j.id&&['queued','downloading','generating'].includes(j.status));
  for(const old of active){const job=await refreshJob(old.localId);if(job.tabId!=null)chrome.tabs.sendMessage(job.tabId,{type:'GALLERY_QWEN_UPDATE',job}).catch(()=>{});}
  if(!active.length)await chrome.alarms.clear('gallery-qwen-jobs');
});
chrome.runtime.onStartup.addListener(()=>{GalleryConnection.ensure().catch(()=>{});chrome.alarms.create('gallery-qwen-jobs',{periodInMinutes:0.5});});
