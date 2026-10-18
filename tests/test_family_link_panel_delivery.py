from __future__ import annotations

import ast
from pathlib import Path


WEB = (Path(__file__).resolve().parents[1] / "app" / "webapp.py").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    tree = ast.parse(WEB)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(WEB, node) or ""
    raise AssertionError(name)


def test_family_link_form_submits_csrf_token():
    detail = function_source("user_detail_page")
    assert 'action="/users/id/{int(tg_id)}/family-link"' in detail
    assert 'name="csrf_token" value="{html.escape(session_csrf_token(request)' in detail


def test_family_link_route_sends_one_time_link_to_primary_telegram_chat():
    route = function_source("user_family_link_request")
    assert "require_auth(request, csrf_token)" in route
    assert '"chat_id": int(tg_id)' in route
    assert "/sendMessage" in route
    assert '"disable_web_page_preview": True' in route
    assert '"family_link_request_sent"' in route
    assert '"family_link_request_send_failed"' in route
    assert "30 минут" in route
