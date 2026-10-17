from __future__ import annotations

import sys
import types

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app'))
if 'config' not in sys.modules:
    cfg = types.ModuleType('config')
    cfg.PAYMENT_PRICE = 150
    sys.modules['config'] = cfg

from services.billing import subscription_label


def test_no_pin_is_not_misreported_as_lifetime():
    assert subscription_label({'registration_status': 'awaiting_invite', 'expiry_time': 0}) == 'Не прошёл проверку PIN-кода'
    assert subscription_label({'registration_status': 'new', 'expiry_time': 0}) == 'Не прошёл проверку PIN-кода'


def test_registered_without_xui_identity_is_unpaid():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'uuid': '', 'email': '', 'sub_id': ''}) == 'Подписка не оплачена'


def test_zero_expiry_is_lifetime_only_for_an_existing_subscription():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'email': 'user@example.test'}) == 'Бессрочная подписка'


def test_disabled_and_expired_subscriptions_keep_their_real_state():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'email': 'u@example.test', 'enable': False}) == 'Доступ отключён'
    assert subscription_label({'registration_status': 'active', 'expiry_time': 10, 'email': 'u@example.test'}, now_ms=11) == 'Подписка истекла'
