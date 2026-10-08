#!/usr/bin/env python3
"""Real headless-Chromium smoke tests for 5.1.14 page controllers."""
from __future__ import annotations
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
LOGS_JS = (ROOT / 'app/static/logs.js').read_text(encoding='utf-8')
DIAG_JS = (ROOT / 'app/static/diagnostic.js').read_text(encoding='utf-8')

HTML = '''<!doctype html><body>
<select id='logs-service'><option value='app'>app</option><option value='github-publish'>github</option></select>
<select id='logs-lines'><option value='100'>100</option><option value='1000'>1000</option></select>
<select id='logs-level'><option value=''>Все</option><option value='ERROR'>ERROR</option></select>
<input id='logs-query'><input id='logs-auto' type='checkbox'><button id='logs-refresh' type='button'>Обновить</button><a id='logs-download' href='#'>Скачать</a><span id='logs-loading'></span><button id='logs-retry' type='button' hidden>Повторить</button><pre id='logs-output'></pre>
<div id='backup-live-panel'></div><span id='backup-live-status'></span><span id='backup-live-archive'></span><span id='backup-live-phase'></span><span id='backup-live-progress-bar'></span><span id='backup-live-progress-text'></span><div id='backup-live-events'></div>
<button id='diagnostic-collect' type='button'>Диагностика</button><pre id='diagnostic-progress'></pre><a id='diagnostic-download' hidden>Скачать</a>
'''
FETCH = r'''window.apiFetch = undefined; window.__calls=[]; window.panelToast=(m)=>window.__toast=String(m); window.fetch=async function(url,opts){ const u=String(url); window.__calls.push(u); if(u.includes('/api/logs')) return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),text:async()=>JSON.stringify({ok:true,status:'ok',source:'/var/log/vpn_bot.log',detail:'mock',text:'REAL BROWSER LOG'})}; if(u.endsWith('/api/diagnostics/collect')) return {ok:true,status:202,headers:new Headers({'content-type':'application/json'}),text:async()=>JSON.stringify({ok:true,job:{job_id:'job-514'}})}; if(u.includes('/api/diagnostics/collect/job-514')) return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),text:async()=>JSON.stringify({ok:true,state:'completed',checks:[{status:'PASS',name:'version'},{status:'PASS',name:'logs'}]})}; return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),text:async()=>JSON.stringify({ok:true})}; };'''

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page()
    errors=[]; bad=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(f'console.{m.type}: {m.text}') if m.type=='error' else None)
    page.on('response', lambda r: bad.append((r.status,r.url)) if r.status>=400 else None)
    page.set_content(HTML, wait_until='domcontentloaded')
    page.add_script_tag(content=FETCH)
    page.add_script_tag(content=LOGS_JS)
    page.add_script_tag(content=DIAG_JS)
    page.select_option('#logs-service','github-publish')
    page.wait_for_timeout(250)
    assert 'REAL BROWSER LOG' in page.locator('#logs-output').text_content()
    page.locator('#logs-refresh').click(); page.wait_for_timeout(150)
    assert any('service=github-publish' in u for u in page.evaluate('window.__calls'))
    page.locator('#diagnostic-collect').click(); page.wait_for_timeout(250)
    assert not page.locator('#diagnostic-download').get_attribute('hidden') or page.locator('#diagnostic-download').is_visible()
    assert 'PASS' in page.locator('#diagnostic-progress').text_content()
    assert not errors, errors
    assert not bad, bad
    browser.close()
print('BROWSER_SMOKE_514=PASS')
