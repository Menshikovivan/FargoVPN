from __future__ import annotations

from pathlib import Path

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
    source = _messages_page_source()
    start = source.index("scripts = f'''<script>(function(){{")
    end = source.index("</script>'''", start)
    script = source[start:end].split("<script>", 1)[1]
    script = script.replace("{{", "{").replace("}}", "}")
    return (
        script.replace("__MESSAGES_PATH__", '"/messages"')
        .replace("__MESSAGES_FEED_PATH__", '"/api/panel/messages/feed"')
        .replace("__MESSAGES_CHAT_FEED_PATH__", '"/api/panel/messages/chat-feed"')
        .replace("{selected}", "123")
        .replace("{direction}", "all")
        .replace("{message_kind}", "all")
        .replace("{global_last_id}", "1000")
        .replace("{selected_chat_last_id}", "900")
    )


def test_selected_chat_has_an_independent_authenticated_incremental_endpoint():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/api/panel/messages/chat-feed")')
    end = source.index("\n\nAPP_LOG_DEFAULT", start)
    endpoint = source[start:end]
    assert "require_auth(request)" in endpoint
    assert "user_events.recent_events(" in endpoint
    assert "after_id=after_id" in endpoint
    assert '"next_after_id": next_after_id' in endpoint
    assert "limit=500" in endpoint
    assert '"events": serialized' in endpoint


def test_page_uses_separate_selected_chat_cursor_and_isolates_poll_errors():
    page = _messages_page_source()
    assert "selected_chat_last_id = (" in page
    assert 'messages_chat_feed_path_js = json.dumps(public_path("/api/panel/messages/chat-feed"))' in page
    assert "let selectedAfterId=Number({selected_chat_last_id})||0;" in page
    assert "chatUrl.searchParams.set('after_id',String(selectedAfterId));" in page
    assert "[FargoVPN] messages dialog feed" in page
    assert "[FargoVPN] messages chat feed" in page


def test_sender_row_uses_full_row_click_target_but_keeps_telegram_link_above_it():
    css = (APP / "static/panel.css").read_text(encoding="utf-8")
    assert ".message-user-open::after{content:\"\";position:absolute;inset:0;z-index:1" in css
    assert ".message-user-telegram{position:relative;z-index:2}" in css
    page = _messages_page_source()
    assert 'class="message-user-open" href="%s"' in page
    assert 'class="message-user-telegram"' in page


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_incoming_message_is_inserted_without_reload_even_if_global_feed_fails():
    script = _messages_script()
    panel_css = (APP / "static/panel.css").read_text(encoding="utf-8")
    chat_css = panel_css[panel_css.index(".messages-toolbar{margin-bottom:10px}"):panel_css.index("/* Monitoring, updates, jobs, settings */")]
    html = """<!doctype html><meta charset='utf-8'><base href='http://qa.local/'>
    <style>""" + chat_css + """</style>
    <div data-message-list><div class='message-user-row active' data-tg-id='123'>
      <a class='message-user-open' href='/messages?tg_id=123'><span class='message-user-main'><strong>Тестовый пользователь</strong><span>#123</span></span></a>
      <a class='message-user-telegram' href='https://t.me/tester' target='_blank'>Telegram ↗</a>
      <small>1 входящее · 1 исходящее</small></div></div>
    <input data-message-search><div data-message-chat data-selected-tg-id='123'><div class='chat-message in' data-event-id='900'>Старое сообщение</div></div>
    <form id='messages-compose-form' action='/users/123/message' method='post'><textarea id='messages-compose-text'></textarea><input type='file' name='media'><div id='messages-compose-status' hidden></div><button id='messages-compose-send'>Отправить</button><button id='messages-compose-retry' hidden>Повторить</button></form><div data-message-dialog-count>1</div>
    <script>
      const nativeTimeout=window.setTimeout.bind(window);
      window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,Math.min(Number(ms)||0,80),...args);
      window.__chatFeedCalls=0;
      window.fetch=async (url,opts={})=>{
        const u=new URL(String(url),document.baseURI);
        if(u.pathname.endsWith('/api/panel/messages/feed')) return {ok:false,status:503,json:async()=>({detail:'dialog feed offline'})};
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
            executable_path="/usr/bin/chromium",
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
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("document.querySelector('.message-user-open').addEventListener('click', e => { window.__internalChatClicked = true; e.preventDefault(); })")
        box = page.locator(".message-user-row small").bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert page.evaluate("window.__internalChatClicked === true")
        browser.close()
