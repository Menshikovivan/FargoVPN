import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import nginx_panel_guard as g

SAMPLE = 'server {\n    listen 127.0.0.1:9443 ssl;\n    server_name vpn.example.test;\n}\n'

@pytest.fixture
def setup(monkeypatch, tmp_path):
    target = tmp_path / 'nginx.conf'
    target.write_bytes(SAMPLE.encode())
    monkeypatch.setattr(g, 'NGINX_CONFIG_ROOT', tmp_path)
    monkeypatch.setattr(g, 'cfg', lambda name, default=None: {'WEB_REVERSE_PROXY': True, 'WEB_DOMAIN':'vpn.example.test', 'WEB_SOCKET_PATH':'/run/vpn-service/fargovpn.sock'}.get(name, default))
    monkeypatch.setattr(g, 'prefix', lambda:'/abcdef123')
    monkeypatch.setattr(g, '_nginx_loaded_conf_files', lambda:[target])
    monkeypatch.setattr(g, '_nginx_dump', lambda:target.read_text())
    monkeypatch.setattr(g.subprocess, 'run', lambda *a,**k:SimpleNamespace(returncode=0,stderr='',stdout=''))
    return target

def test_missing_route_invokes_strict_repair(monkeypatch, setup):
    calls=[]
    monkeypatch.setattr(g, 'ensure_once', lambda **kw:calls.append(kw) or True)
    assert g.ensure_install_route()
    assert calls == [dict(persist_identity=False, expected_target=setup, expected_source=SAMPLE)]

def test_healthy_no_edit_or_reload(monkeypatch, setup):
    setup.write_bytes(SAMPLE.replace('}', 'location ^~ /abcdef123/ {\n proxy_pass http://unix:/run/vpn-service/fargovpn.sock:/;\n}\n}').encode())
    monkeypatch.setattr(g, 'ensure_once', lambda **kw:pytest.fail('unexpected repair'))
    assert g.ensure_install_route()

def test_ambiguous_host_or_conflict_fails(monkeypatch, setup):
    monkeypatch.setattr(g, 'ensure_once', lambda **kw:pytest.fail('unexpected repair'))
    setup.write_text(SAMPLE+SAMPLE)
    assert not g.ensure_install_route()
    setup.write_text(SAMPLE.replace('}', 'location ^~ /abcdef123/ {\n proxy_pass http://127.0.0.1:5555;\n}\n}'))
    assert not g.ensure_install_route()

def test_crlf_and_wrong_domain_rejected(monkeypatch, setup):
    setup.write_bytes(SAMPLE.replace('\n','\r\n').encode())
    assert not g.ensure_install_route()
    setup.write_text(SAMPLE.replace('vpn.example.test','other.example.test'))
    assert not g.ensure_install_route()

def test_existing_backup_kept(monkeypatch, setup):
    backup = setup.with_name(setup.name+'.fargovpn-backup')
    backup.write_text('previous')
    assert not g.ensure_once(persist_identity=False, expected_target=setup, expected_source=SAMPLE)
    assert backup.read_text() == 'previous'
    assert setup.read_text() == SAMPLE

def test_nginx_invalid_rolls_back(monkeypatch, setup):
    monkeypatch.setattr(g.os, 'chown', lambda *a:None, raising=False)
    monkeypatch.setattr(g.subprocess, 'run', lambda *a,**k:SimpleNamespace(returncode=1,stderr='bad',stdout=''))
    assert not g.ensure_once(persist_identity=False, expected_target=setup, expected_source=SAMPLE)
    assert setup.read_bytes() == SAMPLE.encode()

def test_existing_legacy_routes_not_removed(setup):
    original=SAMPLE.replace('}', '# BEGIN FARGOVPN-old MANAGED LOCATION\n    location /legacy { return 200; }\n    # END FARGOVPN-old MANAGED LOCATION\n}')
    setup.write_bytes(original.encode())
    assert not g.ensure_once(persist_identity=False,expected_target=setup,expected_source=original)
    assert setup.read_bytes()==original.encode()


def test_identity_existing_values_are_not_overwritten(monkeypatch):
    values = {'WEB_PUBLIC_PREFIX':'/abcdef123', 'WEB_DOMAIN':'second.example.test',
              'WEB_TLS_SERVER_NAME':'second.example.test',
              'BOT_PANEL_URL':'https://second.example.test/abcdef123/',
              'PUBLIC_PANEL_URL':'https://second.example.test/abcdef123/'}
    monkeypatch.setattr(g,'cfg',lambda name,default=None: values.get(name,default))
    monkeypatch.setattr(g,'_nginx_dump',lambda:SAMPLE.replace('vpn.example.test','first.example.test second.example.test'))
    monkeypatch.setattr(g,'_set_config_value',lambda *a:pytest.fail('identity overwritten'))
    assert g._persist_public_identity() == values['PUBLIC_PANEL_URL']

def test_identity_conflict_is_not_modified(monkeypatch):
    values = {'WEB_PUBLIC_PREFIX':'/abcdef123','WEB_DOMAIN':'one.example.test',
              'PUBLIC_PANEL_URL':'https://other.example.test/abcdef123/'}
    monkeypatch.setattr(g,'cfg',lambda name,default=None: values.get(name,default))
    monkeypatch.setattr(g,'_set_config_value',lambda *a:pytest.fail('identity overwritten'))
    assert g._persist_public_identity() == values['PUBLIC_PANEL_URL']

def test_first_install_sets_only_unique_routed_domain(monkeypatch):
    values = {'WEB_PUBLIC_PREFIX':'/abcdef123'}
    server = SAMPLE.replace('}', 'location ^~ /abcdef123/ {\n proxy_pass http://unix:/run/vpn-service/fargovpn.sock:/;\n}\n}')
    monkeypatch.setattr(g,'cfg',lambda name,default=None: values.get(name,default))
    monkeypatch.setattr(g,'_nginx_dump',lambda:server)
    monkeypatch.setattr(g,'_set_config_value',lambda name,value:values.update({name:value}))
    assert g._persist_public_identity() == 'https://vpn.example.test/abcdef123/'
    assert values['WEB_DOMAIN'] == 'vpn.example.test'
    assert values['BOT_PANEL_URL'] == 'https://vpn.example.test/abcdef123/'

def test_first_install_multiple_names_does_not_guess(monkeypatch):
    server = SAMPLE.replace('vpn.example.test','one.example.test two.example.test').replace('}', 'location ^~ /abcdef123/ {\n proxy_pass http://unix:/run/vpn-service/fargovpn.sock:/;\n}\n}')
    monkeypatch.setattr(g,'cfg',lambda name,default=None: '/abcdef123' if name=='WEB_PUBLIC_PREFIX' else default)
    monkeypatch.setattr(g,'_nginx_dump',lambda:server)
    monkeypatch.setattr(g,'_set_config_value',lambda *a:pytest.fail('identity guessed'))
    assert g._persist_public_identity() == ''
