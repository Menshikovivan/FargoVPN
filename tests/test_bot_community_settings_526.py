"""Focused checks for the configurable public bot community link."""
import ast
import html
from pathlib import Path
import re
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def load(names, config):
    tree = ast.parse((APP / "webapp.py").read_text(encoding="utf-8"))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    env = {
        "Any": object, "html": html, "re": re, "urlsplit": urlsplit,
        "HTTPException": HTTPException, "config": config,
        "BOT_COMMUNITY_URL_DEFAULT": "https://t.me/fargovpn",
        "_config_text": lambda name, default="": html.escape(str(getattr(config, name, default) or ""), quote=True),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "webapp.py", "exec"), env)
    return env


def test_community_setting_displays_default_and_explicit_blank():
    default = load({"_bot_community_url_field"}, NS())
    markup = default["_bot_community_url_field"]()
    assert 'name="bot_community_url"' in markup
    assert 'value="https://t.me/fargovpn"' in markup
    assert "Пустое поле скрывает кнопку" in markup
    blank = load({"_bot_community_url_field"}, NS(BOT_COMMUNITY_URL=""))
    assert 'value=""' in blank["_bot_community_url_field"]()


def test_community_url_form_preserves_missing_value_and_saves_valid_or_blank():
    env = load({"_validate_bot_community_url", "_bot_community_url_from_form"}, NS(BOT_COMMUNITY_URL="https://t.me/savedname"))
    read = env["_bot_community_url_from_form"]
    assert read({}) == "https://t.me/savedname"
    assert read({"bot_community_url": "https://t.me/NewName/"}) == "https://t.me/NewName"
    assert read({"bot_community_url": "https://t.me/NewName?start=group#info"}) == "https://t.me/NewName?start=group#info"
    assert read({"bot_community_url": "https://t.me/+AbCdEf_123456"}) == "https://t.me/+AbCdEf_123456"
    assert read({"bot_community_url": "https://t.me/joinchat/AbCdEf_123456"}) == "https://t.me/joinchat/AbCdEf_123456"
    assert read({"bot_community_url": ""}) == ""


@pytest.mark.parametrize("bad", [
    "http://t.me/name1", "https://telegram.me/name1", "https://t.me/+short",
    "https://t.me/joinchat/short", "https://t.me/name1/extra",
    "https://user@t.me/name1", "https://t.me:443/name1", "https://t.me/name1/extra",
    "https://t.me/a", "https://t.me/name name", "javascript:alert(1)",
    "https://t.me/name1\n", "https://t.me/name1?x=bad\x00value",
])
def test_invalid_community_links_rejected(bad):
    reader = load({"_validate_bot_community_url", "_bot_community_url_from_form"}, NS())["_bot_community_url_from_form"]
    with pytest.raises(HTTPException) as error:
        reader({"bot_community_url": bad})
    assert error.value.status_code == 400


def test_settings_save_payload_includes_validated_community_value():
    tree = ast.parse((APP / "webapp.py").read_text(encoding="utf-8"))
    save = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_save_settings")
    assignments = [n.value for n in ast.walk(save) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "bot_community_url" for t in n.targets)]
    assert len(assignments) == 1 and ast.unparse(assignments[0]) == "_bot_community_url_from_form(form)"
    payloads = [n.value for n in ast.walk(save) if isinstance(n, ast.AnnAssign)
                and isinstance(n.target, ast.Name) and n.target.id == "values"]
    assert len(payloads) == 1
    entry = next(item for item in payloads[0].keys if isinstance(item, ast.Constant) and item.value == "BOT_COMMUNITY_URL")
    value = payloads[0].values[list(payloads[0].keys).index(entry)]
    assert ast.unparse(value) == "bot_community_url"
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "save_config_values"
               and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "values" for n in ast.walk(save))

