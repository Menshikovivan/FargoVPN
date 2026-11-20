import ast
import asyncio
import html
import logging
import time
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from psycopg import errors
from sqlalchemy.exc import DBAPIError

import db


APP = Path(__file__).resolve().parents[1] / "app"


def load_function(filename, name, **namespace):
    path = APP / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if getattr(node, "name", None) == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def database_error(error_type):
    return DBAPIError(None, None, error_type("synthetic database failure"))


@pytest.mark.parametrize("error_type", [errors.SerializationFailure, errors.DeadlockDetected, errors.LockNotAvailable])
def test_unread_retry_accepts_transient_postgres_conflicts(error_type):
    wait = Mock()
    operation = Mock(side_effect=[database_error(error_type), 17])
    retry = load_function("user_events.py", "_with_busy_retry", DBAPIError=DBAPIError,
                          database_adapter=db, time=SimpleNamespace(sleep=wait))
    assert retry(operation) == 17
    assert operation.call_count == 2
    wait.assert_called_once_with(0.04)


@pytest.mark.parametrize(("error_type", "attempts"), [(errors.UndefinedTable, 1), (errors.LockNotAvailable, 5)])
def test_unread_retry_propagates_permanent_or_exhausted_errors(error_type, attempts):
    error = database_error(error_type)
    wait = Mock()
    operation = Mock(side_effect=error)
    retry = load_function("user_events.py", "_with_busy_retry", DBAPIError=DBAPIError,
                          database_adapter=db, time=SimpleNamespace(sleep=wait))
    with pytest.raises(DBAPIError) as caught:
        retry(operation)
    assert caught.value is error
    assert operation.call_count == attempts
    assert wait.call_count == attempts - 1


def test_summary_preserves_primary_totals_when_optional_query_fails():
    totals = Mock()
    totals.execute.side_effect = [
        SimpleNamespace(fetchone=lambda: (12, 7, 4)),
        SimpleNamespace(fetchone=lambda: (3,)),
    ]
    unread = Mock()
    unread.execute.side_effect = database_error(errors.UndefinedTable)
    connect = Mock(side_effect=[nullcontext(totals), unread])
    summary = load_function(
        "main.py", "admin_summary_text", time=time, html=html,
        config=SimpleNamespace(DB_PATH="unused"), database_adapter=SimpleNamespace(connect=connect),
        DBAPIError=DBAPIError, closing=closing, current_version=lambda: "5.3.4",
        logger=logging.getLogger(__name__),
    )

    result = summary()
    assert "Всего пользователей: <b>12</b>" in result
    assert "Ожидают проверки: <b>3</b>" in result
    assert "Непрочитанных сообщений: <b>0</b>" in result
    assert totals.execute.call_count == 2
    unread.close.assert_called_once()


def test_audit_write_failure_does_not_repeat_subscription_delivery():
    connection = Mock()
    connection.execute.side_effect = [None, None, database_error(errors.UndefinedTable)]
    send = Mock(return_value=(True, "OK", 123))
    status = Mock()
    run = load_function(
        "subscription_refresh_worker.py", "run", datetime=datetime, timezone=timezone,
        time=SimpleNamespace(sleep=Mock()), html=html, DBAPIError=DBAPIError,
        logger=logging.getLogger(__name__), config=SimpleNamespace(DB_PATH="unused"),
        database_adapter=SimpleNamespace(connect=Mock(return_value=connection),
                                         postgres_sqlstate=db.postgres_sqlstate),
        httpx=SimpleNamespace(Client=lambda **kwargs: nullcontext(object()), Timeout=httpx.Timeout),
        manager=SimpleNamespace(write_status=status, read_status=lambda _: {}, cleanup=Mock()),
        message_journal=SimpleNamespace(record_outgoing_sync=Mock()),
        _users=lambda: [{"tg_id": 42, "username": "test"}],
        fetch_snapshot_sync=lambda **kwargs: {},
        fetch_subscription_settings_sync=lambda **kwargs: {},
        _find_client=lambda *_: {"sub_id": "synthetic"}, _telegram_send=send,
        subscription_url_from_settings=lambda *args, **kwargs: "https://example.test/sub/synthetic",
    )

    assert run("synthetic-job") == 0
    send.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()
    args, details = status.call_args
    assert args == ("synthetic-job", "completed")
    assert (details["processed"], details["delivered"], details["failed"]) == (1, 1, 0)
    assert any("Не удалось сохранить аудит" in entry for entry in details["recent_log"])


@pytest.mark.parametrize("handler", ["manual_grant_from_shared_user", "manual_grant_process"])
def test_manual_subscription_messages_escape_url_as_html_text(handler):
    url = "https://example.test/sub/synthetic?name=ivan&label=<>"
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), text="42 ivan", answer=AsyncMock(),
        users_shared=SimpleNamespace(user_ids=[42], users=[SimpleNamespace(username="ivan")]),
    )
    send = AsyncMock(return_value=object())
    subscription = SimpleNamespace(created=True, sub_id="synthetic", tg_id=42,
                                   username="ivan", expiry_time=0)
    grant = load_function(
        "main.py", handler, asyncio=asyncio, html=html, Message=object, FSMContext=object,
        config=SimpleNamespace(ADMIN_IDS=[1], DB_PATH="unused"), SUBSCRIPTION_DAYS=30,
        ensure_subscription_sync=lambda *args: subscription,
        current_subscription_url_sync=lambda *args, **kwargs: url,
        user_events=SimpleNamespace(safe_record_event=Mock()),
        message_journal=SimpleNamespace(service_message=nullcontext),
        bot=SimpleNamespace(send_message=send), ReplyKeyboardRemove=object, get_admin_menu=object,
        logger=logging.getLogger(__name__),
    )

    asyncio.run(grant(message, SimpleNamespace(clear=AsyncMock())))
    message.answer.assert_awaited_once()
    contents = [message.answer.await_args.args[0]]
    if handler == "manual_grant_process":
        send.assert_awaited_once()
        assert send.await_args.kwargs["parse_mode"] == "HTML"
        contents.append(send.await_args.args[1])
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert all(f"<code>{html.escape(url)}</code>" in content for content in contents)
