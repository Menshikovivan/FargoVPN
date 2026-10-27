"""Exercise real whitelist renderers, callbacks and config persistence offline."""
import ast
import html
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
spec = importlib.util.spec_from_file_location("whitelist_under_test", APP / "whitelist_help.py")
help = importlib.util.module_from_spec(spec)
spec.loader.exec_module(help)


def load(file, names, env):
    nodes = [n for n in ast.parse((APP / file).read_text(encoding="utf-8")).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
        node.returns = None
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            arg.annotation = None
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), env)
    return env


def immediate(coro):
    """All IO is AsyncMock; execute without a Windows selector/socket loop."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise AssertionError("Unexpected asynchronous boundary")


class Keyboard:
    def __init__(self):
        self.buttons = []

    def button(self, **kw):
        self.buttons.append(kw)

    def adjust(self, *args):
        return self

    def as_markup(self):
        return self.buttons


def test_menu_available_without_paid_subscription():
    env = load("main.py", {"get_user_menu", "get_whitelist_menu"}, {
        "has_active_subscription": lambda _: False,
        "InlineKeyboardBuilder": Keyboard, "config": NS(ADMIN_IDS=[]),
        "whitelist_help": help,
    })
    assert any(b.get("callback_data") == "menu:whitelist" for b in env["get_user_menu"](123))
    buttons = env["get_whitelist_menu"]()
    assert {b.get("callback_data") for b in buttons} >= {
        "whitelist:android", "whitelist:ios", "whitelist:windows", "menu:home",
    }


@pytest.mark.parametrize("platform", ["android", "ios", "windows", "unknown"])
def test_platform_callback_acknowledges_and_clears_purchase_state(platform):
    env = load("main.py", {"whitelist_platform_callbacks", "get_whitelist_menu"}, {
        "safe_callback_answer": AsyncMock(), "InlineKeyboardBuilder": Keyboard,
        "config": NS(), "whitelist_help": help,
        "cabinet_service": NS(public_web_base_url=lambda: "https://vpn.example.org/panel"),
    })
    answer = AsyncMock()
    call = NS(data="whitelist:" + platform, message=NS(answer=answer), from_user=NS(id=123))
    state = NS(clear=AsyncMock())
    immediate(env["whitelist_platform_callbacks"](call, state))
    env["safe_callback_answer"].assert_awaited_once_with(call)
    state.clear.assert_awaited_once()
    answer.assert_awaited_once()
    text = answer.call_args.args[0]
    assert "бесплатные сторонние подборки" in text
    if platform != "unknown":
        assert help.PLATFORMS[platform][0] in text
        assert "автообнов" in text.lower()


@pytest.mark.parametrize("platform,install", [
    ("android", "APK"), ("ios", "App Store"), ("windows", "администратора"),
])
def test_platform_uses_own_configured_subscription(platform, install):
    key = help.PLATFORMS[platform][1]
    config = NS(**{key: "https://example.org/custom?a=1&b=2"})
    text = help.instruction_text(config, platform)
    assert "https://example.org/custom?a=1&amp;b=2" in text
    assert install in text
    assert "не принадлежат FargoVPN" in text
    assert "не гарантируется" in text
    assert "Happ" in text and "Karing" not in text


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "file:///etc/passwd", "https://user:password@example.org/a",
    "https://example.org/a\nb", "https://example.org/a b", "https://example.org/a\x00b",
    "https://example.org:bad/a", "https://[bad/a", "https://example.org\\bad",
    "//example.org/a", "https:///a", "https://example.org/" + "a" * 1000,
])
def test_invalid_links_rejected_and_hidden_in_bot(bad):
    with pytest.raises(ValueError):
        help.safe_url(bad)
    assert help.link(NS(WHITELIST_ANDROID_URL=bad), "WHITELIST_ANDROID_URL") == ""


def test_old_config_defaults_and_intentional_blanks():
    for key, _, _, default in help.FIELDS:
        assert help.link(NS(), key) == default
        assert help.link(NS(**{key: ""}), key) == ""
    config = NS(WHITELIST_ANDROID_URL="", WHITELIST_BACKUP_URL="")
    assert "<code>" not in help.instruction_text(config, "android")
    assert "временно недоступны" in help.instruction_text(config, "android")


def test_maximum_urls_remain_within_telegram_message_limit_after_entities():
    prefix = "https://example.org/"
    url = prefix + "&" * (1000 - len(prefix))
    config = NS(WHITELIST_ANDROID_URL=url, WHITELIST_BACKUP_URL=url)
    text = help.instruction_text(config, "android")
    visible = html.unescape(re.sub(r"<[^>]*>", "", text))
    assert len(visible.encode("utf-16-le")) // 2 <= 4096
    assert "<code>" + html.escape(url) + "</code>" in text


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def admin(config):
    return load("webapp.py", {"_whitelist_form_values", "_whitelist_settings_fields"}, {
        "config": config, "whitelist_help": help, "HTTPException": HTTPException,
        "html": html, "_config_text": lambda key, default: html.escape(str(getattr(config, key, default)), quote=True),
    })


def test_admin_missing_fields_preserved_blank_is_not_default():
    config = NS(WHITELIST_ANDROID_URL="https://example.org/saved")
    env = admin(config)
    values = env["_whitelist_form_values"]({"whitelist_ios_url": ""})
    assert values["WHITELIST_ANDROID_URL"] == config.WHITELIST_ANDROID_URL
    assert values["WHITELIST_IOS_URL"] == ""
    assert values["WHITELIST_WINDOWS_URL"] == help.DEFAULTS["WHITELIST_WINDOWS_URL"]
    with pytest.raises(HTTPException) as error:
        env["_whitelist_form_values"]({"whitelist_backup_url": "javascript:alert(1)"})
    assert error.value.status_code == 400


def test_admin_renders_all_fields_without_attribute_injection():
    malicious = 'https://example.org/" autofocus onfocus="alert(1)'
    body = admin(NS(WHITELIST_ANDROID_URL=malicious))["_whitelist_settings_fields"]()
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inputs = []

        def handle_starttag(self, tag, attrs):
            if tag == "input":
                self.inputs.append(dict(attrs))
    parser = Inputs()
    parser.feed(body)
    assert {i["name"] for i in parser.inputs} == {f[1] for f in help.FIELDS}
    assert parser.inputs[0]["value"] == malicious
    assert not any("onfocus" in i or "autofocus" in i for i in parser.inputs)


def test_settings_validation_happens_before_persistence():
    tree = ast.parse((APP / "webapp.py").read_text(encoding="utf-8"))
    save = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_save_settings")
    calls = [n for n in ast.walk(save) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert next(n.lineno for n in calls if n.func.id == "_whitelist_form_values") < next(
        n.lineno for n in calls if n.func.id == "save_config_values")


def test_real_config_writer_roundtrip_preserves_existing_values(tmp_path):
    path = tmp_path / "config.py"
    path.write_text("BOT_TOKEN = 'synthetic-unchanged'\nSERVICE_NAME = 'FargoVPN'\n", encoding="utf-8")
    config = NS()
    env = load("webapp.py", {"replace_assignment", "_save_config_values_locked"}, {
        "config": config, "CONFIG_PATH": path, "time": time, "shutil": shutil,
        "os": os, "re": re, "LOGGER": NS(warning=lambda *a: None),
        "invalidate_snapshot_cache": lambda: None,
        "update_manager": NS(invalidate_update_cache=lambda: None),
    })
    values = admin(NS())["_whitelist_form_values"]({
        "whitelist_android_url": "https://example.org/new?a=1&b=2", "whitelist_ios_url": "",
    })
    env["_save_config_values_locked"](values)
    saved = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), saved)
    assert saved["BOT_TOKEN"] == "synthetic-unchanged"
    assert saved["SERVICE_NAME"] == "FargoVPN"
    assert all(saved[k] == v for k, v in values.items())
    assert help.link(config, "WHITELIST_ANDROID_URL") == values["WHITELIST_ANDROID_URL"]
    assert help.link(config, "WHITELIST_IOS_URL") == ""


@pytest.mark.parametrize("platform,app", [
    ("android", "happ"), ("ios", "happ"), ("windows", "happ"), ("ios", "incy"),
])
@pytest.mark.parametrize("backup", [False, True])
def test_native_launcher_buttons_resolve_current_subscription(platform, app, backup):
    config = NS(**{key: "https://new.example.org/" + name for key, name, _, _ in help.FIELDS})
    env = load("main.py", {"get_whitelist_menu"}, {
        "InlineKeyboardBuilder": Keyboard, "config": config, "whitelist_help": help,
        "cabinet_service": NS(public_web_base_url=lambda: "https://vpn.example.org/panel/"),
    })
    buttons = env["get_whitelist_menu"](platform=platform)
    expected = f"https://vpn.example.org/panel/open-whitelist/{platform}/{app}" + ("?backup=1" if backup else "")
    assert any(b.get("url") == expected for b in buttons)
    key = "WHITELIST_BACKUP_URL" if backup else (
        "WHITELIST_INCY_SUB_URL" if app == "incy" else help.PLATFORMS[platform][1])
    assert help.subscription_url(config, platform, app, backup) == getattr(config, key)
    assert not any("token=" in b.get("url", "") or "tg_id=" in b.get("url", "") for b in buttons)
    if platform != "ios":
        assert not any("incy" in b.get("url", "") for b in buttons)


@pytest.mark.parametrize("platform,app", [
    ("android", "incy"), ("windows", "incy"), ("unknown", "happ"), ("ios", "unknown"),
])
def test_impossible_platform_app_combinations_rejected(platform, app):
    with pytest.raises(ValueError):
        help.subscription_url(NS(), platform, app)


def test_default_happ_json_and_incy_universal_formats_distinct():
    assert help.subscription_url(NS(), "android", "happ").endswith(".json")
    assert "/Export/Happ/" in help.subscription_url(NS(), "ios", "happ")
    assert help.subscription_url(NS(), "ios", "incy").endswith(".txt")


def test_missing_public_domain_falls_back_to_manual_import():
    env = load("main.py", {"get_whitelist_menu"}, {
        "InlineKeyboardBuilder": Keyboard, "config": NS(), "whitelist_help": help,
        "cabinet_service": NS(public_web_base_url=lambda: ""),
    })
    buttons = env["get_whitelist_menu"](platform="android")
    assert not any("/open-whitelist/" in b.get("url", "") for b in buttons)
    assert "<code>https://" in help.instruction_text(NS(), "android")


def test_cleared_or_invalid_subscriptions_hide_native_import_buttons():
    config = NS(WHITELIST_ANDROID_URL="", WHITELIST_BACKUP_URL="javascript:alert(1)")
    env = load("main.py", {"get_whitelist_menu"}, {
        "InlineKeyboardBuilder": Keyboard, "config": config, "whitelist_help": help,
        "cabinet_service": NS(public_web_base_url=lambda: "https://vpn.example.org"),
    })
    assert not any("/open-whitelist/" in b.get("url", "") for b in env["get_whitelist_menu"](platform="android"))


def launch_env(config):
    def HTMLResponse(body, headers):
        # The HTML response boundary is intentionally synthetic; ASGI routing
        # and native app installation require the full integration environment.
        return NS(body=body.encode("utf-8"), headers={k.lower(): v for k, v in headers.items()}, status_code=200)
    return load("webapp.py", {"open_whitelist_app"}, {
        "whitelist_help": help, "config": config, "json": json, "html": html,
        "HTMLResponse": HTMLResponse, "HTTPException": HTTPException,
    })


@pytest.mark.parametrize("platform,app", [
    ("android", "happ"), ("ios", "happ"), ("windows", "happ"), ("ios", "incy"),
])
@pytest.mark.parametrize("backup", [0, 1])
def test_user_flow_platform_to_https_launcher_to_correct_native_import(platform, app, backup):
    """Execute actual bot handlers and launcher, mocking only Telegram boundaries."""
    config = NS(ADMIN_IDS=[], **{key: "https://edited.example.org/" + name for key, name, _, _ in help.FIELDS})
    env = load("main.py", {"get_user_menu", "dynamic_user_menu", "whitelist_platform_callbacks", "get_whitelist_menu"}, {
        "safe_callback_answer": AsyncMock(), "InlineKeyboardBuilder": Keyboard,
        "config": config, "whitelist_help": help,
        "cabinet_service": NS(public_web_base_url=lambda: "https://vpn.example.org/panel"),
        "has_active_subscription": lambda _: False,
    })
    main_menu = env["get_user_menu"](123)
    menu_action = next(b["callback_data"] for b in main_menu if b.get("callback_data") == "menu:whitelist")
    overview_answer = AsyncMock()
    overview_call = NS(data=menu_action, message=NS(answer=overview_answer), from_user=NS(id=123))
    immediate(env["dynamic_user_menu"](overview_call, NS(clear=AsyncMock())))
    assert any(b.get("callback_data") == "whitelist:" + platform
               for b in overview_answer.call_args.kwargs["reply_markup"])
    answer = AsyncMock()
    call = NS(data="whitelist:" + platform, message=NS(answer=answer), from_user=NS(id=123))
    immediate(env["whitelist_platform_callbacks"](call, NS(clear=AsyncMock())))
    suffix = f"/open-whitelist/{platform}/{app}" + ("?backup=1" if backup else "")
    buttons = answer.call_args.kwargs["reply_markup"]
    url = next(b["url"] for b in buttons if b.get("url", "").endswith(suffix))
    parts = urlsplit(url)
    response = launch_env(config)["open_whitelist_app"](
        None, *parts.path.split("/")[-2:], backup=int(parse_qs(parts.query).get("backup", ["0"])[0]))
    assert response.status_code == 200
    body = response.body.decode()
    sub_url = help.subscription_url(config, platform, app, backup=bool(backup))
    assert html.escape(app + "://add/" + sub_url, quote=True) in body
    assert html.escape(sub_url, quote=True) in body
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "бесплатные сторонние подборки" in body
    assert "tg_id" not in body and "access_token" not in body
    assert "Karing" not in body


@pytest.mark.parametrize("platform,app,backup", [
    ("android", "incy", 0), ("windows", "incy", 1), ("unknown", "happ", 0),
    ("ios", "unknown", 0), ("ios", "happ", 2), ("ios", "happ", -1),
])
def test_public_launcher_rejects_invalid_route_options(platform, app, backup):
    with pytest.raises(HTTPException) as error:
        launch_env(NS())["open_whitelist_app"](None, platform, app, backup=backup)
    assert error.value.status_code == 404


@pytest.mark.parametrize("key,app", [("WHITELIST_IOS_URL", "happ"), ("WHITELIST_INCY_SUB_URL", "incy")])
def test_disabled_public_collection_returns_not_found(key, app):
    with pytest.raises(HTTPException) as error:
        launch_env(NS(**{key: ""}))["open_whitelist_app"](None, "ios", app)
    assert error.value.status_code == 404


def test_public_launcher_escapes_script_and_html_delimiters():
    malicious = 'https://example.org/</script><script>alert(1)</script>?x="&y=1'
    response = launch_env(NS(WHITELIST_IOS_URL=malicious))["open_whitelist_app"](None, "ios", "happ")
    body = response.body.decode()
    assert body.count("<script>") == 1 and body.count("</script>") == 1
    script = re.search(r"<script>const u=(.*?);setTimeout", body).group(1)
    assert "<" not in script and ">" not in script and "&" not in script
    assert json.loads(script) == "happ://add/" + malicious
    assert html.escape(malicious, quote=True) in body


@pytest.mark.parametrize("path,prefix,policy", [
    ("/open-whitelist/ios/happ", "", "no-referrer"),
    ("/panel/open-whitelist/ios/incy", "/panel", "no-referrer"),
    ("/settings", "", "same-origin"),
    ("/panel/settings", "/panel", "same-origin"),
])
def test_actual_response_middleware_preserves_launcher_privacy_and_other_routes(path, prefix, policy):
    env = load("webapp.py", {"response_security_headers"}, {
        "time": time, "re": re, "urlsplit": urlsplit, "public_prefix": lambda: prefix,
        "LOGGER": NS(warning=lambda *a: None),
        "update_manager": NS(current_version=lambda: "5.2.5"),
        "PANEL_CONTENT_SECURITY_POLICY": "default-src 'self'",
    })
    request = NS(method="GET", headers={}, url=NS(path=path, scheme="https"))
    response = NS(status_code=200, headers={})
    result = immediate(env["response_security_headers"](request, AsyncMock(return_value=response)))
    assert result.headers["Referrer-Policy"] == policy
    assert "no-store" in result.headers["Cache-Control"]
    assert result.headers["X-VPN-Platform-Version"] == "5.2.5"


def test_iphone_without_public_domain_shows_correct_incy_manual_fallback():
    config = NS(WHITELIST_INCY_SUB_URL="https://example.org/incy.txt", WHITELIST_IOS_URL="",
                WHITELIST_BACKUP_URL="")
    text = help.instruction_text(config, "ios", auto_import=False)
    assert "https://example.org/incy.txt" in text
    assert "Incy" in text
    assert "Ссылки временно недоступны" not in text
    assert "Открыть в Happ» ниже" not in text
    assert text.count("<code>") == 1


def test_iphone_without_public_domain_can_manually_import_happ_if_incy_disabled():
    config = NS(WHITELIST_INCY_SUB_URL="", WHITELIST_IOS_URL="https://example.org/happ.json",
                WHITELIST_BACKUP_URL="")
    text = help.instruction_text(config, "ios", auto_import=False)
    assert "https://example.org/happ.json" in text
    assert "Happ" in text
    assert "Ссылки временно недоступны" not in text
    assert text.count("<code>") == 1
