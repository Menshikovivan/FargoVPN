from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
WEB = (ROOT / "webapp.py").read_text(encoding="utf-8")


def func(name: str) -> str:
    tree = ast.parse(WEB)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(WEB, node) or ""
    raise AssertionError(name)


def test_panel_message_success_is_not_coupled_to_compatibility_log():
    src = func("message_user")
    assert "_safe_message_log(" in src
    assert "return_event=True" in src
    assert "_safe_event_for_id(" in WEB
    assert "return JSONResponse({\"ok\": True" in src
    assert "with database() as connection:" not in src


def test_message_transport_journal_is_best_effort():
    src = func("telegram_send")
    assert "_safe_journal_outgoing_sync(" in src


def test_media_transport_journal_is_best_effort():
    src = func("telegram_send_media")
    assert "_safe_journal_outgoing_sync(" in WEB
    assert "def journal_media" in src
    assert "message_journal.record_outgoing_sync(" not in src


def test_create_user_does_not_turn_post_creation_sync_failure_into_502():
    src = func("create_user")
    assert "ensure_subscription_sync(" in src
    assert "fetch_and_sync(force=True" in src
    assert "sync_warning =" in src
    assert "auth_warning =" in src


def test_adjust_days_distinguishes_remote_success_from_local_sync_failures():
    src = func("adjust_days")
    assert src.index("change_client_days_sync(") < src.index("local_warning =")
    assert "fetch_and_sync(force=True" in src
    assert "sync_warning =" in src
    assert "warning_text = local_warning + sync_warning" in src


def test_toggle_user_distinguishes_remote_success_from_refresh_failure():
    src = func("toggle_user")
    assert "set_client_status_sync(" in src
    assert "fetch_and_sync(force=True" in src
    assert "sync_warning =" in src


def test_delete_user_logs_deletion_only_after_primary_operation():
    src = func("delete_user")
    assert src.index("delete_client_sync(") < src.index('event_type="user_deleted"')
    assert "3x-ui уже удалён" in src


def test_comment_update_distinguishes_3xui_from_local_bookkeeping():
    src = func("user_comment_update")
    assert "local_warning =" in src
    assert "3x-ui обновлён, но локальная заметка" in src


def test_bind_telegram_reports_partial_secondary_failures_as_warning():
    src = func("bind_telegram_user")
    assert "warnings: list[str] = []" in src
    assert "3x-ui не обновлена автоматически" in src
    assert "авторизация Telegram не обновлена" in src


def test_update_status_reads_are_best_effort_after_jobs_start():
    for name in ("updates_publish", "updates_upload_and_apply", "updates_apply", "updates_force_version", "updates_rollback"):
        src = func(name)
        if name == "updates_rollback":
            assert "start_rollback_job(" in src
            assert "update_manager.read_status()" in src
        elif name == "updates_publish":
            assert "start_publish_job(" in src
            assert "audit(actor, " in src
        else:
            assert "start_update_job(" in src
            assert "update_manager.read_status()" in src


def test_restart_service_does_not_emit_unhandled_500_on_systemd_run_failure():
    src = func("restart_service")
    assert "try:\n        schedule_restart(" in src
    assert "Не удалось запланировать перезапуск служб" in src


def test_audit_is_already_best_effort_and_remains_non_fatal():
    src = WEB[WEB.index("def audit("):WEB.index("def panel_timezone_name")]
    assert "except Exception:" in src
    assert "pass" in src
