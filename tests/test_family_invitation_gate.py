from pathlib import Path


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
