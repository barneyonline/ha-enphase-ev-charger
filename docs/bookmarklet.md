# Enphase Header Helper Bookmarklet

This bookmarklet helps you quickly extract the headers needed by the Enphase EV Charger (Cloud) integration without inspecting requests manually.

What it does
- Injects a tiny script on the Enlighten site to capture the next API request.
- Extracts:
  - Site ID (from URL or request path)
  - `e-auth-token` header
  - `Cookie` (from `document.cookie`)
- Copies a ready-to-paste block to your clipboard:

```
SITE_ID=...
EAUTH=...
COOKIE=...
```

How to use (Chrome/Edge/Brave/Safari)
1) Create a new bookmark. Set the URL of the bookmark to the code in the “Bookmarklet code” section below (one line).
2) Open https://enlighten.enphaseenergy.com and log in to your site.
3) Click the bookmarklet once (you’ll see a small alert).
4) Refresh the page or interact with the site so it makes an API call.
5) The helper copies the values to your clipboard. Paste them into the HA config step (or the smoke test script).

Notes
- If the browser blocks clipboard access, the bookmarklet will show an alert with the values so you can copy manually.
- The bookmarklet only runs in your browser tab; nothing is sent anywhere.

## Bookmarklet code (paste as the URL of a bookmark)

Paste the entire line below into the bookmark’s URL field:

```
javascript:(()=>{const L='[Enphase Headers] ';const copy=t=>navigator.clipboard&&navigator.clipboard.writeText(t).then(()=>alert(L+'Copied to clipboard')).catch(()=>alert(L+t));const getSite=()=>{const m=location.pathname.match(/\/pv\/systems\/(\d+)\//);return m?m[1]:''};let lastUrl='';const dump=(eauth)=>{try{const site=getSite()||((lastUrl.match(/\/evse_controller\/(\d+)\//)||[])[1]||'');const cookie=document.cookie;const txt=`SITE_ID=${site}\nEAUTH=${eauth}\nCOOKIE=${cookie}\n`;copy(txt);}catch(e){alert(L+'Error: '+e)}};const wrapFetch=window.fetch;window.fetch=async(...a)=>{try{const req=a[0],init=a[1]||{};lastUrl=(typeof req==='string')?req:req.url;const h=new Headers(init.headers||(req&&req.headers)||{});const tok=h.get('e-auth-token');if(tok){dump(tok)}}catch(e){}return wrapFetch.apply(this,a)};const openXhr=XMLHttpRequest.prototype.open;const setXhr=XMLHttpRequest.prototype.setRequestHeader;XMLHttpRequest.prototype.open=function(m,u,...r){this.__url=u;lastUrl=u;return openXhr.call(this,m,u,...r)};XMLHttpRequest.prototype.setRequestHeader=function(k,v){try{if(String(k).toLowerCase()==='e-auth-token'){dump(v)}}catch(e){}return setXhr.call(this,k,v)};alert(L+'Helper installed. Interact with the site; headers will be captured and copied.')})()
```

## Troubleshooting
- Clipboard blocked: Some browsers may block clipboard access from bookmarklets. If so, the helper will show an alert with the same values for manual copy.
- Empty Site ID: Navigate to your system summary page (URL contains `/pv/systems/<site_id>/summary`) and click the bookmarklet again.
- No values copied: After enabling, refresh the page or click inside the app so it triggers an API call.

## Security
Treat the copied values like passwords. They expire periodically; if you see 401 errors later, repeat the steps to refresh them.
