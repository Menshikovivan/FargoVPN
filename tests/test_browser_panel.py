from pathlib import Path
import json
import tempfile

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "app" / "webapp.py"


def _updates_script() -> str:
    source = WEBAPP.read_text(encoding="utf-8")
    start = source.index("    script = r'''<script>", source.index('@app.get("/updates"'))
    end = source.index("</script>'''.replace", start)
    script = source[start:end].split("<script>\n", 1)[1]
    return (
        script.replace("__INITIAL__", "{}")
        .replace("__PAGE_VERSION__", '"5.1.18"')
        .replace("__PUBLISHER__", "true")
        .replace("__BASE_PATH__", '""')
    )


def _logs_script() -> str:
    return (ROOT / "app" / "static" / "logs.js").read_text(encoding="utf-8")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_real_browser_publish_button_upload_progress_and_live_log():
    html = """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><body>
    <div id='update-progress-bar'><span></span></div><strong id='update-progress-value'></strong><span id='update-message'></span><span id='update-phase'></span><span id='update-elapsed'></span><div id='update-connection-note'></div><div id='update-error'></div><span id='update-state'></span><span id='update-current-version'></span><pre id='update-live-output'></pre>
    <form id='publish-update-form'><input id='publish-archive' type='file'><button id='publish-button' type='button'>Publish</button>
    <div id='upload-progress'><span id='upload-progress-bar'></span><span id='upload-progress-text'></span></div>
    <div id='github-publish-progress'><span id='github-publish-progress-bar'></span><span id='github-publish-progress-value'></span><span id='github-publish-progress-text'></span><button id='publish-cancel-button' type='button' disabled>Cancel</button><pre id='github-publish-live-log'></pre></div></form>
    <form id='check-updates-form' action='/updates/check'><button id='check-updates-button'>check</button><span id='check-updates-status'></span></form>
    <script>window.confirm=()=>true;window.panelToast=(m)=>window.lastToast=m;window.__lastPoll=0;window.fetch=(u)=>Promise.resolve({ok:true,status:200,json:async()=>({state:'completed',progress:100,version:'5.1.18',github_tag:'v5.1.18',message:'GitHub Release подтверждён',output:'2026-10-08 [INFO] [pub-browser-1] GitHub Release подтверждён'})});class FakeXHR{constructor(){this.upload={};this.status=0;this.responseText='';}open(m,u){this.method=m;this.url=u;}setRequestHeader(){}send(){window.__xhr={method:this.method,url:this.url};if(this.upload.onprogress)this.upload.onprogress({lengthComputable:true,loaded:10,total:10});setTimeout(()=>{this.status=202;this.responseText=JSON.stringify({ok:true,job:{job_id:'pub-browser-1'},status:{state:'queued',progress:2,version:'5.1.18'}});this.onload&&this.onload();},30);}}window.XMLHttpRequest=FakeXHR;</script>
    <script>""" + _updates_script() + """</script></body>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="domcontentloaded")
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
            archive.write(b"test archive")
            archive.flush()
            page.set_input_files("#publish-archive", archive.name)
            page.locator("#publish-button").click()
            page.wait_for_function("document.getElementById('upload-progress-bar').style.width === '100%' || document.getElementById('upload-progress-text').textContent.includes('Архив принят')", timeout=3000)
            assert page.locator("#publish-button").is_disabled()
            assert page.evaluate("window.__xhr.url") == "/updates/publish"
            page.wait_for_function("document.getElementById('github-publish-progress-value').textContent === '100%'", timeout=5000)
            assert "GitHub Release подтверждён" in page.locator("#github-publish-live-log").text_content()
        assert not errors
        browser.close()


@pytest.mark.skip(reason="Legacy about:blank fixture superseded by real HTTP browser smoke in 5.1.18")
def test_real_browser_logs_switch_refresh_and_filter():
    html = """<!doctype html><base href='http://qa.local/'><body>
    <select id='logs-service'><option value='bot'>bot</option><option value='github-publish'>github</option></select>
    <select id='logs-lines'><option value='100'>100</option><option value='500'>500</option><option value='1000'>1000</option></select>
    <select id='logs-level'><option value=''>Все</option><option value='ERROR'>ERROR</option></select>
    <input id='logs-query'><input id='logs-auto' type='checkbox'><button id='logs-refresh'>Обновить</button><a id='logs-download' href='#'>Скачать</a><span id='logs-loading'></span><pre id='logs-output'></pre>
    <div id='backup-live-panel' style='display:none'></div><span id='backup-live-status'></span><span id='backup-live-archive'></span><span id='backup-live-phase'></span><span id='backup-live-progress-bar'></span><span id='backup-live-progress-text'></span><div id='backup-live-events'></div>
    <script>window.fetch=(url)=>Promise.resolve({ok:true,status:200,headers:new Headers({'content-type':'application/json'}),json:async()=>({service:'github-publish',lines:100,text:'github stage\\n[ERROR] mock error'}),text:async()=>JSON.stringify({service:'github-publish',lines:100,text:'github stage\n[ERROR] mock error'})});</script>
    <script>""" + _logs_script() + """</script></body>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="domcontentloaded")
        page.select_option("#logs-service", "github-publish")
        page.locator("#logs-refresh").click()
        page.wait_for_timeout(300)
        assert "github stage" in page.locator("#logs-output").text_content()
        page.select_option("#logs-lines", "1000")
        assert "lines=1000" in page.locator("#logs-download").get_attribute("href")
        page.check("#logs-auto")
        assert page.locator("#logs-auto").is_checked()
        assert not errors
        browser.close()

@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_real_browser_update_progress_and_live_log_survive_status_poll():
    html = """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><body>
    <div id='update-progress-bar'><span></span></div><strong id='update-progress-value'></strong><span id='update-message'></span><span id='update-phase'></span><span id='update-elapsed'></span><div id='update-connection-note'></div><div id='update-error'></div><span id='update-state'></span><span id='update-current-version'></span><pre id='update-live-output'></pre>
    <form id='apply-update-form'><button id='apply-update-button' type='submit'>Apply</button></form>
    <script>window.confirm=()=>true;window.__polls=0;window.fetch=(u)=>{window.__polls++;return Promise.resolve({ok:true,status:200,json:async()=>({job_id:'upd-browser-1',state:'installing',progress:42,phase:'python',message:'Устанавливаются Python-зависимости',installed_version:'5.1.18',output:'2026-10-08 22:00:00 [INFO] pip: 42%'})});};</script>
    <script>""" + _updates_script() + """</script></body>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_function("document.getElementById('update-progress-value').textContent === '42%'", timeout=3000)
        assert page.locator("#update-message").text_content() == "Устанавливаются Python-зависимости"
        assert "python" in page.locator("#update-phase").text_content()
        assert "pip: 42%" in page.locator("#update-live-output").text_content()
        assert page.locator("#update-current-version").text_content() == "5.1.18"
        assert page.evaluate("window.__polls") >= 1
        assert not errors
        browser.close()


def _messages_script_511() -> str:
    source = WEBAPP.read_text(encoding="utf-8")
    start = source.index("    scripts = f" + "'''<script>(function(){{", source.index('def messages_page('))
    end = source.index("</script>" + "'''", start)
    script = source[start:end].split("<script>", 1)[1].replace("{{", "{").replace("}}", "}")
    return (script.replace("__MESSAGES_PATH__", '"/messages"').replace("__MESSAGES_FEED_PATH__", '"/api/panel/messages/feed"').replace("{selected}", "123").replace("{direction}", "all").replace("{message_kind}", "all").replace("{global_last_id}", "0"))


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_real_browser_messages_send_does_not_reload_and_echoes_event_511():
    html = """<!doctype html><base href="http://qa.local/"><body>
    <div data-message-list></div><input data-message-search><div data-message-chat data-selected-tg-id="123"></div>
    <form id='messages-compose-form' action='/users/123/message'><textarea id='messages-compose-text'></textarea><input id='media' type='file'><div id='messages-compose-status' hidden></div><button id='messages-compose-send' type='submit'>Отправить</button></form>
    <div data-message-dialog-count></div>
    <script>window.fetch=(url,opts)=>{if(opts&&opts.method==='POST')window.__send={url:String(url),method:opts.method,headers:opts.headers};return Promise.resolve({ok:true,status:200,json:async()=>({ok:true,event:{id:9001,tg_id:123,direction:'out',message_kind:'message',delivery_status:'delivered',display_username:'tester',created_at:'2026-10-08 22:10:00',text:'Привет'}})});};</script>
    <script>""" + _messages_script_511() + """</script></body>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page()
        errors=[]; navigations=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('framenavigated', lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        page.set_content(html, wait_until='domcontentloaded')
        page.locator('#messages-compose-text').fill('Привет')
        page.locator('#messages-compose-send').click()
        page.wait_for_function("document.getElementById('messages-compose-status').textContent === 'Сообщение отправлено'", timeout=3000)
        assert page.url.startswith('about:blank')
        assert navigations == []
        assert page.locator('[data-message-chat] [data-event-id="9001"]').count() == 1
        assert page.evaluate('window.__send.method') == 'POST'
        assert page.locator('#messages-compose-send').is_enabled()
        assert not errors
        browser.close()


def test_logs_page_does_not_read_journal_synchronously_on_initial_render_511():
    source = WEBAPP.read_text(encoding="utf-8")
    logs = (ROOT / "app" / "static" / "logs.js").read_text(encoding="utf-8")
    marker='@app.get("/logs", response_class=HTMLResponse)'
    page = source[source.index(marker):source.index('def replace_assignment', source.index(marker))]
    assert 'initial_text = ""' in page
    assert '_read_service_logs(service, lines, level, q)' not in page.split('    body = f', 1)[0]
    assert 'public_path("/static/logs.js")' in page
    assert "history.replaceState" in logs
    assert "Загрузка…" in logs

@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_real_browser_github_authorization_button_is_ajax_and_visible_511():
    html = """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'><body>
    <form id='github-test-form' action='/updates/github/test'><button id='github-test-button' type='submit'>Проверить авторизацию GitHub</button><span id='github-test-status'></span></form>
    <script>window.panelToast=(m)=>window.__toast=m;window.fetch=(url,opts)=>{window.__auth={url:String(url),method:opts.method};return Promise.resolve({ok:true,status:200,json:async()=>({ok:true,login:'admin',repository:'Menshikovivan/FargoVPN',detail:'GitHub авторизация подтверждена'})});};</script>
    <script>""" + _updates_script() + """</script></body>"""
    # The updates script also includes other page handlers; provide the minimal
    # DOM they expect so the authorization handler can run independently.
    html=html.replace("__VERSION__", "5.1.18")
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page=browser.new_page(); errors=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content(html, wait_until='domcontentloaded')
        page.locator('#github-test-button').click()
        page.wait_for_function("document.getElementById('github-test-status').textContent.includes('admin')", timeout=3000)
        assert page.locator('#github-test-button').is_enabled()
        assert page.evaluate('window.__auth.method') == 'POST'
        assert page.locator('#github-test-form').get_attribute('action') == '/updates/github/test'
        assert not errors
        browser.close()

@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_real_browser_messages_receives_incoming_without_navigation_512():
    html = """<!doctype html><base href="http://qa.local/"><body>
    <div data-message-list></div><input data-message-search><div data-message-chat data-selected-tg-id="123"></div>
    <form id='messages-compose-form' action='/users/123/message'><textarea id='messages-compose-text'></textarea><input id='media' type='file'><div id='messages-compose-status' hidden></div><button id='messages-compose-send' type='submit'>Отправить</button><button id='messages-compose-retry' type='button' hidden>Повторить</button></form>
    <div data-message-dialog-count></div>
    <script>window.__polls=0;window.fetch=(url,opts)=>{if(String(url).includes('/api/panel/messages/feed')){window.__polls++;return Promise.resolve({ok:true,status:200,json:async()=>({next_after_id:9100,unread:{total:1,items:[{tg_id:123,count:1}]},dialogs:[{tg_id:123,username:'tester',incoming_events:2,outgoing_events:1,last_event_id:9100}],selected_events:window.__polls===1?[{id:9100,tg_id:123,direction:'in',message_kind:'message',delivery_status:'received',display_username:'tester',created_at:'2026-10-09 00:30:00',text:'Входящее без перезагрузки'}]:[]})});}return Promise.reject(new Error('unexpected fetch '+url));};</script>
    <script>""" + _messages_script_511() + """</script></body>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page(); errors=[]; navigations=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('framenavigated', lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        page.set_content(html, wait_until='domcontentloaded')
        page.wait_for_function("document.body.innerText.includes('Входящее без перезагрузки')", timeout=5000)
        assert not navigations
        assert page.locator('.message-unread-badge').count() == 1
        assert not errors
        browser.close()
