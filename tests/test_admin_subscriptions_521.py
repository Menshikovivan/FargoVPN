import ast
from datetime import date, datetime
import html
import re
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
NOW_SECONDS = 1_800_000_000
NOW_MS = NOW_SECONDS * 1000
DAY_MS = 86_400_000


def _extract(name, *, async_function=False):
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
    kind = ast.AsyncFunctionDef if async_function else ast.FunctionDef
    node = next(n for n in tree.body if isinstance(n, kind) and n.name == name)
    node.decorator_list = []
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        arg.annotation = None
    node.args.vararg = node.args.kwarg = None
    node.returns = None
    return node


def _report(rows, expected_price=None):
    class Result:
        def __init__(self, records):
            self.records = records

        def fetchall(self):
            return self.records

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=()):
            return Result(rows if "FROM users u" in sql else [])

    ns = {
        "database_adapter": SimpleNamespace(connect=lambda *_a, **_k: Connection()),
        "config": SimpleNamespace(DB_PATH="unused", SUBSCRIPTION_DAYS=30),
        "time": SimpleNamespace(time=lambda: NOW_SECONDS),
        "html": html,
        "billing": SimpleNamespace(
            telegram_identity_label=lambda tg_id, username, display, notes: f"@{username}" if username else (notes or f"ID {tg_id}"),
            expected_payment_amount=lambda *_a: expected_price,
        ),
        "latest_trusted_telegram_usernames": lambda _events: {},
        "normalize_username": lambda value, _tg_id: str(value or "").lstrip("@"),
        "logger": SimpleNamespace(warning=lambda *_a: None, debug=lambda *_a: None),
        "SUBSCRIPTION_REPORT_PAGE_SIZE": 8,
        "re": re,
        "local_date_from_timestamp": lambda seconds: datetime.fromtimestamp(seconds).date(),
        "now_local": lambda: datetime.fromtimestamp(NOW_SECONDS),
    }
    exec(compile(ast.Module(body=[_extract("subscription_report_text")], type_ignores=[]), "subscription_report_text", "exec"), ns)
    return ns["subscription_report_text"]


def _row(tg_id, username="", notes="", expiry=0, *, has_sub=False, enabled=True, reminder=-1, family=False):
    # Preserve the SELECT's positional column order in subscription_report_text.
    return (username, "", tg_id, expiry, reminder, int(enabled), "uuid" if has_sub else "", "", "", "active",
            tg_id, "", "uuid" if has_sub else "", "", 0, None, reminder, notes, int(family), None, None)


def test_admin_report_groups_soon_active_expired_and_unpaid_with_safe_identity_links():
    rows = [
        _row(110, "expired", "", NOW_MS - DAY_MS, has_sub=True),
        _row(120, "active", "", NOW_MS + 20 * DAY_MS, has_sub=True),
        _row(130, "soon", "", NOW_MS + 2 * DAY_MS, has_sub=True),
        _row(140, "", "Ops <review>", 0, has_sub=False),
        _row(-1, "", "invalid id", 0, has_sub=True),
    ]
    text = _report(rows)()

    assert text.index("@soon") < text.index("@active") < text.index("@expired") < text.index("Ops &lt;review&gt;")
    assert 'href="tg://user?id=130"' in text
    assert 'href="tg://user?id=-1"' not in text
    assert "<code>-1</code>" in text
    assert "Период 30 дн." not in text  # no invented per-user tariff
    assert "Без подписки" in text


def test_subscription_pages_clamp_to_total_page():
    rows = [_row(100 + index, f"user{index}", has_sub=True, expiry=NOW_MS + (20 + index) * DAY_MS) for index in range(10)]
    report = _report(rows)
    assert "Страница 2/2" in report(999)
    assert "user9" in report(999)


def test_long_operator_note_is_bounded_before_html_rendering():
    text = _report([_row(777, notes="<comment>" * 1000)])()
    assert text.count("&lt;comment&gt;") <= 34
    assert "<comment>" not in text
    assert 'href="tg://user?id=777"' in text


def test_zero_day_reminder_is_not_misreported_as_not_sent():
    text = _report([_row(778, "active", expiry=NOW_MS + 30 * DAY_MS, has_sub=True, reminder=0)])()
    assert "Уведомление: за 0 дн." in text
    assert "Уведомление: не отправлялось" not in text


def test_family_custom_renewal_amount_is_shown_without_claiming_purchase_period():
    text = _report([_row(779, "member", expiry=NOW_MS + 30 * DAY_MS, has_sub=True, family=True)], expected_price=400)()
    assert "Тип: Семейная · 400 ₽ к продлению · 🔔 Уведомление:" in text
    assert "Период 30 дн." not in text


def test_splitter_keeps_utf16_length_under_telegram_limit_at_boundary():
    splitter = _extract("split_subscription_report")
    ns = {"re": re}
    exec(compile(ast.Module(body=[splitter], type_ignores=[]), "split_subscription_report", "exec"), ns)
    text = ("🙂&amp;" * 900) + "\n"
    chunks = ns["split_subscription_report"](text, max_length=4096)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
