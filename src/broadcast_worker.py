#!/usr/bin/env python3
"""Detached Telegram broadcast worker used by the web panel."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

import broadcast_manager
import config


def _extract_file_id(kind: str, payload: dict[str, Any]) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return ""
    if kind == "photo":
        photos = result.get("photo")
        if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
            return str(photos[-1].get("file_id") or "")
    item = result.get(kind)
    return str(item.get("file_id") or "") if isinstance(item, dict) else ""


def _telegram_call(
    client: httpx.Client,
    method: str,
    *,
    data: dict[str, Any],
    files: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], str]:
    endpoint = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    last_error = "Неизвестная ошибка Telegram"
    for attempt in range(4):
        try:
            if files:
                for value in files.values():
                    try:
                        handle = value[1] if isinstance(value, tuple) and len(value) > 1 else None
                        if handle is not None and hasattr(handle, "seek"):
                            handle.seek(0)
                    except (OSError, ValueError):
                        pass
            response = client.post(endpoint, data=data, files=files)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code == 200 and payload.get("ok"):
                return True, payload, "OK"
            description = str(payload.get("description") or f"Telegram HTTP {response.status_code}")
            last_error = description
            retry_after = 0
            parameters = payload.get("parameters") if isinstance(payload, dict) else None
            if isinstance(parameters, dict):
                try:
                    retry_after = int(parameters.get("retry_after") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
            if response.status_code == 429 and attempt < 3:
                time.sleep(min(30, max(1, retry_after)))
                continue
            return False, payload, description
        except httpx.RequestError as error:
            last_error = str(error)
            if attempt < 3:
                time.sleep(1.0 + attempt)
                continue
    return False, {}, last_error


def _record_result(manifest: dict[str, Any], delivered_ids: list[int], failed: int) -> None:
    if not delivered_ids and failed <= 0:
        return
    kind = str(manifest.get("kind") or "text")
    message = str(manifest.get("message") or "")
    filename = str(manifest.get("filename") or "")
    display = message or {
        "photo": f"[Массовая рассылка: фото {filename}]",
        "video": f"[Массовая рассылка: видео {filename}]",
        "document": f"[Массовая рассылка: документ {filename}]",
    }.get(kind, "[Массовая рассылка]")
    actor = str(manifest.get("actor") or "web")
    metadata = json.dumps(
        {
            "broadcast_job": str(manifest.get("job_id") or ""),
            "kind": kind,
            "filename": filename,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(str(config.DB_PATH), timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection.executemany(
            """
            INSERT INTO user_events(
                created_at,tg_id,username,direction,event_type,text,actor,success,metadata
            ) VALUES(?,?,?,'out','broadcast_message',?,?,1,?)
            """,
            [(timestamp, tg_id, "", display[:8000], actor[:120], metadata) for tg_id in delivered_ids],
        )
        connection.execute(
            "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
            (
                actor[:120],
                "web_broadcast_completed",
                json.dumps(
                    {"job_id": manifest.get("job_id"), "delivered": len(delivered_ids), "failed": failed},
                    ensure_ascii=False,
                )[:4000],
            ),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
    finally:
        connection.close()


def run(job_id: str, startup_delay: float = 0.0) -> int:
    manifest = broadcast_manager.read_manifest(job_id)
    if not manifest:
        broadcast_manager.write_status(job_id, "failed", error="Манифест рассылки не найден", message="Рассылка не выполнена")
        return 1
    recipients = manifest.get("recipients") if isinstance(manifest.get("recipients"), list) else []
    total = len(recipients)
    kind = str(manifest.get("kind") or "text")
    message = str(manifest.get("message") or "")
    payload_path = Path(str(manifest.get("payload_path") or "")) if manifest.get("payload_path") else None
    delivered_ids: list[int] = []
    failures: list[str] = []
    reusable_file_id = ""
    broadcast_manager.write_status(
        job_id,
        "queued",
        progress=2,
        total=total,
        processed=0,
        delivered=0,
        failed=0,
        message=f"Команда принята фоновым процессом; подготовлено получателей: {total}",
        error="",
        worker_pid=os.getpid(),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if startup_delay > 0:
        time.sleep(min(10.0, max(0.0, startup_delay)))
    broadcast_manager.write_status(
        job_id,
        "running",
        progress=3,
        total=total,
        processed=0,
        delivered=0,
        failed=0,
        message=f"Начата рассылка по {total} пользователям",
        error="",
    )
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
            for index, item in enumerate(recipients, start=1):
                try:
                    tg_id = int(item.get("tg_id") if isinstance(item, dict) else item)
                except (TypeError, ValueError):
                    failures.append(f"Некорректный TG ID: {item}")
                    continue
                if kind == "text":
                    ok, _payload, detail = _telegram_call(
                        client,
                        "sendMessage",
                        data={"chat_id": tg_id, "text": message},
                    )
                else:
                    method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
                    data: dict[str, Any] = {"chat_id": tg_id}
                    if message:
                        data["caption"] = message
                    if reusable_file_id:
                        data[kind] = reusable_file_id
                        ok, response_payload, detail = _telegram_call(client, method, data=data)
                    else:
                        if payload_path is None or not payload_path.is_file():
                            raise RuntimeError("Файл рассылки не найден")
                        with payload_path.open("rb") as handle:
                            files = {
                                kind: (
                                    str(manifest.get("filename") or payload_path.name),
                                    handle,
                                    str(manifest.get("content_type") or "application/octet-stream"),
                                )
                            }
                            ok, response_payload, detail = _telegram_call(client, method, data=data, files=files)
                        if ok:
                            reusable_file_id = _extract_file_id(kind, response_payload)
                if ok:
                    delivered_ids.append(tg_id)
                else:
                    failures.append(f"{tg_id}: {detail}")
                processed = index
                failed_count = processed - len(delivered_ids)
                progress = max(2, min(99, int(processed * 100 / max(1, total))))
                broadcast_manager.write_status(
                    job_id,
                    "running",
                    progress=progress,
                    total=total,
                    processed=processed,
                    delivered=len(delivered_ids),
                    failed=failed_count,
                    message=f"Обработано {processed} из {total}; доставлено {len(delivered_ids)}",
                    error="",
                    last_error=failures[-1] if failures else "",
                )
                time.sleep(max(0.02, float(getattr(config, "BROADCAST_SEND_DELAY_SECONDS", 0.04))))
        _record_result(manifest, delivered_ids, len(failures))
        final_message = f"Рассылка завершена: доставлено {len(delivered_ids)} из {total}, ошибок {len(failures)}"
        broadcast_manager.write_status(
            job_id,
            "completed",
            progress=100,
            total=total,
            processed=total,
            delivered=len(delivered_ids),
            failed=len(failures),
            message=final_message,
            error="",
            failure_examples=failures[:20],
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if payload_path is not None:
            payload_path.unlink(missing_ok=True)
        return 0
    except Exception as error:
        with broadcast_manager.log_path(job_id).open("a", encoding="utf-8") as log:
            log.write(traceback.format_exc() + "\n")
        broadcast_manager.write_status(
            job_id,
            "failed",
            progress=int(broadcast_manager.read_status(job_id).get("progress") or 1),
            message="Рассылка остановлена из-за ошибки",
            error=str(error),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Фоновая массовая рассылка Telegram")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    args = parser.parse_args()
    return run(args.job_id, args.startup_delay)


if __name__ == "__main__":
    raise SystemExit(main())
