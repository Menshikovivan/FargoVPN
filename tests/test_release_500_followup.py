from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(path.read_text(encoding='utf-8'), node)


def _load_xui_helpers():
    import json, math, re as _re, threading, time
    ns = {'Any': object, 'dict': dict, 'list': list, 'str': str, 'int': int,
          'math': math, 'threading': threading, 'time': time, 'json': json, 're': _re, '_SERVER_TRAFFIC_SAMPLE': {'ts': 0.0, 'up': 0, 'down': 0}, '_SERVER_TRAFFIC_SAMPLE_LOCK': threading.Lock()}
    code = '\n\n'.join(_function_source(ROOT / 'services' / 'xui_api.py', n) for n in (
        '_int', '_bool', '_finite_float', '_client_credential_id', 'normalize_timestamp_ms',
        'normalize_client', '_traffic_counter', '_sample_network_rate', '_normalize_server_status',
        'summarize_server_traffic', 'summarize_inbound_traffic'))
    exec(code, ns)
    return ns


def test_3xui_current_client_and_inbound_shapes_preserve_traffic():
    ns = _load_xui_helpers()
    client = ns['normalize_client']({
        'email': 'alice', 'uuid': 'u1', 'totalGB': 10,
        'expiryTime': 1735689600000,
        'traffic': {'up': 1234, 'down': 5678, 'enable': True},
    })
    assert client['up'] == 1234
    assert client['down'] == 5678
    inbound = ns['summarize_inbound_traffic']([{
        'id': 1, 'up': 0, 'down': 0, 'enable': True,
        'clientStats': [{'email': 'alice', 'up': 1234, 'down': 5678}],
    }])
    assert inbound['up'] == 1234
    assert inbound['down'] == 5678


def test_3xui_server_status_supports_current_and_legacy_network_keys():
    ns = _load_xui_helpers()
    result = ns['summarize_server_traffic']({'netIO': {'up': 1000, 'down': 2000}})
    assert result['up'] == 1000 and result['down'] == 2000
    result2 = ns['summarize_server_traffic']({'netTraffic': {'sent': 3000, 'recv': 4000}})
    assert result2['up'] == 3000 and result2['down'] == 4000


def test_notifications_buttons_are_not_left_disabled_after_preflight_failure():
    text = (ROOT / 'webapp.py').read_text(encoding='utf-8')
    push_text = (ROOT / 'static/push.js').read_text(encoding='utf-8')
    assert '<button type="button" id="panel-push-enable">' in text
    boot = re.search(r'const boot=\(\)=>.*?window\.addEventListener\(\'pagehide\'', push_text, re.S).group(0)
    assert "['panel-push-enable','panel-push-test','panel-push-disable','panel-push-check','panel-push-log-refresh','app-log-refresh']" in boot
    assert 'el.disabled=false;el.removeAttribute(\'disabled\')' in boot
    assert 'Push подготовится по действию пользователя' in boot
    assert 'Подготавливаю Push непосредственно после клика пользователя' in push_text
    assert 'if(b)b.disabled=false' in push_text


def test_github_publish_syncs_full_safe_project_tree():
    text = (ROOT / 'update_manager.py').read_text(encoding='utf-8')
    start = text.index('def _github_main_public_files(')
    end = text.index('\n\ndef _github_main_sync(', start)
    source = text[start:end]
    assert 'archive_root.rglob("*")' in source
    assert 'forbidden_names' in source
    assert 'forbidden_dirs' in source
    assert 'forbidden_suffixes' in source
    assert 'config.py' in source and '.env' in source
    assert 'files["install.sh"] = _github_main_bootstrap()' in source


def test_current_release_versions_are_consistent():
    for name in ('VERSION', 'app/VERSION', 'static/VERSION'):
        assert (ROOT / name).read_text(encoding='utf-8').strip() == '5.1'


def test_install_accepts_legacy_profile_argument():
    src = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--profile)" in src
    assert "--profile=*)" in src
    assert "LEGACY_PROFILE" in src
    assert "в 5.1 используется полный профиль" in src



def test_3xui_monitoring_speed_uses_single_network_sample():
    ns = _load_xui_helpers()
    # Seed a previous sample, then normalize a new server snapshot.
    ns['_SERVER_TRAFFIC_SAMPLE'].update({'ts': ns['time'].monotonic() - 2.0, 'up': 1000, 'down': 2000})
    result = ns['_normalize_server_status']({
        'netIO': {'up': 3000, 'down': 6000},
        'cpu': 10, 'xray': {'state': 'running', 'version': 'v1'},
    })
    assert result['net_up_speed'] > 0
    assert result['net_down_speed'] > 0


def test_monitoring_traffic_summary_does_not_overwrite_speed_sample():
    ns = _load_xui_helpers()
    ns['_SERVER_TRAFFIC_SAMPLE'].update({'ts': 0.0, 'up': 0, 'down': 0})
    result = ns['summarize_server_traffic']({'netIO': {'up': 100, 'down': 200}})
    assert result['up'] == 100 and result['down'] == 200
    assert result['up_speed'] == 0.0 and result['down_speed'] == 0.0


def test_users_filters_rebuild_list_in_selected_order():
    text = (ROOT / 'static/users.js').read_text(encoding='utf-8')
    assert 'list.replaceChildren(frag)' in text
    assert "filtered.sort((a,b)=>" in text
    assert "const key=keys[so]||keys.remaining" in text
    assert "[status,sort,order].forEach(el=>el.addEventListener('change',reset))" in text


def test_panel_assets_use_cache_busting_independent_of_product_version():
    text = (ROOT / 'webapp.py').read_text(encoding='utf-8')
    assert 'def _panel_asset_version()' in text
    assert 'panel.js", "panel.css"' in text
    assert '?v={asset_version}' in text


def test_update_page_reloads_after_completed_same_version_job():
    text = (ROOT / 'webapp.py').read_text(encoding='utf-8')
    assert "updateDoneQuery" in text
    assert "sessionStorage.getItem('fargovpn_update_watch_job')" in text
    assert "lastState==='completed'" in text
    assert "update_done=1" in text
    assert "String(data.installed_version)!==String(pageVersion)" not in text
