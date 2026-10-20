"""Explicit confirmation and revocation gates must precede any erasure."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest


def load_privacy(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'app/services/privacy.py'
    spec = importlib.util.spec_from_file_location('privacy_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, 'config', SimpleNamespace())
    def forbidden_connect():
        raise AssertionError('Database must not be opened before explicit confirmation')
    monkeypatch.setitem(sys.modules, 'db', SimpleNamespace(connect=forbidden_connect))
    return module

@pytest.mark.parametrize('kwargs', [
    {'confirmed_tg_id': 124, 'actor': 'admin', 'vpn_revoked': True},
    {'confirmed_tg_id': 123, 'actor': '', 'vpn_revoked': True},
    {'confirmed_tg_id': 123, 'actor': 'admin', 'vpn_revoked': False},
])
def test_confirmation_and_vpn_revocation_before_database(monkeypatch, kwargs):
    with pytest.raises(ValueError):
        load_privacy(monkeypatch).erase_user_data(123, **kwargs)
