from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_service_worker_network_only_api_ignores_legacy_cached_empty_chat_feed(tmp_path):
    """Execute the real worker fetch handler with an intentionally stale legacy cache."""
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    worker = worker.replace("__FARGOVPN_CACHE__", "fargovpn-static-v5.1.41")
    worker = worker.replace("__FARGOVPN_BASE__", "/app/")
    worker = worker.replace("__FARGOVPN_VERSION__", "5.1.41")
    worker_path = tmp_path / "service-worker.js"
    worker_path.write_text(worker, encoding="utf-8")
    harness = r'''const fs = require('fs');
const assert = require('assert');
const handlers = {};
let legacyCacheLookups = 0;
let networkRequests = 0;
const stale = new Response(JSON.stringify({ok:true, events:[], next_after_id:900}), {
  status:200, headers:{'Content-Type':'application/json'}
});
global.self = {
  location:new URL('https://fargo.test/app/service-worker.js'),
  addEventListener:(type, fn)=>{handlers[type]=fn;},
  clients:{claim:async()=>{}, matchAll:async()=>[], openWindow:async()=>null},
  skipWaiting:async()=>{}, registration:{showNotification:async()=>{}}
};
global.caches = {
  match:async request=>{legacyCacheLookups++; if(String(request.url||request).includes('/api/panel/messages/chat-feed')) return stale.clone(); return undefined;},
  open:async name=>({match:async()=>undefined, put:async()=>{}, add:async()=>{}}),
  keys:async()=>['legacy-fargovpn-cache-v1'], delete:async()=>true
};
global.fetch = async request=>{
  networkRequests++;
  return new Response(JSON.stringify({ok:true, tg_id:123, events:[{id:901,text:'fresh-network-event'}], next_after_id:901}), {
    status:200, headers:{'Content-Type':'application/json'}
  });
};
const workerSource = fs.readFileSync(process.argv[1], 'utf8');
eval(workerSource);
(async()=>{
  assert.equal(typeof handlers.fetch, 'function', 'fetch handler registered');
  const request = new Request('https://fargo.test/app/api/panel/messages/chat-feed?tg_id=123&after_id=900', {
    method:'GET', cache:'no-store', headers:{Accept:'application/json'}
  });
  let responsePromise;
  handlers.fetch({request, respondWith:promise=>{responsePromise=Promise.resolve(promise);}});
  assert(responsePromise, 'fetch event was intercepted');
  const response = await responsePromise;
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.events[0].text, 'fresh-network-event');
  assert.equal(networkRequests, 1, 'API must use network exactly once');
  assert.equal(legacyCacheLookups, 0, 'global CacheStorage lookup must not run for dynamic API');
  console.log('PASS: stale legacy empty chat-feed ignored; live network event returned');
})().catch(error=>{ console.error(error); process.exit(1); });'''
    result = subprocess.run(
        [shutil.which("node"), "-e", harness, str(worker_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"Node Service Worker harness failed:\n{result.stdout}\n{result.stderr}"
    assert "PASS: stale legacy empty chat-feed ignored" in result.stdout
