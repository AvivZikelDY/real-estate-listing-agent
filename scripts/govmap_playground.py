# ruff: noqa: E501 - this file embeds an HTML page; its CSS and markup lines
# are naturally longer than the Python line limit and are not worth wrapping.
"""A tiny local web tool for poking the govmap real-estate API by hand.

    python scripts/govmap_playground.py

Then open http://localhost:8777 — it opens automatically.

WHY A LOCAL SERVER AND NOT JUST AN HTML FILE
--------------------------------------------
govmap sends no CORS headers, so a page opened from disk cannot call it
directly — the browser blocks every request. This script therefore serves
a page AND proxies the calls server-side, where CORS does not apply.

This is a developer scratch tool. It is not imported by the app, not part
of the `price_signal` deliverable, and nothing depends on it.
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

PORT = 8777
BASE = "https://www.govmap.gov.il/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; listing-agent-playground/0.1)"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>govmap playground</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#2a2f3a;--fg:#e6e9ef;--mut:#98a2b3;--acc:#5b9dff;--ok:#3fb950}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;padding:24px}
h1{font-size:18px;margin:0 0 4px} .sub{color:var(--mut);margin-bottom:20px;font-size:13px}
.step{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:14px}
.step h2{font-size:14px;margin:0 0 2px;color:var(--acc)}
.ep{color:var(--mut);font-size:12px;margin-bottom:10px;word-break:break-all}
label{display:block;font-size:12px;color:var(--mut);margin:8px 0 3px}
input{width:100%;padding:8px;background:#0c0e12;border:1px solid var(--line);
border-radius:5px;color:var(--fg);font:inherit}
button{margin-top:10px;padding:8px 16px;background:var(--acc);color:#06101f;border:0;
border-radius:5px;font:inherit;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.1)} button:disabled{opacity:.5;cursor:wait}
pre{background:#0c0e12;border:1px solid var(--line);border-radius:5px;padding:10px;
margin-top:10px;max-height:340px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
.row{display:flex;gap:10px} .row>div{flex:1}
.hint{color:var(--ok);font-size:12px;margin-top:6px}
</style></head><body>
<h1>govmap real-estate API playground</h1>
<div class="sub">The four endpoints <code>price_signal</code> uses, in the order it calls them.
Each step fills in the next one automatically.</div>

<div class="step">
  <h2>1 &middot; autocomplete &mdash; address to a point</h2>
  <div class="ep">POST /search-service/autocomplete</div>
  <label>Address (Hebrew works best)</label>
  <input id="addr" value="&#1512;&#1493;&#1496;&#1513;&#1497;&#1500;&#1491; 30 &#1514;&#1500; &#1488;&#1489;&#1497;&#1489;">
  <button onclick="step1()">Run</button>
  <div class="hint" id="h1"></div>
  <pre id="o1">&mdash;</pre>
</div>

<div class="step">
  <h2>2 &middot; deals by radius &mdash; point to polygon IDs</h2>
  <div class="ep">GET /real-estate/deals/{x},{y}/{radius}
  &nbsp;&mdash;&nbsp; returns polygon metadata only, NOT deals</div>
  <div class="row">
    <div><label>x (Web Mercator)</label><input id="x"></div>
    <div><label>y (Web Mercator)</label><input id="y"></div>
    <div><label>radius</label><input id="r" value="300"></div>
  </div>
  <button onclick="step2()">Run</button>
  <div class="hint" id="h2"></div>
  <pre id="o2">&mdash;</pre>
</div>

<div class="step">
  <h2>3 &middot; street deals &mdash; polygon to transactions</h2>
  <div class="ep">GET /real-estate/street-deals/{polygon_id}?limit=500&amp;dealType=2</div>
  <label>polygon_id</label><input id="p1">
  <button onclick="deals('street')">Run</button>
  <div class="hint" id="h3"></div>
  <pre id="o3">&mdash;</pre>
</div>

<div class="step">
  <h2>4 &middot; neighborhood deals &mdash; the wider net</h2>
  <div class="ep">GET /real-estate/neighborhood-deals/{polygon_id}?limit=500&amp;dealType=2</div>
  <label>polygon_id</label><input id="p2">
  <button onclick="deals('neighborhood')">Run</button>
  <div class="hint" id="h4"></div>
  <pre id="o4">&mdash;</pre>
</div>

<script>
async function call(path, params, outEl, btn){
  const el=document.getElementById(outEl); el.textContent='loading...';
  if(btn) btn.disabled=true;
  try{
    const q=new URLSearchParams(params).toString();
    const r=await fetch('/api/'+path+'?'+q);
    const j=await r.json();
    el.textContent=JSON.stringify(j,null,2);
    return j;
  }catch(e){ el.textContent='ERROR: '+e; return null; }
  finally{ if(btn) btn.disabled=false; }
}
async function step1(){
  const j=await call('autocomplete',{q:document.getElementById('addr').value},'o1',event.target);
  const res=(j&&j.results)||[];
  const h=document.getElementById('h1');
  if(!res.length){ h.textContent='0 results -> geo_resolver reports not_found'; return; }
  const m=(res[0].shape||'').match(/POINT\\(([-\\d.]+) ([-\\d.]+)\\)/);
  if(m){ document.getElementById('x').value=m[1]; document.getElementById('y').value=m[2]; }
  const ratio=res.length>1?(res[0].score/res[1].score).toFixed(2):'n/a';
  h.textContent=res.length+' result(s); top/runner-up score ratio = '+ratio+
    ' (>=2.0 -> resolved, else ambiguous). Coordinates copied to step 2.';
}
async function step2(){
  const j=await call('polygons',{x:document.getElementById('x').value,
    y:document.getElementById('y').value,r:document.getElementById('r').value},'o2',event.target);
  const arr=Array.isArray(j)?j.filter(e=>parseInt(e.dealscount||'0')>0):[];
  if(arr.length){
    document.getElementById('p1').value=arr[0].polygon_id;
    document.getElementById('p2').value=arr[0].polygon_id;
  }
  document.getElementById('h2').textContent=arr.length+
    ' polygons with deals>0 (note dealscount is a STRING). First one copied to steps 3 and 4.';
}
async function deals(kind){
  const id=document.getElementById(kind==='street'?'p1':'p2').value;
  const out=kind==='street'?'o3':'o4', hint=kind==='street'?'h3':'h4';
  const j=await call('deals',{kind:kind,id:id},out,event.target);
  const rows=(j&&j.data)||[];
  const res=rows.filter(d=>['&#1491;&#1497;&#1512;&#1492;','&#1491;&#1497;&#1512;&#1492; &#1489;&#1489;&#1497;&#1514; &#1511;&#1493;&#1502;&#1493;&#1514;','&#1491;&#1497;&#1512;&#1514; &#1490;&#1503;','&#1491;&#1497;&#1512;&#1514; &#1490;&#1490;'].includes(d.dealNatureDescription));
  document.getElementById(hint).textContent=
    'totalCount='+((j&&j.totalCount)||0)+', returned='+rows.length+
    ', residential='+res.length+' (the rest are offices/shops/parking - filtered out).';
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if parsed.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")

        try:
            with httpx.Client(headers=HEADERS, timeout=60) as client:
                if parsed.path == "/api/autocomplete":
                    r = client.post(
                        f"{BASE}/search-service/autocomplete",
                        json={
                            "searchText": params.get("q", ""),
                            "language": "he",
                            "isAccurate": False,
                            "maxResults": 10,
                        },
                    )
                elif parsed.path == "/api/polygons":
                    r = client.get(
                        f"{BASE}/real-estate/deals/"
                        f"{params.get('x')},{params.get('y')}/{params.get('r', 300)}"
                    )
                elif parsed.path == "/api/deals":
                    kind = "street-deals" if params.get("kind") == "street" else (
                        "neighborhood-deals"
                    )
                    r = client.get(
                        f"{BASE}/real-estate/{kind}/{params.get('id')}",
                        params={"limit": 500, "dealType": 2},
                    )
                else:
                    return self._send(404, json.dumps({"error": "unknown path"}))

            return self._send(r.status_code, r.content)
        except Exception as exc:
            return self._send(502, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))


def main() -> None:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"govmap playground running at {url}   (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
