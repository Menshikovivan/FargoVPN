"""Browser checks of real login JS with stubbed credentials, never native Face ID."""
import ast
import html
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parents[1] / "app"
CHROME = os.environ.get("FARGOVPN_CHROMIUM") or os.environ.get("FARGOVPN_TEST_CHROMIUM")


def login_html():
    nodes = [n for n in ast.parse((APP / "webapp.py").read_text(encoding="utf-8")).body
             if isinstance(n, ast.FunctionDef) and n.name in {"login_page", "_brand_mark"}]
    for node in nodes:
        node.decorator_list = []
        for arg in node.args.args:
            arg.annotation = None
    namespace = dict(html=html, json=json, config=SimpleNamespace(SERVICE_NAME="FargoVPN"),
                     public_path=lambda path: "/panel" + path,
                     update_manager=SimpleNamespace(current_version=lambda: (APP / "VERSION").read_text().strip()),
                     page=lambda request, title, body: body)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "login_page", "exec"), namespace)
    return namespace["login_page"](None)


@pytest.fixture
def browser():
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=CHROME or driver.chromium.executable_path,
                                         headless=True, args=['--no-sandbox'])
        yield browser
        browser.close()


def open_login(browser, *, standalone=False, cancel=False, suppress=False, verify_ok=True):
    context = browser.new_context()
    context.add_init_script("""(settings => {
      window.steps=[];
      Object.defineProperty(navigator,'standalone',{value:settings.standalone});
      Object.defineProperty(navigator,'credentials',{value:{get:async()=>{
        steps.push('assertion');
        if(settings.cancel)throw new DOMException('Cancelled','NotAllowedError');
        return {id:'test',rawId:new Uint8Array([1]).buffer,type:'public-key',
          response:{clientDataJSON:new Uint8Array([2]).buffer,signature:new Uint8Array([3]).buffer}};
      }}});
      window.fetch=async(url,options)=>{
        const verify=String(url).endsWith('/verify');steps.push(verify?'verify':'options');
        if(verify){window.sentCredential=JSON.parse(options.body).credential;sessionStorage.setItem('verifiedSteps',JSON.stringify(steps));sessionStorage.setItem('verifiedCredential',JSON.stringify(window.sentCredential));}
        return {ok:!verify||settings.verify_ok,json:async()=>verify?
          {ok:settings.verify_ok,redirect:'/done'}:{ok:true,options:{challenge:'AQ'}}};
      };
    })(""" + json.dumps(dict(standalone=standalone, cancel=cancel, verify_ok=verify_ok)) + ");")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://panel.example.test/**", lambda route: route.fulfill(
        content_type="text/html", body=login_html() if "/login" in route.request.url else "Done"))
    page.goto("https://panel.example.test/panel/login" + ("?auto_passkey=0" if suppress else ""))
    return context, page, errors


@pytest.mark.parametrize("standalone,suppress", [(False, False), (True, True)])
def test_no_automatic_prompt_in_browser_or_after_logout(browser, standalone, suppress):
    context, page, errors = open_login(browser, standalone=standalone, suppress=suppress)
    page.wait_for_timeout(100)
    assert page.evaluate("steps") == []
    assert page.locator('#login-pass').is_visible()
    assert page.locator('#passkey-login').is_visible()
    assert not errors
    context.close()


def test_pwa_cancel_keeps_password_and_allows_manual_retry_without_auto_loop(browser):
    context, page, errors = open_login(browser, standalone=True, cancel=True)
    page.wait_for_function("steps.length===2 && !document.querySelector('#passkey-login').disabled")
    assert page.evaluate("steps") == ['options', 'assertion']
    assert page.url.endswith('/login')
    assert page.locator('#login-pass').is_visible()
    assert 'Face ID / Touch ID' in page.locator('#login-status').inner_text()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(100)
    assert page.evaluate("steps.length") == 2
    page.locator('#passkey-login').click()
    page.wait_for_function("steps.length===4 && !document.querySelector('#passkey-login').disabled")
    assert page.evaluate("steps") == ['options', 'assertion', 'options', 'assertion']
    assert not errors
    context.close()


@pytest.mark.parametrize('verify_ok', [False, True])
def test_assertion_is_verified_before_any_success_redirect(browser, verify_ok):
    context, page, errors = open_login(browser, standalone=True, verify_ok=verify_ok)
    if verify_ok:
        page.wait_for_url('**/done')
        assert page.evaluate("JSON.parse(sessionStorage.getItem('verifiedSteps'))") == ['options', 'assertion', 'verify']
        assert page.evaluate("JSON.parse(sessionStorage.getItem('verifiedCredential')).id") == 'test'
    else:
        page.wait_for_function("steps.length===3 && !document.querySelector('#passkey-login').disabled")
        assert page.url.endswith('/login')
        assert page.evaluate("steps") == ['options', 'assertion', 'verify']
        assert page.evaluate("sentCredential.id") == 'test'
    assert not errors
    context.close()

