/* Run with node tests/test_comparison_interaction.cjs; no browser dependencies. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../app/web/image-comparison.js'), 'utf8');
function harness(sharedStorage = new Map()) {
  const listeners = new Map(), frames = [], calls = [];
  let reads = 0, writes = 0, box = {left:20, width:200};
  const add = (name,fn,options) => { if (!listeners.has(name)) listeners.set(name,[]); listeners.get(name).push({fn,options}); };
  const attrs = new Map([['aria-valuenow','50']]), classes = new Set();
  const imageError = {hidden:true};
  const afterImage = {
    complete:true, naturalWidth:100,
    matches:s=>s==='.compare-after',
    closest:s=>s==='.image-compare'?element:null
  };
  const element = {
    isConnected:true, dataset:{comparisonRoot:'root.png',beforeUrl:'/images/before.png',afterUrl:'/images/after.png'},
    getBoundingClientRect() { reads++;return {...box}; },
    getAttribute(name) {return attrs.get(name);}, setAttribute(name,value) {attrs.set(name,value);},
    style:{setProperty(name,value){writes++;attrs.set(name,value);}},
    classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name),toggle(name,yes){yes?classes.add(name):classes.delete(name);}},
    matches:s=>s==='.image-compare' || (s==='.image-compare.compare-loading' && classes.has('compare-loading')),
    querySelector:s=>s==='.compare-after'?afterImage:s==='.compare-image-error'?imageError:null,
    closest:s=>s==='.image-compare'?element:null, contains:other=>other===element,
    setPointerCapture(){}, hasPointerCapture(){return false;}, releasePointerCapture(){}
  };
  const localStorage = {get length(){return sharedStorage.size;},key:index=>Array.from(sharedStorage.keys())[index] ?? null,getItem:key=>sharedStorage.has(key)?sharedStorage.get(key):null,setItem:(key,value)=>sharedStorage.set(key,String(value)),removeItem:key=>sharedStorage.delete(key)};
  const context = {console, Map,Set,WeakMap,Date,Number,String,Math,encodeURIComponent,localStorage,
    document:{addEventListener:add,querySelectorAll:s=>s==='.image-compare'?[element]:[]}, window:{addEventListener:add,openFullscreenImg:url=>calls.push(url)},
    requestAnimationFrame:fn=>{frames.push(fn);return frames.length;},
    ResizeObserver:class {observe(){} unobserve(){}}
  };
  vm.runInNewContext(code,context);
  function emit(type,params={}) {
    const event={type,target:element,relatedTarget:null,pointerType:'mouse',pointerId:1,isPrimary:true,button:0,clientX:120,clientY:50,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},stopImmediatePropagation(){this.stopped=true;},...params};
    for (const {fn} of listeners.get(type)||[]) fn(event);
    return event;
  }
  return {element,afterImage,attrs,classes,calls,listeners,emit,window:context.window,storage:sharedStorage,run(){while(frames.length)frames.shift()();},frames:()=>frames.length,reads:()=>reads,writes:()=>writes,setBox:value=>{box=value;}};
}
let count=0;
function test(name,fn) { fn();count++;console.log('PASS',name); }

test('240 movement samples share one frame and one geometry read',()=>{
 const h=harness();for(let i=0;i<240;i++)h.emit('pointermove',{clientX:20+200*i/239});
 assert.equal(h.frames(),1);assert.equal(h.reads(),0);assert.equal(h.writes(),0);h.run();
 assert.equal(h.reads(),1);assert.equal(h.writes(),1);assert.equal(h.attrs.get('aria-valuenow'),'100');
});
test('later frames reuse stable bounds',()=>{
 const h=harness();h.emit('pointermove');h.run();h.emit('pointermove',{clientX:180});h.run();assert.equal(h.reads(),1);assert.equal(h.attrs.get('aria-valuenow'),'80');
});
test('scroll invalidates bounds without repeated reads',()=>{
 const h=harness();h.emit('pointermove');h.run();h.setBox({left:40,width:400});h.emit('scroll');h.emit('pointermove',{clientX:240});h.run();assert.equal(h.reads(),2);assert.equal(h.attrs.get('aria-valuenow'),'50');
});
test('same value does not write again',()=>{
 const h=harness();h.emit('pointermove');h.run();h.emit('pointermove');h.run();assert.equal(h.writes(),1);
});
test('detached pending slider cannot retain a rendering loop',()=>{
 const h=harness();h.emit('pointermove');h.element.isConnected=false;h.run();assert.equal(h.writes(),0);assert.equal(h.frames(),0);
});
test('release uses final position and suppresses accidental detail click',()=>{
 const h=harness();h.emit('pointerdown');h.emit('pointermove',{clientX:180});h.emit('pointerup',{clientX:240});h.run();assert.equal(h.attrs.get('aria-valuenow'),'100');assert.equal(h.emit('click').prevented,true);assert.equal(h.classes.has('compare-dragging'),false);
});
test('keyboard cancels a queued pointer sample',()=>{
 const h=harness();h.emit('pointermove',{clientX:200});h.emit('keydown',{key:'Home'});h.run();assert.equal(h.attrs.get('aria-valuenow'),'0');
});
test('touch vertical scroll does not change split',()=>{
 const h=harness();h.emit('pointerdown',{pointerType:'touch'});h.emit('pointermove',{pointerType:'touch',clientX:121,clientY:100});h.emit('pointermove',{pointerType:'touch',clientX:190,clientY:110});h.emit('pointercancel',{pointerType:'touch'});h.run();assert.equal(h.writes(),0);
});
test('horizontal touch moves and releases correctly',()=>{
 const h=harness();h.emit('pointerdown',{pointerType:'touch'});h.emit('pointermove',{pointerType:'touch',clientX:40,clientY:51});h.emit('pointerup',{pointerType:'touch',clientX:30,clientY:51});h.run();assert.equal(h.attrs.get('aria-valuenow'),'0'); // Endpoint snap is intentional.
});
test('another touch cannot take over the active slider',()=>{
 const h=harness();h.emit('pointerdown',{pointerType:'touch'});h.emit('pointermove',{pointerType:'touch',pointerId:2,clientX:210});h.run();assert.equal(h.writes(),0);
});
test('view buttons use capture before modal click suppression',()=>{
 const h=harness();const button={dataset:{compareOpen:'after'},closest:()=>({querySelector:()=>h.element})};
 const target={closest:s=>s==='[data-compare-open]'?button:null};
 const handlers=h.listeners.get('click');const capture=handlers.filter(x=>x.options===true);
 assert.ok(capture.length>=2);for(const {fn} of capture)fn({target,preventDefault(){},stopPropagation(){}});assert.deepEqual(h.calls,['/images/after.png']);
});
test('detail images load eagerly, card images remain lazy',()=>{
 const h=harness();const entry={image_comparison:{root_filename:'root.png',before:{url:'/images/before.png'},after:{url:'/images/after.png'}}};
 assert.ok(h.window.renderImageComparison(entry,true).includes('loading="eager"'));
 assert.ok(h.window.renderImageComparison(entry,false).includes('loading="lazy"'));
 assert.ok(h.window.renderImageComparison(entry,false).includes('fetchpriority="high"'));
});
test('near left edge snaps to exact zero, not a one-pixel strip',()=>{
 const h=harness();h.emit('pointermove',{clientX:22});h.run();assert.equal(h.attrs.get('--compare-split'),'0%');
});
test('near right edge snaps to exact one hundred',()=>{
 const h=harness();h.emit('pointermove',{clientX:218});h.run();assert.equal(h.attrs.get('--compare-split'),'100%');
});
test('only an exact left endpoint persists across a rerender',()=>{
 const storage=new Map(),h=harness(storage);h.emit('pointermove',{clientX:20});h.run();assert.equal(storage.get('portrait_gallery:compare_split:root.png'),'0');
 const rerender=harness(storage);const html=rerender.window.renderImageComparison({image_comparison:{root_filename:'root.png',before:{url:'/images/before.png'},after:{url:'/images/after.png'}}});assert.match(html,/aria-valuenow="0"/);assert.match(html,/--compare-split:0%/);
});
test('a snapped-near-edge position clears persistence and rerenders at center',()=>{
 const storage=new Map([['portrait_gallery:compare_split:root.png','100']]),h=harness(storage);h.emit('pointermove',{clientX:218});h.run();assert.equal(h.attrs.get('--compare-split'),'100%');assert.equal(storage.has('portrait_gallery:compare_split:root.png'),false);
 const rerender=harness(storage);const html=rerender.window.renderImageComparison({image_comparison:{root_filename:'root.png',before:{url:'/images/before.png'},after:{url:'/images/after.png'}}});assert.match(html,/aria-valuenow="50"/);assert.match(html,/--compare-split:50%/);
});
test('keyboard endpoints persist while arrow positions do not',()=>{
 const storage=new Map(),h=harness(storage);h.emit('keydown',{key:'End'});assert.equal(storage.get('portrait_gallery:compare_split:root.png'),'100');h.emit('keydown',{key:'ArrowLeft'});assert.equal(storage.has('portrait_gallery:compare_split:root.png'),false);
});
test('edited image loading gate reveals only after the edited image loads',()=>{
 const h=harness();h.classes.add('compare-loading');h.emit('load',{target:h.afterImage});assert.equal(h.classes.has('compare-loading'),false);
});
test('fully original endpoint does not wait for the hidden edited layer',()=>{
 const storage=new Map([['portrait_gallery:compare_split:root.png','100']]),h=harness(storage);
 const html=h.window.renderImageComparison({image_comparison:{root_filename:'root.png',before:{url:'/images/before.png'},after:{url:'/images/after.png'}}});
 assert.doesNotMatch(html,/class="image-compare compare-loading/);
});
test('header shortcut toggles every rendered comparison without overwriting card persistence',()=>{
 const storage=new Map([['portrait_gallery:compare_split:root.png','100']]),h=harness(storage);
 assert.equal(h.window.toggleComparisonDisplayMode(),'after');
 assert.equal(h.attrs.get('--compare-split'),'0%');
 assert.equal(storage.get('portrait_gallery:compare_split:root.png'),'100');
 assert.equal(h.window.toggleComparisonDisplayMode(),'before');
 assert.equal(h.attrs.get('--compare-split'),'100%');
 assert.equal(storage.get('portrait_gallery:compare_split:root.png'),'100');
});
test('resetting all comparisons centers rendered sliders and clears endpoint persistence',()=>{
 const storage=new Map([['portrait_gallery:compare_split:root.png','100']]),h=harness(storage);
 assert.equal(h.window.toggleComparisonDisplayMode(),'after');
 assert.equal(h.attrs.get('--compare-split'),'0%');
 assert.equal(h.window.resetComparisonSplits(),1);
 assert.equal(h.window.getComparisonDisplayMode(),'');
 assert.equal(h.attrs.get('--compare-split'),'50%');
 assert.equal(h.attrs.get('aria-valuenow'),'50');
 assert.equal(storage.has('portrait_gallery:compare_split:root.png'),false);
 const rerender=harness(storage);
 const html=rerender.window.renderImageComparison({image_comparison:{root_filename:'root.png',before:{url:'/images/before.png'},after:{url:'/images/after.png'}}});
 assert.match(html,/aria-valuenow="50"/);
 assert.match(html,/--compare-split:50%/);
});
test('fast hover exit finishes at left edge without an extra move',()=>{
 const h=harness();h.emit('pointermove',{clientX:70});h.run();h.emit('pointerout',{clientX:5,relatedTarget:{}});h.run();assert.equal(h.attrs.get('--compare-split'),'0%');
});
test('fast hover exit finishes at right edge without an extra move',()=>{
 const h=harness();h.emit('pointermove',{clientX:150});h.emit('pointerout',{clientX:250,relatedTarget:{}});h.run();assert.equal(h.attrs.get('--compare-split'),'100%');
});
test('leaving vertically preserves the last pending position',()=>{
 const h=harness();h.emit('pointermove',{clientX:100});h.emit('pointerout',{clientX:120,clientY:-1,relatedTarget:{}});h.run();assert.equal(h.attrs.get('--compare-split'),'40%');
});
test('failed pointer capture still tracks a drag over the backdrop',()=>{
 const h=harness();h.element.setPointerCapture=()=>{throw Error('capture unavailable');};const outside={closest:()=>null};
 h.emit('pointerdown');h.emit('pointermove',{target:outside,clientX:-20,buttons:1});h.emit('pointerup',{target:outside,clientX:-30,buttons:0});h.run();assert.equal(h.attrs.get('--compare-split'),'0%');assert.equal(h.classes.has('compare-dragging'),false);
});
test('lost pointer capture does not abandon an active drag',()=>{
 const h=harness();const outside={closest:()=>null};h.emit('pointerdown');h.emit('lostpointercapture');h.emit('pointermove',{target:outside,clientX:260,buttons:1});h.emit('pointerup',{target:outside,clientX:260,buttons:0});h.run();assert.equal(h.attrs.get('--compare-split'),'100%');
});
test('pointerup at edge commits even when no move sample arrived',()=>{
 const h=harness();h.emit('pointerdown');h.emit('pointerup',{clientX:21});h.run();assert.equal(h.attrs.get('--compare-split'),'0%');assert.equal(h.emit('click').prevented,true);
});
test('mouse capture recovery handles a missed button-up',()=>{
 const h=harness();h.emit('pointerdown');h.emit('pointermove',{clientX:260,buttons:0,target:{closest:()=>null}});h.run();assert.equal(h.attrs.get('--compare-split'),'100%');assert.equal(h.classes.has('compare-dragging'),false);
});
test('window blur cannot leave a stale active drag',()=>{
 const h=harness();h.emit('pointerdown');h.emit('blur');h.emit('pointermove',{clientX:250,target:{closest:()=>null}});h.run();assert.equal(h.classes.has('compare-dragging'),false);
});
test('endpoints hide the divider itself so there is no residual white seam',()=>{
 const h=harness();h.emit('pointermove',{clientX:20});h.run();assert.equal(h.classes.has('compare-at-start'),true);h.emit('pointermove',{clientX:220});h.run();assert.equal(h.classes.has('compare-at-end'),true);h.emit('pointermove');h.run();assert.equal(h.classes.has('compare-at-end'),false);
});
console.log(`PASS ${count} interaction tests`);
