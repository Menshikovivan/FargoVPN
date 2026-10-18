from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


APP = Path(__file__).resolve().parents[1] / "app"


def _load_journal_helper():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_journal_outgoing"
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "BackgroundTasks": object,
        "_safe_journal_outgoing_sync": lambda *args, **kwargs: 91,
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "webapp_journal_helper.py", "exec"),
        namespace,
    )
    return namespace["_journal_outgoing"]


def test_chat_journal_write_is_deferred_to_background_tasks():
    queued = []

    class Tasks:
        def add_task(self, function, *args, **kwargs):
            queued.append((function, args, kwargs))

    persist = _load_journal_helper()
    task_queue = Tasks()
    event_id = persist(
        123,
        task_queue,
        event_type="admin_message",
        text="hello",
        success=True,
    )

    assert event_id is None
    assert len(queued) == 1
    function, args, kwargs = queued[0]
    assert function.__name__ == "<lambda>"
    assert args == (123,)
    assert kwargs == {"event_type": "admin_message", "text": "hello", "success": True}


def test_chat_post_schedules_journal_compatibility_log_and_audit_after_delivery():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    route = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "message_user"
    )
    route_source = ast.get_source_segment(source, route)
    assert route_source is not None
    assert "background_tasks: BackgroundTasks" in route_source
    assert "return_event=True, background_tasks=background_tasks" in route_source
    assert "_safe_message_log, actor, tg_id" in route_source
    assert "background_tasks.add_task(audit" in route_source
