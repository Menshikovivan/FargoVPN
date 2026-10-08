from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "app"


def _messages_function() -> ast.FunctionDef:
    tree = ast.parse((ROOT / "webapp.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "messages_page":
            return node
    raise AssertionError("messages_page not found")


def test_messages_page_defines_unread_map_before_first_use():
    func = _messages_function()
    assignment_line = None
    first_use_line = None
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "unread_map":
                    assignment_line = min(assignment_line or node.lineno, node.lineno)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "unread_map":
            first_use_line = min(first_use_line or node.lineno, node.lineno)
    assert assignment_line is not None
    assert first_use_line is not None
    assert assignment_line < first_use_line


def test_messages_page_marks_selected_chat_read_before_rendering_user_rows():
    func = _messages_function()
    source = ast.get_source_segment((ROOT / "webapp.py").read_text(encoding="utf-8"), func)
    assert source is not None
    assert source.index("user_events.mark_messages_read(") < source.index("user_rows = []")
    assert source.index("unread_snapshot = user_events.unread_messages_summary(") < source.index("user_rows = []")
