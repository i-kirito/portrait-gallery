/* Automatic local/LAN connection. No user-entered passwords, keys or codes. */
'use strict';
const GalleryConnection = (() => {
  const P='/api/browser-extension';
  const ready=chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
  let chain=Promise.resolve(), pending=null, validatedBase='', validatedAt=0;
  async function read(){await ready;return chrome.storage.local.get(['baseUrl','token','jobs','clientId','connectionMode','autoConnectDisabled','jobsByGallery']);}
  async function response(base,path,options={},token='',timeout=20000){
    const formBody=typeof FormData!=='undefined'&&options.body instanceof FormData;
    const requestHeaders={'X-Gallery-Extension-ID':chrome.runtime.id,...(token?{Authorization:'Bearer '+token}:{}),...(!formBody&&options.body?{'Content-Type':'application/json'}:{}),...options.headers};
    const r=await fetch(base+P+path,{...options,credentials:'omit',redirect:'error',signal:AbortSignal.timeout(timeout),
      headers:requestHeaders});
    if(!r.ok){const d=await r.json().catch(()=>({}));const e=new Error(d.message||d.error||(r.status===404?'画廊尚未支持自动连接，请更新画廊。':`HTTP ${r.status}`));e.httpStatus=r.status;throw e;}
    return r;
  }
  async function ensurePermission(base,explicit){
    const permission={origins:[GalleryX.permissionOrigin(base)]};
    if(await chrome.permissions.contains(permission))return;
    // Optional LAN host permissions are requested only from the explicit
    // “连接扩展” action. Automatic reconnect never prompts or sends first.
    if(!explicit||typeof chrome.permissions.request!=='function')throw new Error('请点击“连接扩展”并允许扩展访问此画廊地址。');
    let granted=false;
    try{granted=await chrome.permissions.request(permission);}catch{}
    if(!granted||!await chrome.permissions.contains(permission))throw new Error('未获得此画廊地址权限，连接未发送请求。');
  }
  async function connect(options={}){
    let cfg=await read();
    if(cfg.autoConnectDisabled&&!options.explicit)throw new Error('已断开自动连接，点击“自动检测”或“连接”重新启用。');
    const manual=options.baseUrl!=null;
    const target=manual?GalleryX.galleryOrigin(options.baseUrl):null;
    if(cfg.token&&!options.localOnly&&(!target||GalleryX.sameGalleryOrigin(target,cfg.baseUrl))){
      if(!options.explicit&&validatedBase===cfg.baseUrl&&Date.now()-validatedAt<30000)return cfg;
      try{
        if(options.explicit)await ensurePermission(GalleryX.galleryOrigin(cfg.baseUrl),true);
        await response(GalleryX.galleryOrigin(cfg.baseUrl),'/config',{},cfg.token,4000);
        validatedBase=cfg.baseUrl;validatedAt=Date.now();
        if(options.explicit){await chrome.storage.local.set({autoConnectDisabled:false,connectionMode:manual?'manual':cfg.connectionMode||'auto'});}
        return await read();
      }catch(e){if(e.httpStatus&&e.httpStatus!==401)throw e;}
    }
    const candidates=options.localOnly?GalleryX.localCandidates:target?[target]
      :cfg.connectionMode==='manual'&&cfg.baseUrl?[cfg.baseUrl]
      :[...(cfg.baseUrl?[cfg.baseUrl]:[]),...GalleryX.localCandidates];
    const clientId=cfg.clientId||crypto.randomUUID();
    if(!cfg.clientId)await chrome.storage.local.set({clientId});
    let lastError=null;
    for(const value of [...new Set(candidates)]){
      const base=GalleryX.galleryOrigin(value);
      try{
        await ensurePermission(base,options.explicit===true);
        const d=await (await response(base,'/connect',{method:'POST',body:JSON.stringify({client_id:clientId})},'',manual?8000:3000)).json();
        if(!d.success||d.extension_id!==chrome.runtime.id||!/^gxe_[\w-]+$/.test(d.token||''))throw new Error('此地址不是兼容的本地画廊。');
        cfg=await read();
        if(cfg.autoConnectDisabled&&!options.explicit)throw new Error('自动连接已取消。');
        const saved={baseUrl:base,token:d.token,connectionMode:manual?'manual':options.localOnly?'auto':cfg.connectionMode||'auto',autoConnectDisabled:false};
        if(cfg.baseUrl&&!GalleryX.sameGalleryOrigin(cfg.baseUrl,base)){
          const archived={...(cfg.jobsByGallery||{}),[cfg.baseUrl]:cfg.jobs||[]};
          saved.jobs=archived[base]||[];saved.jobsByGallery=archived;
        }
        await chrome.storage.local.set(saved);validatedBase=base;validatedAt=Date.now();return await read();
      }catch(e){lastError=e;if(manual||cfg.connectionMode==='manual'&&!options.localOnly||[400,401,403,429].includes(e.httpStatus))break;}
    }
    const detail=lastError?.message||'连接失败';
    throw new Error((manual||cfg.connectionMode==='manual'&&!options.localOnly?'无法连接填写的画廊地址：':'未检测到本机画廊，请启动画廊，或手动填写画廊所在设备的 IP。')+(manual?' '+detail:' '+detail));
  }
  function ensure(options={}){
    if(pending&&!options.explicit)return pending;
    const attempt=chain.then(()=>connect(options));chain=attempt.catch(()=>{});pending=attempt;
    attempt.finally(()=>{if(pending===attempt)pending=null;}).catch(()=>{});return attempt;
  }
  async function request(path,options={}){
    if(!/^\/(?:config|health|uploads|jobs(?:\/[a-f0-9]{32}(?:\/(?:image|source|save))?)?)$/.test(path))throw new Error('扩展接口不允许。');
    const cfg=await ensure();
    try{return await response(GalleryX.galleryOrigin(cfg.baseUrl),path,options,cfg.token);}
    catch(e){
      // A rejected write/generation is never automatically resubmitted.
      if(e.httpStatus!==401||(options.method||'GET')!=='GET')throw e;
      validatedAt=0;await chrome.storage.local.remove('token');const fresh=await ensure();
      return response(GalleryX.galleryOrigin(fresh.baseUrl),path,options,fresh.token);
    }
  }
  async function disconnect(){validatedAt=0;await ready;await chrome.storage.local.remove('token');await chrome.storage.local.set({autoConnectDisabled:true});}
  return Object.freeze({read,ensure,request,disconnect});
})();
