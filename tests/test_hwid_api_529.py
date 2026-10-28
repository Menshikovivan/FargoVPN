import importlib.util
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
if 'config' not in sys.modules:
    spec = importlib.util.spec_from_file_location('config', APP / 'config.example.py')
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    sys.modules['config'] = config
from services import xui_api as api


def test_hwid_resolves_actual_email_case_by_unique_uuid(monkeypatch):
    monkeypatch.setattr(api, '_client_list_sync', lambda: {'obj': [{'email': 'Alice+42', 'uuid': 'one'}]})
    assert api.resolve_hwid_client_email_sync('alice+42', 'one') == 'Alice+42'


@pytest.mark.parametrize('records,email,uuid', [
    ([{'email': 'Alice', 'uuid': 'other'}], 'alice', 'one'),
    ([{'email': 'Alice', 'uuid': 'one'}, {'email': 'ALICE', 'uuid': 'two'}], 'alice', 'one'),
    ([{'email': 'Other', 'uuid': 'one'}], 'alice', 'one'),
    ([{'email': 'Alice', 'uuid': 'one'}, {'email': 'ALICE', 'uuid': 'one'}], 'alice', 'one'),
    ([{'email': 'Alice', 'uuid': 'one'}], 'alice', ''),
    ([None], 'alice', 'one'),
])
def test_hwid_resolve_rejects_conflicts(monkeypatch, records, email, uuid):
    monkeypatch.setattr(api, '_client_list_sync', lambda: {'obj': records})
    with pytest.raises((ValueError, RuntimeError)):
        api.resolve_hwid_client_email_sync(email, uuid)


def test_list_hwids_without_limit_uses_authenticated_post(monkeypatch):
    calls = []
    def request(method, path, *args, **kwargs):
        calls.append((method, path))
        return {'success': True, 'obj': [{'id': 3, 'deviceModel': 'Phone'}]}
    monkeypatch.setattr(api, 'request_json_sync', request)
    assert api.list_client_hwids_sync('Alice+42') == [{'id': 3, 'deviceModel': 'Phone'}]
    assert calls == [('POST', 'panel/api/clients/hwids/Alice%2B42')]


def test_empty_hwids_null_and_empty_list_are_success(monkeypatch):
    for obj in (None, []):
        monkeypatch.setattr(api, 'request_json_sync', lambda *a, **k: {'success': True, 'obj': obj})
        assert api.list_client_hwids_sync('alice') == []


@pytest.mark.parametrize('response', [{}, {'success': False, 'obj': []}, {'success': True}, {'success': True, 'obj': {}}, {'success': True, 'obj': [None]}, {'success': True, 'obj': [{}]}, {'success': True, 'obj': [{'id': 1}, {'id': 1}]}, {'success': True, 'obj': [{'id': True}]}])
def test_bad_response_is_not_reported_as_no_devices(monkeypatch, response):
    monkeypatch.setattr(api, 'request_json_sync', lambda *a, **k: response)
    with pytest.raises(RuntimeError):
        api.list_client_hwids_sync('alice')


def test_single_device_removal_checks_membership_and_never_clears_all(monkeypatch):
    calls = []
    def request(method, path, *args, **kwargs):
        calls.append((method, path))
        return {'success': True, 'obj': [{'id': 7}]} if method == 'POST' else {'success': True}
    monkeypatch.setattr(api, 'request_json_sync', request)
    api.remove_client_hwid_sync('Alice+42', 7)
    assert calls == [('POST', 'panel/api/clients/hwids/Alice%2B42'), ('DELETE', 'panel/api/clients/hwids/Alice%2B42/7')]


@pytest.mark.parametrize('device_id', [True, -1, 0, 2147483648, 'x', '1/2'])
def test_rejects_invalid_device_id_without_request(monkeypatch, device_id):
    monkeypatch.setattr(api, 'request_json_sync', lambda *a, **k: pytest.fail('unexpected request'))
    with pytest.raises(ValueError):
        api.remove_client_hwid_sync('alice', device_id)


def test_stale_or_foreign_device_never_deleted(monkeypatch):
    calls = []
    def request(method, path, *args, **kwargs):
        calls.append(method)
        return {'success': True, 'obj': [{'id': 8}]}
    monkeypatch.setattr(api, 'request_json_sync', request)
    with pytest.raises(ValueError):
        api.remove_client_hwid_sync('alice', 7)
    assert calls == ['POST']


def test_api_error_does_not_leak_url_or_email(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('confidential-client@email.example and private-url')
    monkeypatch.setattr(api, 'request_json_sync', fail)
    with pytest.raises(RuntimeError) as exc:
        api.list_client_hwids_sync('confidential-client@email.example')
    assert 'confidential' not in str(exc.value)
    assert 'private-url' not in str(exc.value)
