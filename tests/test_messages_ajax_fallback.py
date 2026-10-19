from __future__ import annotations

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def function_source(name: str) -> str:
    source = read("webapp.py")
    match = re.search(
        rf"(?ms)^(?:async )?def {re.escape(name)}\b.*?(?=^@app\.|^def |^async def |\Z)",
        source,
    )
    assert match, f"function not found: {name}"
    return match.group(0)


def test_messages_without_tg_id_selects_latest_dialog_and_renders_composer():
    source = function_source("messages_page")
    assert "participants = user_events.conversation_users(db_path=config.DB_PATH)" in source
    assert "if selected <= 0 and participants:" in source
    assert 'selected = int(participants[0].get("tg_id") or 0)' in source
    assert 'if selected > 0:' in source and 'id="messages-compose-form"' in source


def test_message_post_and_unauthorized_handler_preserve_json_contract_for_fetch():
    source = read("webapp.py")
    unauthorized = function_source("unauthorized")
    route = function_source("message_user")
    assert 'def _request_wants_json_response(request: Request) -> bool:' in source
    assert '_request_wants_json_response(request)' in unauthorized
    assert 'wants_json = _request_wants_json_response(request)' in route
    assert 'status_code=401' in unauthorized


def test_global_ajax_fallback_sends_json_accept_and_has_inline_chat_feedback():
    source = read("static/panel.js")
    assert "const isMessagesForm = form.id === 'messages-compose-form';" in source
    assert "{'Accept':'application/json', 'X-Requested-With':'XMLHttpRequest'}" in source
    assert "showMessageStatus('Сообщение отправлено', true)" in source
    assert "Сервер вернул redirect вместо AJAX-ответа; проверьте обработчик страницы" not in source
    assert "Сервер вернул не JSON-ответ; отправка не подтверждена." in source


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"ok": True, "detail": "Отправлено", "event": {"id": 901, "tg_id": 123, "text": "Привет"}}, "Сообщение отправлено"),
        (401, {"ok": False, "detail": "Сессия истекла. Войдите снова"}, "Сессия истекла. Войдите снова"),
    ],
)
def test_messages_fallback_posts_json_without_navigation(status_code, payload, expected):
    panel_js = read("static/panel.js")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox"],
        )
        page = browser.new_page()
        navigations = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        page.set_content(
            """<!doctype html><meta name='fargovpn-csrf-token' content='csrf'>
            <div data-message-chat></div>
            <form id='messages-compose-form' data-no-navigation='1' method='post' action='/users/123/message'>
              <textarea name='message' id='message'></textarea><input name='media' type='file'>
              <div id='messages-compose-status' class='notice' hidden></div>
              <button type='submit' id='send'>Отправить</button>
            </form>""",
            wait_until="domcontentloaded",
        )
        page.add_script_tag(content=panel_js)
        page.evaluate(
            """([statusCode,payload]) => {
              window.__calls=[];
              window.fetch = async (url, options={}) => {
                window.__calls.push({url:String(url), method:String(options.method||'GET'), headers:options.headers||{}});
                return {ok:statusCode>=200&&statusCode<300,status:statusCode,type:'basic',url:String(url),
                  headers:new Headers({'content-type':'application/json'}),json:async()=>payload};
              };
            }""",
            [status_code, payload],
        )
        page.locator("#message").fill("Проверка fallback")
        before = page.url
        page.locator("#send").click()
        page.wait_for_function(
            "document.getElementById('messages-compose-status').textContent.length > 0",
            timeout=3000,
        )
        assert page.url == before
        assert navigations == []
        assert page.locator("#messages-compose-status").inner_text() == expected
        calls = page.evaluate("window.__calls")
        post = next(item for item in calls if item["method"] == "POST")
        assert post["headers"]["Accept"] == "application/json"
        assert post["headers"]["X-Requested-With"] == "XMLHttpRequest"
        if status_code == 200:
            assert page.locator("#message").input_value() == ""
            assert page.locator('[data-message-chat] [data-event-id="901"]').count() == 1
        browser.close()
