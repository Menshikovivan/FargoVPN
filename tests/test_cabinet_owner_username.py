from __future__ import annotations

import ast
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

from user_identity import normalize_username


ROOT = Path(__file__).resolve().parents[1]


def _family_resolver(owner: dict[str, Any], member: dict[str, Any], latest_username: str):
    source = (ROOT / "app/cabinet_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    public_username = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "public_username"
    )
    resolver = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_user_for_cabinet"
    )
    public_username.decorator_list = []
    resolver.decorator_list = []

    class LinkResult:
        def fetchone(self):
            return (100,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return LinkResult()

    namespace = {
        "Any": Any,
        "re": re,
        "normalize_username": normalize_username,
        "Path": Path,
        "_connect": lambda _db_path: Connection(),
        "_latest_username": lambda _tg_id, _db_path: latest_username,
        "get_user": lambda tg_id, _db_path: owner if tg_id == 100 else member,
    }
    exec(
        compile(ast.Module(body=[public_username], type_ignores=[]), "public_username", "exec"),
        namespace,
    )
    exec(
        compile(ast.Module(body=[resolver], type_ignores=[]), "resolve_user_for_cabinet", "exec"),
        namespace,
    )
    return namespace["resolve_user_for_cabinet"](200, "unused")


def test_family_cabinet_does_not_resurrect_owner_history_as_username():
    user = _family_resolver(
        {"tg_id": 100, "username": "id_100", "display_name": "Owner Name"},
        {"tg_id": 200, "username": "member", "display_name": "Member Name"},
        "owner_real",
    )

    assert user["tg_id"] == 200
    assert user["username"] == "member"
    assert user["family_owner_tg_id"] == 100
    assert user["family_owner_username"] == ""
    assert user["family_owner_display_name"] == "Owner Name"


def test_family_cabinet_does_not_expose_synthetic_owner_username_without_history():
    user = _family_resolver(
        {"tg_id": 100, "username": "id_100", "display_name": "Owner Name"},
        {"tg_id": 200, "username": "member", "display_name": "Member Name"},
        "id_100",
    )

    assert user["family_owner_username"] == ""
    assert user["family_owner_display_name"] == "Owner Name"


def test_cabinet_resolver_ignores_stale_owner_history_when_current_username_is_synthetic():
    user = _family_resolver(
        {"tg_id": 100, "username": "id_100", "display_name": "Owner Name"},
        {"tg_id": 200, "username": "member", "display_name": "Member Name"},
        "owner_real",
    )

    assert user["family_owner_username"] == ""


def test_public_username_rejects_only_identity_placeholders_and_numeric_ids():
    source = (ROOT / "app/cabinet_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "public_username"
    )
    namespace = {"Any": Any, "re": re, "normalize_username": normalize_username}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "public_username", "exec"), namespace)

    resolve = namespace["public_username"]
    assert resolve(100, "id_100", "@real_nick") == "real_nick"
    assert resolve(100, "100", "id_100") == ""
    assert resolve(100, "id_200") == ""
    assert resolve(100, "id_0100") == ""
