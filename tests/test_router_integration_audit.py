import ast
from pathlib import Path
from unittest.mock import patch
import nginx_panel_guard as guard


def host(name, listen, extra=''):
    return f'server {{\n listen {listen};\n server_name {name};\n {extra}\n}}\n'


def test_configured_host_beats_legacy_path_heuristic():
    source = host('cdn.test', 'unix:/dev/shm/nginx-http.sock ssl proxy_protocol', 'location ^~ /Stream-One-Path/ { return 404; }')
    source += host('panel.test', '443 ssl')
    with patch.object(guard, 'cfg', side_effect=lambda name, default=None: {'WEB_DOMAIN': 'panel.test', 'WEB_PUBLIC_PREFIX': '/private123'}.get(name, default)):
        result = guard._insert_into_named_https_server(source, '# AUDIT INSERT')
        selected = [server for _, _, server in guard._server_blocks(result) if '# AUDIT INSERT' in server]
        assert len(selected) == 1
        assert 'server_name panel.test;' in selected[0]
        assert 'server_name cdn.test;' not in selected[0]
        assert guard.public_domain_from_nginx_text(source) == 'panel.test'


def test_domain_discovery_excludes_reject_handshake_host():
    source = host('reject.test', '127.0.0.1:9443 ssl proxy_protocol', 'ssl_reject_handshake on;')
    source += host('panel.test', 'unix:/dev/shm/nginx-http.sock ssl proxy_protocol')
    with patch.object(guard, 'cfg', return_value=''):
        assert guard.public_domain_from_nginx_text(source, '/private123') == 'panel.test'


def test_managed_route_keeps_identity_over_unrelated_fallback():
    source = host('fallback.test', '127.0.0.1:9443 ssl proxy_protocol')
    source += host('panel.test', 'unix:/dev/shm/nginx-http.sock ssl proxy_protocol', 'location ^~ /private123/ { proxy_pass http://unix:/tmp/panel.sock:/; }')
    with patch.object(guard, 'cfg', return_value=''):
        assert guard.public_domain_from_nginx_text(source, '/private123') == 'panel.test'


def test_settings_markup_labels_and_tab_semantics():
    source=(Path(__file__).resolve().parents[1]/'app/webapp.py').read_text(encoding='utf-8')
    ast.parse(source)
    for name in ('service_name', 'bot_token', 'admin_ids', 'subscription_days', 'web_timezone'):
        assert f'<label for="setting-{name}">' in source
        assert f'id="setting-{name}"' in source
    assert 'role="tablist" data-tabs' in source
    for name in ('bot', 'payment', 'xui', 'security', 'notifications', 'data'):
        assert f'aria-controls="{name}"' in source
        assert f'role="tabpanel" aria-labelledby="settings-tab-{name}"' in source


def test_settings_keyboard_navigation_executes_in_node(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    import pytest
    node=os.environ.get('FARGOVPN_NODE') or shutil.which('node')
    if not node:
        pytest.skip('Node.js is unavailable')
    script=(Path(__file__).resolve().parents[1]/'app/static/settings.js').read_text(encoding='utf-8')
    harness=r'''
const assert=require('node:assert/strict');
const handlers={},selected={value:''};
const buttons=['bot','payment','xui'].map(name=>({dataset:{tab:name},classList:{toggle(){}},setAttribute(k,v){this[k]=v},addEventListener(){},focus(){document.activeElement=this}}));
const sections=buttons.map(b=>({dataset:{tabSection:b.dataset.tab},classList:{toggle(){}}}));
const root={querySelectorAll:()=>buttons,addEventListener:(k,f)=>handlers[k]=f};
const form={querySelector:s=>s.includes('settings_tab')?selected:(s==='#settings-save-status'?{}:null),addEventListener(){}};
global.document={getElementById:()=>form,querySelector:()=>root,querySelectorAll:()=>sections,activeElement:buttons[0]};
global.location={href:'https://example.test/settings',search:'',hash:''};global.history={replaceState(){}};global.window={scrollTo(){}};
'''
    checks=r'''
assert.equal(buttons[0].tabIndex,0);assert.equal(buttons[1].tabIndex,-1);assert.equal(sections[1].hidden,true);
function key(k){handlers.keydown({key:k,preventDefault(){}})}
key('ArrowRight');assert.equal(selected.value,'payment');assert.equal(document.activeElement,buttons[1]);assert.equal(sections[1].hidden,false);assert.equal(buttons[1]['aria-selected'],'true');
key('End');assert.equal(selected.value,'xui');key('ArrowRight');assert.equal(selected.value,'bot');key('ArrowLeft');assert.equal(selected.value,'xui');key('Home');assert.equal(selected.value,'bot');
'''
    path=tmp_path/'settings-keyboard.cjs';path.write_text(harness+script+checks,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr



def test_managed_location_keeps_hsts_for_gateway_errors():
    with patch.object(guard, 'cfg', side_effect=lambda name, default=None: default):
        block=guard.block('/private123','/tmp/fargo.sock')
    assert 'proxy_hide_header Strict-Transport-Security;' in block
    assert 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' in block


def test_edge_replaces_untrusted_forwarded_chain():
    with patch.object(guard, 'cfg', side_effect=lambda name, default=None: default):
        block = guard.block('/private123', '/tmp/fargo.sock')
    assert 'proxy_set_header X-Forwarded-For $remote_addr;' in block
    assert '$proxy_add_x_forwarded_for' not in block
