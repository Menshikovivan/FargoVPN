from pathlib import Path
import os
from tempfile import gettempdir
import json
import re
import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SRC = Path('/mnt/data/qa512_src/FargoVPN-5.1.12')
HAS_512_FIXTURE = ARCHIVE_SRC.exists()
CURRENT = ROOT / 'app'
OUT = Path(os.environ.get('FARGOVPN_QA_ARTIFACTS', Path(gettempdir()) / 'fargovpn_qa513_browser_artifacts'))
pytestmark = pytest.mark.skipif(not HAS_512_FIXTURE, reason='5.1.12 browser fixture is not available in this runtime')


def artifact_path(name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / name


def extract_messages_script(source_path: Path) -> str:
    source = source_path.read_text(encoding='utf-8')
    start = source.index("    scripts = f'''<script>(function(){{", source.index('def messages_page('))
    end = source.index("</script>'''", start)
    script = source[start:end].split('<script>', 1)[1].replace('{{','{').replace('}}','}')
    return (script.replace('__MESSAGES_PATH__', '"/messages"')
                 .replace('__MESSAGES_FEED_PATH__', '"/api/panel/messages/feed"')
                 .replace('{selected}', '123')
                 .replace('{direction}', 'all')
                 .replace('{message_kind}', 'all')
                 .replace('{global_last_id}', '0'))


def extract_logs_script(source_path: Path) -> str:
    source = source_path.read_text(encoding='utf-8')
    start = source.index('    script = """<script>', source.index('def logs(request'))
    end = source.index('</script>""".replace', start)
    script = source[start:end].split('<script>\n', 1)[1]
    return (script.replace('LIVE_URL', '"/api/backup/live"')
                 .replace('LOGS_URL', '"/api/logs"')
                 .replace('DOWNLOAD_URL', '"/api/logs/download"'))


def extract_github_handler(source_path: Path) -> str:
    source = source_path.read_text(encoding='utf-8')
    m = re.search(r'const githubTestForm=.*?\nconst publishForm=', source[source.index('def updates_page'):], re.S)
    assert m, 'github handler not found'
    return m.group(0).rsplit('\nconst publishForm=',1)[0].strip().rstrip(';')


def extract_publish_handler(source_path: Path) -> str:
    source = source_path.read_text(encoding='utf-8')
    start = source.index('const publishForm=')
    end = source.index('const cancelPublish=', start)
    return source[start:end] + "document.getElementById('publish-button')?.addEventListener('click',startPublishUpload);"


@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
def test_512_messages_reproduce_redirect_when_page_controller_is_blocked():
    panel_js = (ARCHIVE_SRC/'app/static/panel.js').read_text(encoding='utf-8')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page = browser.new_page()
        nav = []
        page.on('framenavigated', lambda f: nav.append(f.url) if f == page.main_frame else None)
        page.set_content("<meta name='fargovpn-csrf-token' content='csrf'><form method='post' action='/users/123/message'><button type='submit'>Отправить</button></form>")
        page.add_script_tag(content=panel_js)
        page.evaluate("window.fetch=async()=>({redirected:true,ok:true,status:200,type:'basic',url:'about:blank#user-card',headers:new Headers(),json:async()=>({})})")
        before = page.url
        page.locator('button').click()
        page.wait_for_timeout(150)
        assert page.url != before
        assert nav and nav[-1].endswith('#user-card')
        page.screenshot(path=str(artifact_path('5.1.12-message-redirect.png')), full_page=True)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
def test_513_messages_no_navigation_and_live_echo():
    panel_js = (CURRENT/'static/panel.js').read_text(encoding='utf-8')
    msg_js = extract_messages_script(ARCHIVE_SRC/'app/webapp.py')
    html = """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><div data-message-live-feed='1' data-selected-tg-id='123' data-direction='all' data-message-kind='all' data-global-last-id='0' data-messages-path='/messages' data-feed-path='/api/panel/messages/feed'><div data-message-list></div><input data-message-search><div data-message-chat data-selected-tg-id='123'></div><form id='messages-compose-form' data-no-navigation='1' data-ajax-form='1' action='/users/123/message' method='post'><textarea id='messages-compose-text'></textarea><input id='media' type='file'><div id='messages-compose-status' hidden></div><button id='messages-compose-send' type='submit'>Отправить</button><button id='messages-compose-retry' type='button' hidden>Повторить</button></form><div data-message-dialog-count></div></div>"""
    fake = """<script>window.fetch=async(url,opts)=>{if(opts&&opts.method==='POST'){return {ok:true,status:200,type:'basic',url:String(url),headers:new Headers({'content-type':'application/json'}),json:async()=>({ok:true,event:{id:9001,tg_id:123,direction:'out',message_kind:'message',delivery_status:'delivered',display_username:'tester',created_at:'2026-10-09 00:30:00',text:'Привет'}})}};return {ok:true,status:200,type:'basic',url:String(url),headers:new Headers({'content-type':'application/json'}),json:async()=>({next_after_id:0,dialogs:[],selected_events:[],unread:{total:0,items:[]}})}};</script>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page = browser.new_page(); errors=[]; nav=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('framenavigated', lambda f: nav.append(f.url) if f == page.main_frame else None)
        page.set_content(html + fake, wait_until='domcontentloaded')
        page.add_script_tag(content=panel_js)
        page.add_script_tag(content=msg_js)
        page.locator('#messages-compose-text').fill('Привет')
        before=page.url
        page.locator('#messages-compose-send').click()
        page.wait_for_function("document.getElementById('messages-compose-status').textContent === 'Сообщение отправлено'", timeout=3000)
        assert page.url == before
        assert nav == []
        assert page.locator('[data-message-chat] [data-event-id="9001"]').count() == 1
        assert page.locator('#messages-compose-text').input_value() == ''
        assert page.locator('#messages-compose-text').evaluate('(e)=>document.activeElement===e')
        assert not errors
        page.screenshot(path=str(artifact_path('5.1.18-messages.png')), full_page=True)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
@pytest.mark.skipif(not HAS_512_FIXTURE, reason="5.1.12 browser fixture is not available in this runtime")
def test_512_logs_do_not_switch_when_inline_controller_is_missing_and_513_does():
    panel_js_512=(ARCHIVE_SRC/'app/static/panel.js').read_text(encoding='utf-8')
    logs_js=extract_logs_script(ARCHIVE_SRC/'app/webapp.py')
    html = """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><select id='logs-service'><option value='app'>app</option><option value='github-publish'>github</option></select><select id='logs-lines'><option value='100'>100</option><option value='1000'>1000</option></select><select id='logs-level'><option value=''>Все</option><option value='ERROR'>ERROR</option></select><input id='logs-query'><button id='logs-refresh'>Обновить</button><button id='logs-retry' type='button' hidden>Повторить</button><input id='logs-auto' type='checkbox'><a id='logs-download' href='#'>Скачать</a><span id='logs-loading'></span><pre id='logs-output'></pre><div id='backup-live-panel'><span id='backup-live-status'></span><span id='backup-live-archive'></span><span id='backup-live-phase'></span><span id='backup-live-progress-bar'></span><span id='backup-live-progress-text'></span><div id='backup-live-events'></div></div>"""
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        # 5.1.12: no page-specific logs controller executes.
        p=browser.new_page(); calls=[]
        p.set_content(html)
        p.add_script_tag(content=panel_js_512)
        p.evaluate("window.fetch=async(u)=>{window.__calls=(window.__calls||[]).concat(String(u));return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),json:async()=>({service:'github-publish',text:'ok'})}}")
        p.locator('#logs-service').select_option('github-publish'); p.wait_for_timeout(150)
        assert not any('/api/logs' in str(x) for x in p.evaluate('window.__calls||[]'))
        # 5.1.18: same DOM with the actual controller executes.
        p.set_content(html)
        p.add_script_tag(content=panel_js_512)
        p.evaluate("window.fetch=async(u)=>{window.__calls=(window.__calls||[]).concat(String(u));return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),json:async()=>({service:'github-publish',text:'github log'})}}")
        p.add_script_tag(content=logs_js)
        p.locator('#logs-service').select_option('github-publish')
        p.wait_for_function("(window.__calls||[]).some(x=>x.includes('/api/logs'))", timeout=3000)
        assert 'github log' in p.locator('#logs-output').inner_text()
        p.screenshot(path=str(artifact_path('5.1.18-logs.png')), full_page=True)
        assert not p.evaluate('window.__pageErrors')
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
def test_512_github_auth_reproduces_redirect_and_513_stays_in_place():
    panel_512=(ARCHIVE_SRC/'app/static/panel.js').read_text(encoding='utf-8')
    handler=extract_github_handler(ARCHIVE_SRC/'app/webapp.py')
    base="""<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><form id='github-test-form' method='post' action='/updates/github/test'><button id='github-test-button' type='submit'>Проверить авторизацию GitHub</button><span id='github-test-status'></span></form>"""
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        p=browser.new_page(); nav=[]; p.on('framenavigated',lambda f: nav.append(f.url) if f==p.main_frame else None)
        p.set_content(base); p.add_script_tag(content=panel_512)
        p.evaluate("window.fetch=async()=>({redirected:true,ok:true,status:200,type:'basic',url:'about:blank#updates',headers:new Headers(),json:async()=>({})})")
        p.locator('#github-test-button').click(); p.wait_for_timeout(150)
        assert p.url.endswith('#updates') and nav
        # 5.1.18 page handler executes; it must not navigate.
        p.set_content(base)
        p.add_script_tag(content=panel_512)
        p.evaluate("window.panelToast=(m)=>window.__toast=m;window.fetch=async(u,o)=>{window.__auth={url:String(u),method:(o&&o.method)||'GET'};return {ok:true,status:200,headers:new Headers({'content-type':'application/json'}),json:async()=>({ok:true,login:'admin',repository:'Menshikovivan/FargoVPN'})}}")
        p.add_script_tag(content="const csrfToken='csrf'; const fetchWithTimeout=(u,o)=>window.fetch(u,o);"+handler)
        before2=p.url; nav_before=len(nav); p.locator('#github-test-button').click(); p.wait_for_function("window.__auth && document.getElementById('github-test-status').textContent.includes('admin')",timeout=3000)
        assert p.url == before2
        assert len(nav)==nav_before  # no new navigation after reset
        assert p.evaluate('window.__auth.method') == 'POST'
        p.screenshot(path=str(artifact_path('5.1.18-github-auth.png')),full_page=True)
        browser.close()


def test_release_client_version_and_nginx_contract_are_present():
    panel=(CURRENT/'static/panel.js').read_text(encoding='utf-8')
    install=(CURRENT/'install.sh').read_text(encoding='utf-8')
    guard=(CURRENT/'nginx_panel_guard.py').read_text(encoding='utf-8')
    assert "PANEL_BUILD_VERSION = '5.1.18'" in panel
    assert "nginx_panel_guard.py\" --once" in install
    assert 'proxy_hide_header Content-Security-Policy' in guard
    assert 'caches.keys()' in panel and 'controllerchange' in panel

@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
def test_513_client_server_version_mismatch_shows_refresh_banner():
    panel=(CURRENT/'static/panel.js').read_text(encoding='utf-8')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        page=browser.new_page()
        page.set_content("<meta name='fargovpn-app-version' content='5.1.12'><div id='global-update-slot'></div><main>panel</main>")
        page.add_script_tag(content=panel)
        page.wait_for_selector('#fargovpn-version-mismatch', timeout=3000)
        assert '5.1.18' in page.locator('#fargovpn-version-mismatch').inner_text()
        page.screenshot(path=str(artifact_path('5.1.18-version-mismatch.png')), full_page=True)
        browser.close()

@pytest.mark.skipif(sync_playwright is None, reason='Playwright is not installed')
def test_512_publish_button_is_dead_without_controller_and_513_starts_ajax_upload():
    panel_512=(ARCHIVE_SRC/'app/static/panel.js').read_text(encoding='utf-8')
    publish_js=extract_publish_handler(CURRENT/'webapp.py')
    base="""<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><form id='publish-update-form' method='post' action='/updates/publish'><input id='publish-archive' type='file' name='archive'><button id='publish-button' type='BUTTON'>Загрузить обновление в GitHub</button><button id='publish-cancel-button' type='button' disabled>Отменить</button><div id='upload-progress'><span id='upload-progress-bar'></span><span id='upload-progress-text'></span></div><div id='github-publish-progress'><span id='github-publish-progress-bar'></span><span id='github-publish-progress-text'></span><span id='github-publish-progress-value'></span><pre id='github-publish-live-log'></pre></div></form><div id='update-progress-bar'></div><span id='update-progress-value'></span><span id='update-message'></span><span id='update-phase'></span><span id='update-elapsed'></span><div id='update-connection-note'></div><div id='update-error'></div><span id='update-state'></span><span id='update-current-version'></span><pre id='update-live-output'></pre>"""
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        p=browser.new_page(); p.set_content(base); p.add_script_tag(content=panel_512)
        p.evaluate("window.__xhrCount=0;window.QAXHR=function(){this.upload={}};window.QAXHR.prototype.open=function(){};window.QAXHR.prototype.setRequestHeader=function(){};window.QAXHR.prototype.send=function(){window.__xhrCount++};window.XMLHttpRequest=window.QAXHR")
        p.locator('#publish-button').click(); p.wait_for_timeout(150)
        assert p.evaluate('window.__xhrCount') == 0
        # 5.1.18: real publish controller starts XHR and exposes progress.
        p.set_content(base.replace("type='BUTTON'", 'type="submit"'))
        p.add_script_tag(content=panel_512)
        p.evaluate("window.QAXHR=function(){this.upload={};this.status=202;this.responseText=JSON.stringify({ok:true,job:{job_id:'qa-pub-1'},status:{state:'queued',progress:2}})};window.QAXHR.prototype.open=function(m,u){this.url=u;window.__xhrUrl=u};window.QAXHR.prototype.setRequestHeader=function(){};window.QAXHR.prototype.send=function(){window.__xhrCount=(window.__xhrCount||0)+1;if(this.upload.onprogress)this.upload.onprogress({lengthComputable:true,loaded:1,total:1});setTimeout(()=>this.onload&&this.onload(),20)};window.XMLHttpRequest=window.QAXHR")
        p.add_script_tag(content="const purl=(x)=>x; const csrfToken='csrf'; const isPublisher=true; const fetchWithTimeout=(u,o)=>window.fetch(u,o);"+publish_js)
        # Use a tiny file so FormData is valid.
        path=artifact_path('qa-publish.tar.gz'); path.write_bytes(b'qa')
        p.set_input_files('#publish-archive',str(path))
        before=p.url; p.locator('#publish-button').click(); p.wait_for_timeout(100)
        assert p.url==before
        assert p.evaluate('window.__xhrCount') == 1
        assert p.evaluate('window.__xhrUrl') == '/updates/publish'
        p.screenshot(path=str(artifact_path('5.1.18-publish.png')),full_page=True)
        browser.close()
