'use strict';
// No network or production files are modified by these UI state tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../app/web/index.html'),'utf8');
const start = html.indexOf('async function deleteImageVersion(');
const code = html.slice(start, html.indexOf('async function activateImageVersion(',start));
function harness(options={}) {
  const calls=[], notices=[], renders=[], confirms=[];
  const buttons = [{innerHTML:'delete',isConnected:true,disabled:false},{innerHTML:'activate',isConnected:true,disabled:false}];
  const history={hidden:false,setAttribute(){},focus(){}};
  const payload={success:true,version_count:0,deleted_count:1,unavailable_count:0,items:[]};
  const context={console,Number,String,Boolean,Object,Array,JSON,Error,encodeURIComponent,
    API:'',imageVersionSwitchBusy:false,currentModalImg:'original.png',imageVersionRequestToken:1,
    imageVersionOverlay:{classList:{contains:()=>true}},allEntries:[{image_filename:'original.png',favorite:true,image_comparison:{kept:true}},{image_filename:'other.png',version_count:4}],todayData:{photos:[]},
    document:{querySelectorAll:()=>buttons,getElementById:()=>history},requestAnimationFrame:fn=>fn(),
    confirmDeleteAction:async o=>{confirms.push(o);return options.confirm!==false;},
    fetchWithTimeout:async (url,request)=>{calls.push({url,request});if(options.fail)throw Error('image_busy');return {ok:true,payload};},
    readGalleryApiJson:async response=>response.payload,
    renderActivePhotoView(){},renderImageVersions:data=>renders.push(data),showToast:value=>notices.push(value)
  };
  vm.createContext(context);vm.runInContext(code,context);
  return {context,calls,notices,renders,confirms,buttons,history,payload,run:()=>context.deleteImageVersion({id:'a'.repeat(32)},buttons[0])};
}
(async()=>{
  let n=0;
  const test=async(name,fn)=>{await fn();n++;console.log('PASS',name);};
  await test('cancel never sends DELETE',async()=>{const h=harness({confirm:false});await h.run();assert.equal(h.calls.length,0);assert.equal(h.context.imageVersionSwitchBusy,false);assert.equal(h.buttons[0].disabled,false);});
  await test('confirmed deletion targets only the selected version',async()=>{const h=harness();await h.run();assert.equal(h.calls.length,1);assert.equal(h.calls[0].request.method,'DELETE');assert.equal(h.calls[0].url,'/api/images/original.png/versions/'+'a'.repeat(32));assert.equal(h.context.allEntries[0].favorite,true);assert.equal(h.context.allEntries[0].image_comparison.kept,true);assert.equal(h.context.allEntries[1].version_count,4);assert.equal(h.renders.length,1);assert.equal(h.history.hidden,true);});
  await test('confirmation explains current image protection',async()=>{const h=harness({confirm:false});await h.run();assert.ok(h.confirms[0].description.includes('当前图片'));assert.ok(h.confirms[0].description.includes('无法撤销'));});
  await test('changing dialog while confirming cancels deletion',async()=>{const h=harness();h.context.confirmDeleteAction=async()=>{h.context.currentModalImg='other.png';return true;};await h.run();assert.equal(h.calls.length,0);});
  await test('a busy activation prevents parallel deletion',async()=>{const h=harness();h.context.imageVersionSwitchBusy=true;await h.run();assert.equal(h.confirms.length,0);assert.equal(h.calls.length,0);});
  await test('backend failure preserves list and releases buttons',async()=>{const h=harness({fail:true});await h.run();assert.equal(h.renders.length,0);assert.equal(h.context.imageVersionSwitchBusy,false);assert.equal(h.buttons[0].innerHTML,'delete');assert.equal(h.buttons[1].disabled,false);assert.ok(h.notices[0].includes('image_busy'));});
  await test('closing history during request never overwrites another dialog',async()=>{const h=harness();h.context.fetchWithTimeout=async()=>{h.context.imageVersionRequestToken++;return {ok:true,payload:h.payload};};await h.run();assert.equal(h.renders.length,0);});
  await test('delete action is after activation and confirmation is required',async()=>{const section=html.slice(html.indexOf('function renderImageVersions('),start);assert.ok(section.indexOf('data-version-delete-index="${index}"')>section.indexOf('data-version-activate-index="${index}"'));assert.ok(section.includes('image-version-actions'));assert.ok(code.indexOf('if (!confirmed || !sameDialog()) return;')<code.indexOf("method: 'DELETE'"));});
  console.log(`PASS ${n} history deletion UI tests`);
})().catch(error=>{console.error(error);process.exitCode=1;});
