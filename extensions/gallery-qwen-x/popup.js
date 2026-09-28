'use strict';
const $=id=>document.getElementById(id);
async function send(message){const r=await chrome.runtime.sendMessage(message);if(!r?.success)throw new Error(r?.error||'扩展未响应');return r;}
async function action(button,fn){button.disabled=true;try{await fn();}catch(e){$('status').textContent=e.message;}finally{button.disabled=false;}}
async function jobs(){const r=await send({type:'JOBS'});$('jobs').replaceChildren();$('clearJobs').disabled=!r.jobs.length;if(!r.jobs.length){$('jobs').textContent='在 X 图片上点击魔法棒开始，或从下方上传图片。';return;}for(const job of r.jobs.slice(0,12)){const row=document.createElement('div');row.className='job';const text=document.createElement('span');text.textContent=(job.inputType==='upload'&&job.sourceName?job.sourceName+' · ':'')+(job.message||job.status);const date=document.createElement('small');date.textContent=new Date(job.createdAt).toLocaleString();text.append(date);const b=document.createElement('button');const localUpload=job.inputType==='upload';b.textContent=localUpload?'查看结果':(job.status==='done'?'回到 X':'进度');b.onclick=()=>send({type:localUpload?'OPEN_RESULT':'OPEN_SOURCE',localId:job.localId}).catch(e=>$('status').textContent=e.message);const remove=document.createElement('button');remove.textContent='删除';remove.onclick=()=>action(remove,async()=>{if(!confirm('删除这条最近任务记录？已保存到画廊的图片不会删除。'))return;await send({type:'DELETE_JOB',localId:job.localId});await jobs();});row.append(text,b,remove);$('jobs').append(row);}}
function base64FromBuffer(buffer){const bytes=new Uint8Array(buffer);let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(binary);}
async function batchGenerate(){
 const input=$('uploadImages'),files=Array.from(input.files||[]);
 if(!files.length)throw new Error('请先选择图片。');
 if(files.length>12)throw new Error('一次最多选择 12 张图片。');
 const allowed=new Set(['image/jpeg','image/jpg','image/png','image/webp']);
 let accepted=0,failed=0;const failures=[];
 for(let i=0;i<files.length;i++){
  const file=files[i];$('batchStatus').textContent=`正在提交 ${i+1}/${files.length}…`;
  try{
   if(!allowed.has((file.type||'').toLowerCase()))throw new Error('格式不支持');
   if(file.size>10*1024*1024)throw new Error('超过 10 MiB');
   // Chrome runtime messages use JSON serialization; send a bounded base64
   // payload rather than an ArrayBuffer that older Chrome versions stringify.
   const data=base64FromBuffer(await file.arrayBuffer());
   await send({type:'BATCH_GENERATE',file:{name:file.name,type:file.type,size:file.size,data}});accepted++;
  }catch(e){failed++;failures.push(`${file.name}：${e.message}`);}
 }
 input.value='';await jobs();const detail=failures.slice(0,3).join('；');$('batchStatus').textContent=failed?`已提交 ${accepted} 张，失败 ${failed} 张。${detail}${failures.length>3?'…':''}`:`已提交 ${accepted} 张，结果可在最近任务查看。`;
}
$('openGallery').onclick=()=>send({type:'OPEN_GALLERY'}).catch(e=>$('status').textContent=e.message);
$('refresh').onclick=()=>action($('refresh'),jobs);
$('clearJobs').onclick=()=>action($('clearJobs'),async()=>{if(!confirm('清空最近任务？已保存到画廊的图片不会删除。'))return;await send({type:'CLEAR_JOBS'});await jobs();});
$('batchGenerate').onclick=()=>action($('batchGenerate'),async()=>{try{await batchGenerate();}catch(e){$('batchStatus').textContent=e.message;throw e;}});
jobs().catch(e=>$('status').textContent=e.message);
