from pathlib import Path
from contextlib import contextmanager
import hashlib
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "app"


def test_private_family_start_is_the_only_deep_link_allowed_before_pin():
    source = (ROOT / "telegram_registration_middleware.py").read_text(encoding="utf-8")
    assert 'r"/start(?:@\\w+)?\\s+family_[A-Za-z0-9_-]{20,100}"' in source
    assert "is_private_source(source, chat)" in source
    assert "family_" in source


def test_pending_family_member_is_kept_pending_until_pin():
    source = (ROOT / "services" / "billing.py").read_text(encoding="utf-8")
    assert "if str(by_id[owner][1]).lower() != 'active'" in source
    assert "Оба аккаунта должны пройти проверку PIN-кода" not in source
    assert "Keep pending PIN state intact" in source


def test_family_start_is_allowed_even_while_pin_attempts_are_throttled():
    source = (ROOT / "telegram_registration_middleware.py").read_text(encoding="utf-8")
    family_gate = source.index('re.fullmatch(r"/start(?:@\\w+)?\\s+family_')
    pin_throttle = source.index("if state.blocked_until > now:")
    assert family_gate < pin_throttle
    assert "does not activate PIN-gated users" in source


def test_existing_user_row_can_be_joined_without_stale_expiry_blocking():
    source = (ROOT / "services" / "billing.py").read_text(encoding="utf-8")
    assert "def _has_own_vpn_identity(user_row" in source
    assert "return any(str(user_row[index] or \"\").strip() for index in (2, 3, 4))" in source
    assert "_has_own_vpn_identity(alias)" in source
    assert "int(alias[5] or 0) > 0" not in source


def test_same_owner_reinvite_is_idempotent_even_when_profile_is_full(monkeypatch):
    app_root = str(ROOT)
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    if "config" not in sys.modules:
        cfg = types.ModuleType("config")
        cfg.PAYMENT_PRICE = 200
        sys.modules["config"] = cfg
    from services import billing

    token = "idempotent-family-invite-token"
    digest = hashlib.sha256(token.encode()).hexdigest()

    class Result:
        def __init__(self, one=None, many=None):
            self.one = one
            self.many = many or []
        def fetchone(self):
            return self.one
        def fetchall(self):
            return self.many

    class Connection:
        def __init__(self, existing_owner):
            self.existing_owner = existing_owner
            self.consumed = False
            self.count_checked = False
        def execute(self, sql, params=()):
            if "SELECT profile_tg_id,expires_at,consumed_at FROM telegram_profile_link_requests" in sql:
                return Result((100, 4_000_000_000, None))
            if "FROM users WHERE tg_id IN" in sql:
                return Result(many=[
                    (100, "active", "owner-uuid", "owner@example.test", "owner-sub", 0),
                    (101, "active", "", "", "", 0),
                ])
            if "SELECT profile_tg_id FROM telegram_profile_links WHERE telegram_id=?" in sql:
                return Result((self.existing_owner,))
            if "SELECT COUNT(*) FROM telegram_profile_links" in sql:
                self.count_checked = True
                return Result((4,))
            if "UPDATE telegram_profile_link_requests SET consumed_at" in sql:
                self.consumed = True
            return Result()

    for existing_owner, should_succeed in ((100, True), (200, False)):
        connection = Connection(existing_owner)

        @contextmanager
        def fake_connect(_db_path=None):
            yield connection

        monkeypatch.setattr(billing.db, "connect", fake_connect)
        if should_succeed:
            assert billing.approve_profile_link_sync(token, 101) == 100
            assert connection.consumed is True
            assert connection.count_checked is False
        else:
            with pytest.raises(ValueError, match="уже связан с другим профилем"):
                billing.approve_profile_link_sync(token, 101)
            assert connection.consumed is False
