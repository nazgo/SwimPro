const STATIC_CACHE="swimpro-v48-static";
const PAGE_CACHE="swimpro-v48-pages";
const OFFLINE_URL="/offline";
const SHELL=["/login","/offline","/static/style.css","/static/manifest.webmanifest","/static/pwa/icon-192.png","/static/pwa/icon-512.png","/static/pwa/apple-touch-icon.png"];
self.addEventListener("install",e=>{e.waitUntil(caches.open(STATIC_CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>![STATIC_CACHE,PAGE_CACHE].includes(k)).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener("fetch",e=>{
  if(e.request.method!=="GET") return;
  const u=new URL(e.request.url);
  if(e.request.mode==="navigate"){
    e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(PAGE_CACHE).then(x=>x.put(e.request,c));return r;}).catch(async()=>await caches.match(e.request)||await caches.match(OFFLINE_URL)));
    return;
  }
  if(u.origin===self.location.origin&&u.pathname.startsWith("/static/")){
    e.respondWith(caches.match(e.request).then(c=>c||fetch(e.request).then(r=>{const x=r.clone();caches.open(STATIC_CACHE).then(k=>k.put(e.request,x));return r;})));
  }
});