"""Render the actual cabinet functions without OS services or production dependencies."""
import ast
from contextlib import contextmanager
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
        panel_from_timestamp=lambda _value: __import__('datetime').datetime.fromtimestamp(0),
        billing=SimpleNamespace(
            telegram_identity_label=lambda tg_id, username, display_name:
                '@' + username if username else display_name or f'ID {tg_id}',
            subscription_label=lambda user, now_ms: 'Без подписки',
            subscription_status_tone=lambda label: (
                'bad' if 'отключён' in str(label).casefold()
                else 'warn' if any(marker in str(label).casefold() for marker in (
                    'нет профиля', 'не зарегистрирован', 'без подписки', 'истек'
                ))
                else 'good'
            ),
        ),
        user_subscription_label=lambda user, now_ms: 'Без подписки',
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


def test_cabinet_uses_full_name_when_telegram_username_is_missing():
    ns = cabinet_functions()
    markup = ns['_cabinet_markup']('', {'tg_id': 2, 'display_name': 'Иван Петров'}, '')
    assert '<h1>Иван Петров</h1>' in markup
    assert '<h1>@ID 2</h1>' not in markup


def test_cabinet_prefers_telegram_username_to_numeric_id():
    ns = cabinet_functions()
    markup = ns['_cabinet_markup']('', {'tg_id': 2, 'username': 'ivan_petrov'}, '')
    assert '<h1>@ivan_petrov</h1>' in markup
    assert '<h1>@ID 2</h1>' not in markup


def test_family_cabinet_shows_owner_username_separately_from_member_identity():
    ns = cabinet_functions()
    ns['user_subscription_label'] = lambda _user, _now: 'Семейная · подписка активна'
    ns['billing'].has_subscription = lambda _user: True
    markup = ns['_cabinet_markup']('', {
        'tg_id': 20,
        'username': 'member',
        'uuid': 'member-uuid',
        'expiry_time': 2_000_000_000_000,
        'family_subscription': True,
        'family_owner_tg_id': 10,
        'family_owner_username': 'owner',
    }, '')
    assert '<h1>@member<br><small>Семейная подписка пользователя @owner</small></h1>' in markup


def test_user_subscription_badges_use_yellow_for_no_subscription():
    tree = ast.parse((ROOT / 'app/webapp.py').read_text(encoding='utf-8'))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == 'user_subscription_badge'
    )
    namespace = {
        'html': html,
        'billing': SimpleNamespace(subscription_status_tone=lambda label: (
            'warn' if any(marker in str(label).casefold() for marker in ('без подписки', 'не зарегистрирован', 'истек'))
            else 'bad' if any(marker in str(label).casefold() for marker in ('заблокирован', 'отключён'))
            else 'good'
        )),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'user_subscription_badge', 'exec'), namespace)

    assert 'badge warn' in namespace['user_subscription_badge']('Без подписки')
    assert 'badge warn' in namespace['user_subscription_badge']('Семейная · без подписки')
    assert 'badge warn' in namespace['user_subscription_badge']('Подписка истекла')
    assert 'badge good' in namespace['user_subscription_badge']('Подписка активна')


def test_family_cabinet_keeps_member_identity_and_owner_subscription():
    tree = ast.parse((ROOT / 'app/cabinet_service.py').read_text(encoding='utf-8'))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == 'resolve_user_for_cabinet'
    )
    members = {
        10: {'tg_id': 10, 'username': 'owner', 'display_name': 'Owner', 'sub_id': 'owner-sub'},
        20: {'tg_id': 20, 'username': '', 'display_name': 'Иван Петров'},
    }

    class Result:
        def fetchone(self):
            return (10,)

    class Connection:
        def execute(self, *_args):
            return Result()

    @contextmanager
    def connect(_path):
        yield Connection()

    namespace = {
        'Path': Path,
        'Any': object,
        '_connect': connect,
        '_latest_username': lambda *_args: 'membernick',
        'get_user': lambda tg_id, _path: members.get(tg_id),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'resolve_user_for_cabinet', 'exec'), namespace)
    user = namespace['resolve_user_for_cabinet'](20, 'unused')

    assert user['tg_id'] == 20
    assert user['username'] == 'membernick'
    assert user['display_name'] == 'Иван Петров'
    assert user['sub_id'] == 'owner-sub'
    assert user['family_subscription'] is True
    assert user['family_owner_tg_id'] == 10
    assert user['family_owner_username'] == 'owner'
    assert user['family_owner_display_name'] == 'Owner'
    assert user['family_owner_has_subscription'] is True
    assert user['family_owner_expiry_time'] == 0
    assert user['family_owner_enable'] is True


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
