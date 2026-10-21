from __future__ import annotations

import ast
import html
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from user_identity import get_display_name as canonical_get_display_name


def _load_report_helpers():
    root = Path(__file__).resolve().parents[1]
    reminder_tree = ast.parse((root / "app" / "trigger_reminders.py").read_text(encoding="utf-8"))
    billing_tree = ast.parse((root / "app" / "services" / "billing.py").read_text(encoding="utf-8"))
    names = {"_reminder_report_line", "_utf16_units", "_reminder_report_chunks"}
    nodes = [item for item in reminder_tree.body if isinstance(item, ast.FunctionDef) and item.name in names]
    billing_nodes = [item for item in billing_tree.body if isinstance(item, ast.FunctionDef) and item.name in {"get_display_name", "telegram_identity_label"}]
    namespace = {"html": html, "Any": Any}
    exec(compile(ast.Module(body=billing_nodes, type_ignores=[]), "billing.py", "exec"), namespace)
    namespace["_canonical_get_display_name"] = canonical_get_display_name
    namespace["billing"] = SimpleNamespace(telegram_identity_label=namespace["telegram_identity_label"])
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "trigger_reminders.py", "exec"), namespace)
    return namespace


def test_reminder_report_uses_identity_fallbacks_and_safe_links():
    render = _load_report_helpers()["_reminder_report_line"]
    assert render(123, "@nick", "A&B", 7) == '<a href="tg://user?id=123"><b>@nick</b></a>: 7 дн.'
    assert render(123, "", "Иван & <Оля>", 1) == '<a href="tg://user?id=123"><b>Иван &amp; &lt;Оля&gt;</b></a>: 1 дн.'
    assert render(123, "", "", 0) == '<a href="tg://user?id=123"><b>ID 123</b></a>: 0 дн.'
    assert render(-123, "", "<x>", 0) == "<b>&lt;x&gt;</b>: 0 дн."
    assert render("invalid-id", "x" * 33, "Fallback", 1) == '<b>Fallback</b>: 1 дн.'
    assert render(123, "x" * 32, "Fallback", 1).startswith('<a href="tg://user?id=123"><b>@')


def test_reminder_report_bounds_labels_and_chunks_whole_html_lines():
    helpers = _load_report_helpers()
    render = helpers["_reminder_report_line"]
    chunks = helpers["_reminder_report_chunks"]
    long_comment = "界&<" * 100
    line = render(321, "", long_comment, 7)
    assert "…" in line
    assert len(line) < 500
    assert "&amp;" in line and "&lt;" in line
    entries = [render(index + 1, "", long_comment, 7) for index in range(40)]
    parts = chunks(entries)
    assert len(parts) > 1
    assert all(helpers["_utf16_units"](part) <= 3500 for part in parts)
    assert all("часть " in part for part in parts)
    joined = "\n".join(parts)
    assert all(entry in joined for entry in entries)
    assert all(part.count("<a ") == part.count("</a>") for part in parts)

