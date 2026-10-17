"""Render the actual cabinet functions without OS services or production dependencies."""
import ast
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from html.parser import HTMLParser

import pytest

ROOT = Path(__file__).resolve().parents[1]

class Tags(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tags = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def cabinet_functions(sub_url='https://example.test/sub'):
    tree = ast.parse((ROOT / 'app/webapp.py').read_text(encoding='utf-8'))
    names = {'_cabinet_markup', 'cabinet_connection_page'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.decorator_list = []
    ns = dict(Any=object, Request=object, html=html, json=json,
        time=SimpleNamespace(time=lambda: 0),
        config=SimpleNamespace(SERVICE_NAME='Test', DB_PATH='unused', CABINET_PATH='/cabinet'),
        update_manager=SimpleNamespace(current_version=lambda: '5.1.20'),
        public_path=lambda path: '/private123' + path,
        _cabinet_bot_username=lambda: 'testbot',
        referral_codes=SimpleNamespace(ensure_referral_code=lambda *args: 'TEST'),
        bytes_to_gb=lambda value: 0, remaining_label=lambda *args: '0',
        _cabinet_session_user=lambda *args: {'tg_id': 1, 'sub_id': 'test'},
        current_subscription_url_sync=lambda *args, **kwargs: sub_url,
        HTMLResponse=lambda body, **kwargs: SimpleNamespace(body=body, **kwargs))
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'cabinet-functions', 'exec'), ns)
    return ns


def test_cabinet_stylesheet_and_telegram_script_are_separate_elements():
    ns = cabinet_functions()
    markup = ns['_cabinet_markup']('tester', {'tg_id': 1}, 'https://example.test/sub')
    tags = Tags(markup).tags
    assert ('link', {'rel': 'stylesheet', 'href': '/private123/static/panel.css?v=5.1.20'}) in tags
    assert ('script', {'src': 'https://telegram.org/js/telegram-web-app.js'}) in tags


def test_cabinet_interactive_script_parses(tmp_path):
    node = os.environ.get('FARGOVPN_NODE') or shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to parse browser JavaScript')
    ns = cabinet_functions()
    markup = ns['_cabinet_markup']('tester', {'tg_id': 1}, 'https://example.test/sub')
    scripts = [script for script in re.findall(r'<script[^>]*>(.*?)</script>', markup, re.S) if script.strip()]
    assert len(scripts) == 1
    path = tmp_path / 'cabinet.js'
    path.write_text(scripts[0], encoding='utf-8')
    result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('sub_url', ['https://example.test/sub', ''])
def test_connection_page_preserves_public_prefix(sub_url):
    ns = cabinet_functions(sub_url)
    response = ns['cabinet_connection_page'](object())
    tags = Tags(response.body).tags
    assert ('link', {'rel': 'stylesheet', 'href': '/private123/static/panel.css?v=5.1.20'}) in tags
    anchors = [attrs['href'] for tag, attrs in tags if tag == 'a']
    assert '/private123/cabinet' in anchors
    assert '/cabinet' not in anchors
