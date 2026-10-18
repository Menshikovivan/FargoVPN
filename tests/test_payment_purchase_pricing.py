from __future__ import annotations

import ast
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def test_inline_buy_handler_passes_callback_user_into_purchase():
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    handler = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "dynamic_user_menu"
    )
    calls = [
        node for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "start_purchase"
    ]
    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords}
    payer = keywords.get("payer_tg_id")
    assert isinstance(payer, ast.Name) and payer.id == "user_id"


def test_purchase_price_is_resolved_from_explicit_payer_profile():
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    purchase = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "start_purchase"
    )
    params = {arg.arg for arg in purchase.args.args}
    assert "payer_tg_id" in params

    owner_assignment = next(
        node for node in ast.walk(purchase)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "payer_id" for target in node.targets)
    )
    assert isinstance(owner_assignment.value, ast.Call)
    assert isinstance(owner_assignment.value.func, ast.Name)
    assert owner_assignment.value.func.id == "int"

    calls = [
        node for node in ast.walk(purchase)
        if isinstance(node, ast.Attribute) and node.attr == "payment_profile_id"
    ]
    assert len(calls) == 1
    threaded_call = next(
        node for node in ast.walk(purchase)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        and node.args and node.args[0] is calls[0]
    )
    assert isinstance(threaded_call.args[1], ast.Name)
    assert threaded_call.args[1].id == "payer_id"


def test_receipt_requotes_when_family_price_changes_from_200_to_400():
    """A two-person family change invalidates a previously shown 200 ₽ quote."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    quote_check = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "purchase_quote_changed"
    )
    namespace = {}
    exec(compile(ast.Module(body=[quote_check], type_ignores=[]), "main.py", "exec"), namespace)
    check = namespace["purchase_quote_changed"]
    assert check(101, 200, 101, 400) is True
    assert check(101, 200, 101, 200) is False
    assert check(102, 200, 101, 200) is True

    handler = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "process_receipt_photo"
    )
    stale_quote_branch = next(
        item for item in ast.walk(handler)
        if isinstance(item, ast.If)
        and isinstance(item.test, ast.Call)
        and isinstance(item.test.func, ast.Name)
        and item.test.func.id == "purchase_quote_changed"
    )
    submit_reference = next(
        item for item in ast.walk(handler)
        if isinstance(item, ast.Name) and item.id == "submit_receipt_sync"
    )
    assert stale_quote_branch.lineno < submit_reference.lineno
    assert any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "update_data"
        for item in ast.walk(stale_quote_branch)
    )
    assert any(
        isinstance(item, ast.Constant)
        and isinstance(item.value, str)
        and "Чек не принят" in item.value
        for item in ast.walk(stale_quote_branch)
    )


@pytest.mark.parametrize(
    ("member_count", "custom_price", "expected"),
    [(1, None, 200), (2, None, 400), (4, None, 800), (4, 777, 777), (4, 0, 0)],
)
def test_family_quote_scales_or_uses_per_profile_override(
    monkeypatch, member_count, custom_price, expected
):
    sys.path.insert(0, str(APP))
    if "config" not in sys.modules:
        cfg = types.ModuleType("config")
        cfg.PAYMENT_PRICE = 200
        sys.modules["config"] = cfg
    from services import billing

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            class Result:
                def __init__(self, row):
                    self.row = row

                def fetchone(self):
                    return self.row

            if "SELECT profile_tg_id FROM telegram_profile_links" in sql:
                return Result((101,))
            if "SELECT custom_payment_price FROM users" in sql:
                return Result((custom_price,))
            if "SELECT COUNT(*) FROM telegram_profile_links" in sql:
                return Result((member_count - 1,))
            raise AssertionError(f"Unexpected billing query: {sql}")

    monkeypatch.setattr(billing.db, "connect", lambda _path=None: FakeConnection())
    monkeypatch.setattr(billing.config, "PAYMENT_PRICE", 200, raising=False)

    assert billing.expected_payment_amount(101, "ignored-test-db") == expected
