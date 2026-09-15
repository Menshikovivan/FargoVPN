#!/usr/bin/env python3
"""Detached worker: resolve live 3x-ui subscription URLs and send them to users."""
from __future__ import annotations

import argparse
import html
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx

import config
import subscription_refresh_manager as manager
from services.xui_api import fetch_snapshot_sync, fetch_subscription_settings_sync, subscription_url_from_settings


def _users() -> list[dict[str, Any]]:
    connection = sqlite3.connect(str(config.DB_PATH), timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT tg_id, COALESCE(username,'') AS username, COALESCE(email,'') AS email FROM users WHERE tg_id>0 GROUP BY tg_id ORDER BY tg_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _telegram_send(client: httpx.Client, tg_id: int, text: str) -> tuple[bool, str]:
    endpoint = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    try:
        response = client.post(endpoint, data={"chat_id": int(tg_id), "text": text}, timeout=httpx.Timeout(30, connect=10))
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 200 and payload.get("ok"):
            return True, "OK"
        return False, str(payload.get("description") or f"Telegram HTTP {response.status_code}")
    except httpx.RequestError as exc:
        return False, str(exc)


def _find_client(snapshot: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    email = str(row.get("email") or "").strip().lower()
    if email:
        candidate = snapshot.get("by_email", {}).get(email)
        if isinstance(candidate, dict):
            return candidate
    tg_id = int(row.get("tg_id") or 0)
    candidate = snapshot.get("by_tg_id", {}).get(tg_id)
    return dict(candidate) if isinstance(candidate, dict) else None


def run(job_id: str) -> int:
    rows = _users()
    started_at = datetime.now(timezone.utc)
    recent_log: list[str] = [f"Подготовка: найдено {len(rows)} Telegram-пользователей"]
    manager.write_status(job_id, "running", progress=1 if rows else 100, total=len(rows), processed=0, delivered=0, failed=0, skipped=0, message=f"Подготовка: найдено {len(rows)} пользователей", error="", started_at=started_at.isoformat(timespec="seconds"), current_user="", recent_log=recent_log[-30:])
    if not rows:
        manager.write_status(job_id, "completed", progress=100, total=0, processed=0, delivered=0, failed=0, skipped=0, message="Telegram-пользователей с подписками не найдено")
        return 0

    try:
        snapshot = fetch_snapshot_sync(force=True)
        # Both the client snapshot and subscription settings are read live from 3x-ui.
        settings = fetch_subscription_settings_sync(force=True)
        if not bool(settings.get("subEnable", True)):
            raise RuntimeError("В 3x-ui сервер подписок отключён")
    except Exception as exc:
        manager.write_status(job_id, "failed", progress=0, total=len(rows), processed=0, delivered=0, failed=len(rows), skipped=0, message="Не удалось получить актуальные данные 3x-ui", error=str(exc)[:1000], finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return 1

    delivered = failed = skipped = 0
    failures: list[str] = []
    actor = str(manager.read_status(job_id).get("actor") or "web")
    audit_rows: list[tuple[int, str, str]] = []
    with httpx.Client(timeout=httpx.Timeout(30, connect=10), trust_env=False, headers={"Accept": "application/json"}) as client:
        for index, row in enumerate(rows, start=1):
            tg_id = int(row.get("tg_id") or 0)
            username = str(row.get("username") or "").strip()
            try:
                candidate = _find_client(snapshot, row)
                if not candidate:
                    skipped += 1
                    failures.append(f"{tg_id}: клиент 3x-ui не найден")
                    reason = "клиент 3x-ui не найден"
                else:
                    sub_id = str(candidate.get("sub_id") or "").strip()
                    if not sub_id:
                        skipped += 1
                        failures.append(f"{tg_id}: отсутствует Sub ID")
                        reason = "отсутствует Sub ID"
                    else:
                        url = subscription_url_from_settings(settings, sub_id, fallback_base_url="")
                        if not url:
                            raise RuntimeError("3x-ui не вернула публичный URL подписки")
                        text = (
                            "🔄 <b>Обновление ссылки VPN</b>\n\n"
                            "Администратор обновил адрес VPN-сервера. Используйте новую ссылку подписки ниже:\n\n"
                            f"<code>{html.escape(url)}</code>\n\n"
                            "📱 Откройте приложение VPN → обновите/замените подписку по этой ссылке.\n"
                            "Ваша подписка и срок действия не изменились."
                        )
                        # Telegram HTML is deliberate and the URL is derived from trusted 3x-ui settings + opaque Sub ID.
                        endpoint = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
                        response = None
                        payload: dict[str, Any] = {}
                        for attempt in range(4):
                            response = client.post(endpoint, data={"chat_id": tg_id, "text": text, "parse_mode": "HTML"})
                            try:
                                payload = response.json()
                            except ValueError:
                                payload = {}
                            if response.status_code == 200 and payload.get("ok"):
                                break
                            retry_after = 0
                            params = payload.get("parameters") if isinstance(payload, dict) else None
                            if isinstance(params, dict):
                                try:
                                    retry_after = int(params.get("retry_after") or 0)
                                except (TypeError, ValueError):
                                    retry_after = 0
                            if response.status_code == 429 and attempt < 3:
                                time.sleep(min(30, max(1, retry_after)))
                                continue
                            raise RuntimeError(str(payload.get("description") or f"Telegram HTTP {response.status_code}"))
                        delivered += 1
                        reason = "ссылка отправлена"
                        audit_rows.append((tg_id, username, "subscription_url_refresh_sent"))
            except Exception as exc:
                failed += 1
                reason = str(exc)[:300]
                failures.append(f"{tg_id}: {reason}")
            processed = index
            progress = max(2, min(99, int(processed * 100 / max(1, len(rows)))))
            manager.write_status(job_id, "running", progress=progress, total=len(rows), processed=processed, delivered=delivered, failed=failed, skipped=skipped, message=f"Обработано {processed} из {len(rows)}; отправлено {delivered}", error="", last_error=(failures[-1] if failures else ""))
            time.sleep(max(0.04, float(getattr(config, "SUBSCRIPTION_REFRESH_SEND_DELAY_SECONDS", getattr(config, "BROADCAST_SEND_DELAY_SECONDS", 0.04)))))

    try:
        connection = sqlite3.connect(str(config.DB_PATH), timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for tg_id, username, event_type in audit_rows:
                connection.execute(
                    "INSERT INTO user_events(created_at,tg_id,username,direction,event_type,text,actor,success,metadata) VALUES(?,?,?,'out',?,?,1,?)",
                    (now, tg_id, username[:120], event_type, "Отправлена актуальная ссылка подписки из 3x-ui", actor[:120], "{\"source\":\"3x-ui-live\"}"),
                )
            connection.execute(
                "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
                (actor[:120], "subscription_urls_refresh", f"delivered={delivered}; failed={failed}; skipped={skipped}"),
            )
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error:
        pass

    finished_at = datetime.now(timezone.utc)
    elapsed = max(0.0, (finished_at - started_at).total_seconds())
    final = f"Готово: отправлено {delivered}, ошибок {failed}, пропущено {skipped}"
    state = "completed"
    recent_log.append(final)
    manager.write_status(job_id, state, progress=100, total=len(rows), processed=len(rows), delivered=delivered, failed=failed, skipped=skipped, message=final, error="", failure_examples=failures[:20], finished_at=finished_at.isoformat(timespec="seconds"), elapsed_seconds=round(elapsed, 2), current_user="", last_action="Завершено", recent_log=recent_log[-30:])
    manager.cleanup()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    return run(str(args.job_id))


if __name__ == "__main__":
    raise SystemExit(main())
