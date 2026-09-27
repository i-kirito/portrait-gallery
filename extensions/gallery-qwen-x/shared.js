'use strict';
const GalleryX=Object.freeze({
  X_HOSTS:['x.com','www.x.com','twitter.com','www.twitter.com'],
  galleryOrigin(value){
    let raw=String(value||'').trim()||'127.0.0.1:18889';
    if(!/^https?:\/\//i.test(raw))raw='http://'+raw;
    let u;try{u=new URL(raw);}catch{throw new Error('请输入有效的画廊 IP，例如 192.168.31.216。');}
    const parts=u.hostname.split('.').map(Number),ip=parts.length===4&&parts.every(n=>Number.isInteger(n)&&n>=0&&n<=255);
    const local=u.hostname==='localhost'||u.hostname==='[::1]'||(ip&&(parts[0]===127||parts[0]===10||(parts[0]===192&&parts[1]===168)||(parts[0]===172&&parts[1]>=16&&parts[1]<=31)));
    if(!['http:','https:'].includes(u.protocol)||!local||u.username||u.password||u.pathname!=='/'||u.search||u.hash)throw new Error('仅支持本机或内网 IP，不接受公网地址、路径或用户名密码。');
    if(!u.port)u.port='18889';
    return u.origin;
  },
  permissionOrigin(value){const u=new URL(this.galleryOrigin(value));return u.protocol+'//'+u.hostname+'/*';},
  localCandidates:['http://127.0.0.1:18889','http://localhost:18889'],
  mediaUrl(value){let u;try{u=new URL(value);}catch{throw new Error('找不到 X 原图。');}if(u.protocol!=='https:'||u.host!=='pbs.twimg.com'||u.username||u.password||u.hash||!/^\/media\/[\w-]+(?:\.(?:jpe?g|png|webp))?$/.test(u.pathname))throw new Error('仅支持 X 帖子静态图片。');const format=u.searchParams.get('format')||(u.pathname.includes('.')?u.pathname.split('.').pop():'jpg');if(!['jpg','jpeg','png','webp'].includes(format))throw new Error('图片格式不支持。');u.search='';u.searchParams.set('format',format);u.searchParams.set('name','orig');return u.href;},
  mediaKey(value){
    let u;try{u=new URL(value);}catch{return '';}
    if(u.protocol!=='https:'||u.host!=='pbs.twimg.com'||u.username||u.password||u.hash)return '';
    const match=u.pathname.match(/^\/media\/([\w-]+)(?:\.(?:jpe?g|png|webp))?$/i);
    return match?match[1]:'';
  },
  mediaCandidates(image){
    if(!image)return[];
    const values=[],seen=new Set();
    const add=value=>{const text=String(value||'').trim();if(text&&!seen.has(text)){seen.add(text);values.push(text.startsWith('//')?'https:'+text:text);}};
    add(image.currentSrc);add(image.src);
    for(const name of ['src','data-src','data-original','data-lazy-src','data-image-url','data-url'])add(image.getAttribute?.(name));
    const srcset=image.getAttribute?.('srcset')||image.getAttribute?.('data-srcset')||'';
    const candidates=[];
    for(const part of String(srcset).split(',')){
      const bits=part.trim().split(/\s+/);if(!bits[0])continue;
      const descriptor=bits[1]||'';const match=descriptor.match(/^(\d+(?:\.\d+)?)(w|x)$/);
      const score=match?Number(match[1])*(match[2]==='x'?1000000:1):0;
      candidates.push({url:bits[0],score,index:candidates.length});
    }
    candidates.sort((a,b)=>b.score-a.score||a.index-b.index);
    for(const candidate of candidates)add(candidate.url);
    return values;
  },
  mediaUrlFromImage(image){
    for(const candidate of this.mediaCandidates(image)){
      try{return this.mediaUrl(candidate);}catch{}
    }
    return '';
  },
  postUrl(value){try{const u=new URL(value),m=u.pathname.match(/^\/([\w]{1,50})\/status\/(\d+)(?:\/photo\/[1-4])?$/);return u.protocol==='https:'&&this.X_HOSTS.includes(u.hostname)&&m?`https://x.com/${m[1]}/status/${m[2]}`:'';}catch{return '';}}
});
if(typeof module!=='undefined')module.exports=GalleryX;
