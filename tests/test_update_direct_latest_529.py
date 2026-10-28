import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'app'


@pytest.fixture
def manager(monkeypatch):
    if os.name == 'nt':
        monkeypatch.setitem(sys.modules, 'fcntl', types.ModuleType('fcntl'))
    cfg = types.ModuleType('config')
    detached = types.ModuleType('detached_jobs')
    detached.DetachedJobError = RuntimeError
    detached.launch_detached = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'config', cfg)
    monkeypatch.setitem(sys.modules, 'detached_jobs', detached)
    spec = importlib.util.spec_from_file_location('update_manager_direct_test', APP / 'update_manager.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stale_remote_metadata_refreshes_directly_to_latest(monkeypatch, manager):
    old = {'source': 'github', 'version': '5.2.1', 'sha256': 'old'}
    latest = {'source': 'github', 'version': '5.2.8', 'available': True, 'sha256': 'new'}
    called = []
    monkeypatch.setattr(manager, 'check_available_update', lambda force=False: called.append(force) or latest)
    assert manager.resolve_automatic_latest_update(old) == latest
    assert called == [True]


def test_publisher_older_local_cannot_shadow_newer_release(monkeypatch, manager):
    local = {'source': 'local', 'version': '5.2.6', 'path': '/updates/old.tar.gz'}
    remote = {'source': 'github', 'version': '5.2.8', 'available': True}
    monkeypatch.setattr(manager, 'check_available_update', lambda force=False: remote)
    assert manager.resolve_automatic_latest_update(local) == remote


def test_newer_local_or_offline_publisher_keeps_prepared_archive(monkeypatch, manager):
    local = {'source': 'local', 'version': '5.2.9', 'path': '/updates/prepared.tar.gz'}
    monkeypatch.setattr(manager, 'check_available_update', lambda force=False: {'available': True, 'version': '5.2.8'})
    assert manager.resolve_automatic_latest_update(local) == local
    monkeypatch.setattr(manager, 'check_available_update', lambda force=False: {'available': False, 'error': 'offline'})
    assert manager.resolve_automatic_latest_update(local) == local


def test_remote_offline_fails_closed_not_stale(monkeypatch, manager):
    monkeypatch.setattr(manager, 'check_available_update', lambda force=False: {'available': False, 'error': 'offline'})
    with pytest.raises(manager.UpdateError):
        manager.resolve_automatic_latest_update({'source': 'github', 'version': '5.2.4'})


def test_explicit_manual_update_is_not_retargeted(monkeypatch, manager):
    monkeypatch.setattr(manager, 'check_available_update', lambda **kw: pytest.fail('unexpected network'))
    manual = {'source': 'manual', 'version': '5.2.6', 'path': '/updates/chosen.tar.gz'}
    assert manager.resolve_automatic_latest_update(manual) == manual


def test_worker_invokes_resolver_before_download():
    source = (APP / 'update_worker.py').read_text(encoding='utf-8')
    assert source.index('resolve_automatic_latest_update') < source.index('obtain_update_archive')
