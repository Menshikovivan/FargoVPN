from __future__ import annotations

import sys
import types
import sqlite3

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app'))
if 'config' not in sys.modules:
    cfg = types.ModuleType('config')
    cfg.PAYMENT_PRICE = 150
    sys.modules['config'] = cfg

from services import billing
from services.billing import (
    family_profile_map,
    subscription_label,
    subscription_status_emoji,
    subscription_status_tone,
    telegram_identity_label,
)


def test_no_pin_is_not_misreported_as_lifetime():
    assert subscription_label({'registration_status': 'awaiting_invite', 'expiry_time': 0}) == 'Не зарегистрирован'
    assert subscription_label({'registration_status': 'new', 'expiry_time': 0}) == 'Не зарегистрирован'


def test_registered_without_xui_identity_is_unpaid():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'uuid': '', 'email': '', 'sub_id': ''}) == 'Без подписки'


def test_zero_expiry_is_lifetime_only_for_an_existing_subscription():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'email': 'user@example.test'}) == 'Бессрочная подписка'


def test_disabled_and_expired_subscriptions_keep_their_real_state():
    assert subscription_label({'registration_status': 'active', 'expiry_time': 0, 'email': 'u@example.test', 'enable': False}) == 'Доступ отключён'
    assert subscription_label({'registration_status': 'active', 'expiry_time': 10, 'email': 'u@example.test'}, now_ms=11) == 'Подписка истекла'


def test_subscription_status_colors_are_shared_for_all_registration_states():
    assert subscription_status_tone('Не зарегистрирован') == 'warn'
    assert subscription_status_tone('Без подписки') == 'warn'
    assert subscription_status_tone('Семейная · без подписки') == 'warn'
    assert subscription_status_tone('Подписка истекла') == 'warn'
    assert subscription_status_tone('Семейная · подписка истекла') == 'warn'
    assert subscription_status_tone('Доступ отключён') == 'bad'
    assert subscription_status_tone('Семейная · подписка активна') == 'good'
    assert subscription_status_emoji('Подписка истекла') == '🟡'


def test_family_subscription_uses_owner_expiry_and_status():
    member = {
        'registration_status': 'active',
        'family_subscription': True,
        'family_owner_has_subscription': True,
        'family_owner_enable': True,
        'family_owner_expiry_time': 200,
    }
    assert subscription_label(member, now_ms=100) == 'Семейная · подписка активна'
    assert subscription_label({**member, 'family_owner_expiry_time': 50}, now_ms=100) == 'Семейная · подписка истекла'
    assert subscription_label({**member, 'family_owner_expiry_time': 0}, now_ms=100) == 'Семейная · бессрочная подписка'
    assert subscription_label({**member, 'family_owner_enable': False}, now_ms=100) == 'Семейная · доступ отключён'


def test_removed_family_member_does_not_inherit_owner_subscription():
    assert subscription_label({
        'registration_status': 'active',
        'expiry_time': 0,
        'uuid': '',
        'email': '',
        'sub_id': '',
    }) == 'Без подписки'


def test_telegram_identity_falls_back_from_username_to_full_name_then_id():
    assert telegram_identity_label(10, '@nick', 'Иван Петров') == '@nick'
    assert telegram_identity_label(10, '', '  Иван   Петров ') == 'Иван Петров'
    assert telegram_identity_label(10, '', '') == 'ID 10'


def test_family_profile_map_reads_owner_updates_and_stops_after_unlink(monkeypatch):
    connection = sqlite3.connect(':memory:')
    connection.executescript(
        """
        CREATE TABLE users (tg_id INTEGER PRIMARY KEY, uuid TEXT, email TEXT, sub_id TEXT, expiry_time INTEGER, enable INTEGER);
        CREATE TABLE telegram_profile_links (telegram_id INTEGER, profile_tg_id INTEGER);
        INSERT INTO users VALUES(10,'owner-uuid','owner@example','owner-sub',1000,1);
        INSERT INTO telegram_profile_links VALUES(20,10);
        INSERT INTO telegram_profile_links VALUES(10,10);
        """
    )
    monkeypatch.setattr(billing.db, 'connect', lambda _path: connection)

    profiles = family_profile_map('unused')
    assert set(profiles) == {20}
    first = profiles[20]
    assert first['family_owner_tg_id'] == 10
    assert first['family_owner_expiry_time'] == 1000
    assert first['family_owner_has_subscription'] is True

    connection.execute("UPDATE users SET expiry_time=2000 WHERE tg_id=10")
    assert family_profile_map('unused')[20]['family_owner_expiry_time'] == 2000

    connection.execute("DELETE FROM telegram_profile_links WHERE telegram_id=20")
    assert family_profile_map('unused') == {}
    connection.close()
