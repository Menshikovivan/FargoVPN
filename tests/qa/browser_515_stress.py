#!/usr/bin/env python3
"""Small headless-browser stress/regression checks for the critical 5.1.17 controllers."""
from __future__ import annotations
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
LOGS_JS = (ROOT / 'app/static/logs.js').read_text(encoding='utf-8')
DIAG_JS = (ROOT / 'app/static/diagnostic.js').read_text(encoding='utf-8')

HTML = '''<!doctype html><body>
<select id="logs-service"><option value="app">app</option><option value="github-publish">github</option></select>
<select id="logs-lines"><option value="100">100</option><option value="2000">2000</option></select>
<select id="logs-level"><option value="">Все</option></select>
<input id="logs-query"><input id="logs-auto" type="checkbox"><button id="logs-refresh" type="button">Обновить</button><a id="logs-download" href="#">Скачать</a><span id="logs-loading"></span><button id="logs-retry" type="button" hidden>Повторить</button><pre id="logs-output"></pre>
<div id="backup-live-panel"></div><span id="backup-live-status"></span><span id="backup-live-archive"></span><span id="backup-live-phase"></span><span id="backup-live-progress-bar"></span><span id="backup-live-progress-text"></span><div id="backup-live-events"></div>
<button id="diagnostic-collect" type="button">Диагностика</button><pre id="diagnostic-progress"></pre><a id="diagnostic-download" hidden>Скачать</a>
'''
FETCH = r'''window.apiFetch=undefined;window.__calls=[];window.panelToast=()=>{};const big=Array.from({length:5000},(_,i)=>`2026-10-09 INFO stress-${i}`).join("\n");window.fetch=async function(url,opts){const u=String(url);window.__calls.push(u);if(u.includes("/api/logs"))return{ok:true,status:200,url:u,headers:new Headers({"content-type":"application/json"}),text:async()=>JSON.stringify({ok:true,status:"ok",source:"fixture",detail:"stress",text:big,truncated:false})};if(u.endsWith("/api/diagnostics/collect"))return{ok:true,status:202,url:u,headers:new Headers({"content-type":"application/json"}),text:async()=>JSON.stringify({ok:true,job:{job_id:"stress-job"}})};if(u.includes("/api/diagnostics/collect/stress-job"))return{ok:true,status:200,url:u,headers:new Headers({"content-type":"application/json"}),text:async()=>JSON.stringify({ok:true,state:"completed",path:"/tmp/stress.log",checks:[{status:"PASS",name:"stress"}]})};return{ok:true,status:200,url:u,headers:new Headers({"content-type":"application/json"}),text:async()=>JSON.stringify({ok:true})}};'''

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page()
    errors=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(f'console.{m.type}: {m.text}') if m.type=='error' else None)
    page.set_content(HTML, wait_until='domcontentloaded')
    page.add_script_tag(content=FETCH)
    page.add_script_tag(content=LOGS_JS)
    page.add_script_tag(content=DIAG_JS)
    page.wait_for_timeout(150)

    # 50 rapid refresh clicks; controller must remain usable and not throw.
    for _ in range(50):
        page.locator('#logs-refresh').dispatch_event('click')
    page.wait_for_timeout(350)
    assert 'stress-4999' in page.locator('#logs-output').text_content()

    # Synchronous double/triple click is accepted only once because the button is disabled immediately.
    page.evaluate("for(let i=0;i<10;i++) document.getElementById('diagnostic-collect').click()")
    page.wait_for_timeout(300)
    collect_calls=[u for u in page.evaluate('window.__calls') if u.endswith('/api/diagnostics/collect')]
    assert len(collect_calls)==1, collect_calls
    assert 'PASS' in page.locator('#diagnostic-progress').text_content()
    assert not errors, errors
    browser.close()
print('BROWSER_STRESS_514=PASS')
