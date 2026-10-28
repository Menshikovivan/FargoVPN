import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

@pytest.fixture
def module(monkeypatch):
    services = types.ModuleType('services')
    billing = types.ModuleType('services.billing')
    billing.profile_owner = Mock(side_effect=lambda actor, path: actor)
    api = types.ModuleType('services.xui_api')
    api.resolve_hwid_client_email_sync = Mock(return_value='Exact+42')
    api.list_client_hwids_sync = Mock(return_value=[{'id': 7, 'deviceModel':'<img>', 'deviceOs':'iOS', 'lastSeen':0}])
    api.remove_client_hwid_sync = Mock()
    status = types.ModuleType('services.subscription_status')
    status.has_subscription = lambda user: bool(user and user.get('uuid'))
    services.billing = billing
    services.xui_api = api
    monkeypatch.setitem(sys.modules, 'services', services)
    monkeypatch.setitem(sys.modules, 'services.billing', billing)
    monkeypatch.setitem(sys.modules, 'services.xui_api', api)
    monkeypatch.setitem(sys.modules, 'services.subscription_status', status)
    fake_time = types.ModuleType('time_utils')
    fake_time.from_timestamp = Mock()
    monkeypatch.setitem(sys.modules, 'time_utils', fake_time)
    path=Path(__file__).resolve().parents[1]/'app'/'bot_devices.py'
    spec=importlib.util.spec_from_file_location('bot_devices_test',path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result, billing, api

def user(actor):
    return {'email':'exact+42', 'uuid':'sub-uuid'} if actor in (1,2) else None

def test_owner_can_list_and_remove_exact_resolved_device(module):
    m,b,api = module
    assert m.list_for_actor(1,None,user)[0] == 1
    m.delete_for_actor(1,7,None,user)
    api.remove_client_hwid_sync.assert_called_once_with('Exact+42',7)

def test_family_member_cannot_remove_or_enumerate_other_devices(module):
    m,b,api = module
    b.profile_owner.side_effect=None
    b.profile_owner.return_value=2
    assert m.list_for_actor(1,None,user)[0] == 2
    with pytest.raises(PermissionError): m.delete_for_actor(1,7,None,user)
    api.remove_client_hwid_sync.assert_not_called()

def test_foreign_or_missing_device_is_not_deleted(module):
    m,b,api = module
    with pytest.raises(ValueError): m.delete_for_actor(1,99,None,user)
    api.remove_client_hwid_sync.assert_not_called()

def test_owner_rechecked_before_delete(module):
    m,b,api = module
    b.profile_owner.side_effect=[1,2]
    with pytest.raises(PermissionError): m.delete_for_actor(1,7,None,user)
    api.remove_client_hwid_sync.assert_not_called()

def test_unknown_actor_denied_before_api(module):
    m,b,api = module
    with pytest.raises(PermissionError): m.list_for_actor(10,None,user)
    api.resolve_hwid_client_email_sync.assert_not_called()

def test_html_escaped_and_member_cannot_see_delete_buttons(module):
    m,b,api = module
    text,keyboard=m.render_list(1,2,api.list_client_hwids_sync.return_value)
    assert '&lt;img&gt;' in text and '<img>' not in text
    assert 'Удалять записи' in text
    assert all(not (button.callback_data or '').startswith('device:ask:') for row in keyboard.inline_keyboard for button in row)
    assert 'не гарантирует отключение VPN' in m.render_confirmation({'deviceModel':'<img>'})

def test_paging_contains_six_items_and_controls(module):
    m,b,api = module
    text,keyboard=m.render_list(1,1,[{'id': i, 'deviceModel':f'Phone {i}'} for i in range(1,9)],2)
    assert 'Phone 7' in text and 'Phone 1' not in text
    assert any((button.callback_data or '') == 'device:page:1' for row in keyboard.inline_keyboard for button in row)
