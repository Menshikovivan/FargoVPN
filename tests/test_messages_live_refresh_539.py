from __future__ import annotations

from pathlib import Path
import os

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _messages_page_source() -> str:
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    start = source.index("def messages_page(")
    end = source.index('@app.get("/users/new"', start)
    return source[start:end]


def _messages_script() -> str:
    import ast
    import json
    source = (APP / "webapp.py").read_text(encoding="utf-8") if "APP" in globals() else WEBAPP.read_text(encoding="utf-8")
    page = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "messages_page")
    assignment = next(n for n in ast.walk(page) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "scripts" for t in n.targets))
    values = dict(selected=123, direction="all", message_kind="all", global_last_id=1000, selected_chat_last_id=900,
                  messages_path_js=json.dumps("/messages"), messages_feed_path_js=json.dumps("/api/panel/messages/feed"),
                  messages_chat_feed_path_js=json.dumps("/api/panel/messages/chat-feed"), messages_user_events_path_js=json.dumps("/api/users/123/events"),
                  json=json, public_path=lambda path: path)
    rendered = eval(compile(ast.Expression(assignment.value), "actual-messages-template", "eval"), values)
    return rendered.removeprefix("<script>").removesuffix("</script>")


def test_selected_chat_has_an_independent_authenticated_incremental_endpoint():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/api/panel/messages/chat-feed")')
    end = source.index("\n\nAPP_LOG_DEFAULT", start)
    endpoint = source[start:end]
    assert "require_auth(request)" in endpoint
    assert "user_events.recent_events(" in endpoint
    assert "after_id=after_id" in endpoint
    assert "overlap=max(0, min(int(overlap or 0), 100))" in endpoint
    assert '"next_after_id": next_after_id' in endpoint
    assert "limit=500" in endpoint
    assert '"events": serialized' in endpoint
    assert 'response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"' in endpoint
    assert 'response.headers["Pragma"] = "no-cache"' in endpoint
    global_feed = source[source.index('@app.get("/api/panel/messages/feed")'):source.index('@app.get("/api/panel/messages/chat-feed")')]
    assert 'response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"' in global_feed


def test_page_uses_separate_selected_chat_cursor_and_isolates_poll_errors():
    page = _messages_page_source()
    assert "selected_chat_last_id = (" in page
    assert 'messages_chat_feed_path_js = json.dumps(public_path("/api/panel/messages/chat-feed"))' in page
    assert "let selectedAfterId=Number({selected_chat_last_id})||0;" in page
    assert "chatUrl.searchParams.set('after_id',String(selectedAfterId));" in page
    assert "chatUrl.searchParams.set('overlap',String(CHAT_OVERLAP));" in page
    assert "chatUrl.searchParams.set('_poll_ts',String(Date.now()));" in page
    assert "url.searchParams.set('_poll_ts',String(Date.now()));" in page
    assert "[FargoVPN] messages dialog feed" in page
    assert "[FargoVPN] messages selected chat feed" in page
    assert "[FargoVPN] messages chat-feed; пробую совместимый events API" in page
    assert "let stopped=false,dialogTimer=0,chatTimer=0" in page
    assert "async function requestJson(url,timeoutMs=POLL_TIMEOUT_MS)" in page
    assert "signal:controller.signal" in page


def test_sender_row_uses_full_row_click_target_but_keeps_telegram_link_above_it():
    css = (APP / "static/panel.css").read_text(encoding="utf-8")
    assert ".message-user-open::after{content:\"\";position:absolute;inset:0;z-index:1" in css
    assert ".message-user-telegram{position:relative;z-index:2}" in css
    page = _messages_page_source()
    assert 'class="message-user-open" href="%s"' in page
    assert 'class="message-user-telegram"' in page


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_incoming_message_is_inserted_without_reload_even_if_global_feed_hangs():
    script = _messages_script()
    panel_css = (APP / "static/panel.css").read_text(encoding="utf-8")
    chat_css = panel_css[panel_css.index(".messages-toolbar{"):panel_css.index("/* Monitoring, updates, jobs, settings */")]
    html = """<!doctype html><meta charset='utf-8'><base href='http://qa.local/'>
    <style>""" + chat_css + """</style>
    <div data-message-list><div class='message-user-row active' data-tg-id='123'>
      <a class='message-user-open' href='/messages?tg_id=123'><span class='message-user-main'><strong>Тестовый пользователь</strong><span>#123</span></span></a>
      <a class='message-user-telegram' href='https://t.me/tester' target='_blank'>Telegram ↗</a>
      <small>1 входящее · 1 исходящее</small></div></div>
    <input data-message-search><div data-message-chat data-selected-tg-id='123'><div class='muted message-empty-state'>Для выбранной выборки сообщений нет.</div></div>
    <form id='messages-compose-form' action='/users/123/message' method='post'><textarea id='messages-compose-text'></textarea><input type='file' name='media'><div id='messages-compose-status' hidden></div><button id='messages-compose-send'>Отправить</button><button id='messages-compose-retry' hidden>Повторить</button></form><div data-message-dialog-count>1</div>
    <script>
      const nativeTimeout=window.setTimeout.bind(window);
      window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,Math.min(Number(ms)||0,80),...args);
      window.__chatFeedCalls=0;
      window.fetch=async (url,opts={})=>{
        const u=new URL(String(url),document.baseURI);
        if(u.pathname.endsWith('/api/panel/messages/feed')) return new Promise(()=>{}); // simulate a stalled proxy/upstream request
        if(u.pathname.endsWith('/api/panel/messages/chat-feed')) {
          window.__chatFeedCalls++;
          const after=Number(u.searchParams.get('after_id')||0);
          const deliver=window.__chatFeedCalls>=2 && after<1001;
          const event={id:1001,tg_id:123,direction:'in',message_kind:'message',delivery_status:'received',display_username:'tester',created_at:'2026-10-20 14:00:00',text:'Новое входящее без обновления'};
          return {ok:true,status:200,json:async()=>({ok:true,tg_id:123,events:deliver?[event]:[],next_after_id:deliver?1001:after})};
        }
        throw new Error('Unexpected fetch URL '+u.href);
      };
    </script>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=os.environ.get("FARGOVPN_CHROMIUM", "/usr/bin/chromium"),
            args=["--no-sandbox"],
        )
        page = browser.new_page()
        errors = []
        navigations = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        page.set_content(html, wait_until="domcontentloaded")
        page.add_script_tag(content=script)
        page.wait_for_function(
            "document.body.innerText.includes('Новое входящее без обновления')",
            timeout=5000,
        )
        assert page.locator('[data-message-chat] [data-event-id="1001"]').count() == 1
        assert page.locator('[data-message-chat] .message-empty-state').count() == 0
        assert page.locator('[data-message-chat]').inner_text().endswith("Новое входящее без обновления")
        assert not navigations
        assert page.evaluate("window.__chatFeedCalls") >= 2
        assert not errors
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_clicking_sender_count_area_opens_internal_chat():
    html = """<!doctype html><meta charset='utf-8'><base href='http://qa.local/'>
    <style>
      .message-user-row{position:relative;display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:9px 10px;border:1px solid transparent;border-radius:10px;cursor:pointer;width:360px}
      .message-user-open::after{content:"";position:absolute;inset:0;z-index:1;border-radius:inherit}
      .message-user-telegram{position:relative;z-index:2}
    </style>
    <div class='message-user-row' data-tg-id='123'>
      <a class='message-user-open' href='/messages?tg_id=123'><span class='message-user-main'><strong>Отправитель</strong><span>#123</span></span></a>
      <a class='message-user-telegram' href='https://t.me/tester' target='_blank'>Telegram ↗</a>
      <small>20 входящих · 10 исходящих</small>
    </div>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("FARGOVPN_CHROMIUM", "/usr/bin/chromium"), args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("document.querySelector('.message-user-open').addEventListener('click', e => { window.__internalChatClicked = true; e.preventDefault(); })")
        box = page.locator(".message-user-row small").bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert page.evaluate("window.__internalChatClicked === true")
        browser.close()

@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_sending_reply_does_not_skip_unpolled_incoming_event():
    """A send echo may have a greater ID than an incoming event that has not been polled yet."""
    script = _messages_script()
    html = """<!doctype html><meta charset='utf-8'><base href='http://qa.local/'>
    <div data-message-list></div><input data-message-search>
    <div data-message-chat data-selected-tg-id='123'></div>
    <form id='messages-compose-form' action='/users/123/message' method='post'>
      <textarea id='messages-compose-text'></textarea><input type='file' name='media'>
      <div id='messages-compose-status' hidden></div><button id='messages-compose-send'>Отправить</button>
      <button id='messages-compose-retry' hidden>Повторить</button>
    </form><div data-message-dialog-count>0</div>
    <script>
      const nativeTimeout=window.setTimeout.bind(window);
      window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,Number(ms)===650?10000:Math.min(Number(ms)||0,40),...args);
      window.__chatAfter=[];window.__sent=false;
      window.fetch=(url,opts={})=>{
        const u=new URL(String(url),document.baseURI);
        if(opts.method==='POST'){
          window.__sent=true;
          return Promise.resolve({ok:true,status:200,json:async()=>({ok:true,event:{id:902,tg_id:123,direction:'out',message_kind:'message',delivery_status:'delivered',display_username:'tester',created_at:'2026-10-20 14:10:00',text:'Ответ отправлен'}})});
        }
        if(u.pathname.endsWith('/api/panel/messages/feed'))return new Promise(()=>{});
        if(u.pathname.endsWith('/api/panel/messages/chat-feed')){
          window.__chatAfter.push(Number(u.searchParams.get('after_id')||0));
          const first=window.__chatAfter.length===1;
          const event={id:901,tg_id:123,direction:'in',message_kind:'message',delivery_status:'received',display_username:'tester',created_at:'2026-10-20 14:09:00',text:'Входящее до ответа'};
          return Promise.resolve({ok:true,status:200,json:async()=>({ok:true,next_after_id:first?901:Number(u.searchParams.get('after_id')||0),events:first?[event]:[]})});
        }
        return Promise.reject(new Error('Unexpected fetch URL '+u.href));
      };
    </script>""" + "<script>" + script + "</script>"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("FARGOVPN_CHROMIUM", "/usr/bin/chromium"), args=["--no-sandbox"])
        page = browser.new_page()
        errors=[]
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="domcontentloaded")
        page.locator('#messages-compose-text').fill('Ответ')
        page.locator('#messages-compose-send').click()
        page.wait_for_function("document.body.innerText.includes('Входящее до ответа')", timeout=5000)
        assert page.locator('[data-message-chat] [data-event-id="901"]').count() == 1
        assert page.locator('[data-message-chat] [data-event-id="902"]').count() == 1
        assert page.evaluate("window.__chatAfter[0] === 900")
        assert not errors
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_late_committing_lower_id_event_is_inserted_in_order_and_deduplicated():
    script = _messages_script().replace("let selectedAfterId=Number(900)||0;", "let selectedAfterId=Number(902)||0;")
    html = """<!doctype html><meta charset='utf-8'><base href='http://qa.local/'>
    <div data-message-list><div class='message-user-row active' data-tg-id='123'><a class='message-user-open' href='/messages?tg_id=123'><strong>Пользователь</strong></a></div></div>
    <input data-message-search><div data-message-chat data-selected-tg-id='123'><div class='chat-message in' data-event-id='900'>Раннее</div><div class='chat-message in' data-event-id='902'>Позднее по ID</div></div>
    <form id='messages-compose-form' action='/users/123/message' method='post'><textarea id='messages-compose-text'></textarea><input type='file' name='media'><div id='messages-compose-status' hidden></div><button id='messages-compose-send'>Отправить</button><button id='messages-compose-retry' hidden>Повторить</button></form><div data-message-dialog-count>1</div>
    <script>
      const nativeTimeout=window.setTimeout.bind(window);window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,Math.min(Number(ms)||0,80),...args);
      window.__chatFeedCalls=0;window.__pollUrls=[];
      window.fetch=async (url,opts={})=>{const u=new URL(String(url),document.baseURI);window.__pollUrls.push(u.href);if(u.pathname.endsWith('/api/panel/messages/feed'))return new Promise(()=>{});if(u.pathname.endsWith('/api/panel/messages/chat-feed')){window.__chatFeedCalls++;const after=Number(u.searchParams.get('after_id')||0);const event={id:901,tg_id:123,direction:'in',message_kind:'message',delivery_status:'received',display_username:'tester',created_at:'2026-10-20 14:00:00',text:'Поздно зафиксированное событие'};return {ok:true,status:200,json:async()=>({ok:true,tg_id:123,events:[event],next_after_id:after})};}throw new Error('Unexpected URL '+u.href);};
    </script>"""
    # Every overlap response deliberately repeats ID 901; the DOM must de-duplicate it.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("FARGOVPN_CHROMIUM", "/usr/bin/chromium"), args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="domcontentloaded")
        page.add_script_tag(content=script)
        page.wait_for_function("document.querySelector('[data-message-chat] [data-event-id=\"901\"]') !== null", timeout=5000)
        ids = page.locator('[data-message-chat] [data-event-id]').evaluate_all("nodes => nodes.map(node => Number(node.dataset.eventId))")
        assert ids == [900, 901, 902]
        page.wait_for_timeout(200)
        assert page.locator('[data-message-chat] [data-event-id="901"]').count() == 1
        assert page.evaluate("window.__pollUrls.some(url => new URL(url).pathname.endsWith('/api/panel/messages/chat-feed') && new URL(url).searchParams.has('_poll_ts'))")
        assert not errors
        browser.close()
